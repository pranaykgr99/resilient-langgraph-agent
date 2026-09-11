import asyncio
import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from agent.config import Settings
from agent.datasets import MAX_BYTES, aggregate, DataError, run_operation
from api.main import create_app

AUTH = {"Authorization": "Bearer secret"}
SALES = (Path(__file__).parents[1] / "samples/sales.csv").read_bytes()


def settle(client, task):
    for _ in range(400):
        state = client.get('/tasks/' + task, headers=AUTH).json()
        if state['status'] != 'running':
            return state
        time.sleep(.025)
    raise AssertionError('Analysis did not settle')


def upload(client, content=SALES, filename='sales.csv', **params):
    return client.post('/datasets', params={'filename': filename, **params}, content=content, headers=AUTH)


def start(client, dataset, kind='aggregate', pause=False):
    response = client.post('/tasks', headers=AUTH, json={
        'task': 'Analyze', 'dataset_id': dataset, 'pause_after_plan': pause,
        'analysis': {'kind': kind, 'metric': 'Revenue', 'group_by': 'Category', 'date_column': 'Date'}})
    assert response.status_code == 202, response.text
    return response.json()['id']


def settings(tmp_path):
    return Settings(llm_mode='demo', data_dir=str(tmp_path), api_token='secret', backoff_base=0, max_replans=0)


def test_upload_aggregate_trend_reports_and_restart(tmp_path):
    cfg = settings(tmp_path)
    with TestClient(create_app(cfg)) as c:
        assert c.post('/datasets?filename=a.csv', content=SALES).status_code == 401
        r = upload(c)
        assert r.status_code == 201, r.text
        d = r.json()
        assert d['row_count'] == 6
        task = start(c, d['id'], pause=True)
        assert settle(c, task)['status'] == 'interrupted'
        assert c.get(f'/tasks/{task}/report', headers=AUTH).status_code == 409
    with TestClient(create_app(cfg)) as c:
        assert c.get('/datasets/' + d['id'], headers=AUTH).json()['sha256'] == d['sha256']
        assert c.post(f'/tasks/{task}/resume', headers=AUTH).status_code == 202
        state = settle(c, task)
        assert state['status'] == 'completed', state
        result = state['completed'][-1]['result']['data']
        assert result['total'] == '10000'
        assert {r['group']: r['sum'] for r in result['table']} == {'Bonding': '3600', 'Composite': '6400'}
        assert result['verified']
        assert c.get(f'/tasks/{task}/report').status_code == 401
        assert '3600' in c.get(f'/tasks/{task}/report?format=csv', headers=AUTH).text
        assert c.get(f'/tasks/{task}/report', headers=AUTH).json()['dataset_id'] == d['id']
        trend = settle(c, start(c, d['id'], 'trend'))
        rows = trend['completed'][-1]['result']['data']['table']
        assert [r['sum'] for r in rows] == ['3000', '3000', '4000']
        assert rows[0]['change_percent'] is None
        assert rows[1]['change_percent'] == '0'


@pytest.mark.parametrize('content,code', [(b'', 'empty_file'), (b'A,A\n1,2', 'bad_header'),
    (b'A,B\n1,2,3', 'ragged_rows'), (b'A\n\xff', 'encoding'), (b'A\n', 'empty_file')])
def test_bad_uploads_cleaned(tmp_path, content, code):
    with TestClient(create_app(settings(tmp_path))) as c:
        r = upload(c, content)
        assert r.status_code == 422, r.text
        assert r.json()['detail']['code'] == code
        assert not list((tmp_path / 'datasets').iterdir())


def test_limits_and_custom_mode(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        assert upload(c, b'x', 'old.xls').status_code == 415
        assert upload(c, b'x' * (MAX_BYTES + 1)).status_code == 413
        d = upload(c).json()
        assert c.post('/tasks', headers=AUTH, json={'task': 'forecast', 'dataset_id': d['id'],
            'analysis': {'kind': 'custom'}}).status_code == 422


def workbook(formula=False):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Sales'
    ws.append(['SKU', 'Revenue', 'Category', 'Date'])
    ws.append(['001', '=1+2' if formula else 12.5, 'A', '2026-01-01'])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_excel_values_sheet_and_formula(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        r = upload(c, workbook(), 'sales.xlsx')
        assert r.status_code == 201, r.text
        assert r.json()['preview'][0][0] == '001'
        assert r.json()['sheet'] == 'Sales'
        state = settle(c, start(c, r.json()['id']))
        assert state['completed'][-1]['result']['data']['total'] == '12.5'
        assert upload(c, workbook(), 'sales.xlsx', sheet='Missing').json()['detail']['code'] == 'bad_sheet'
        assert upload(c, workbook(True), 'sales.xlsx').json()['detail']['code'] == 'formula_cells'


def test_invalid_measure_escalates(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        d = upload(c, b'Category,Revenue\nA,unknown').json()
        state = settle(c, start(c, d['id']))
        assert state['status'] == 'escalated'
        assert 'bad_numeric' in state['final_answer']


def test_missing_duplicates_and_csv_formula_escape(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        d = upload(c, b'Category,Revenue\n=cmd,0.1\n=cmd,0.1\nB,\n,0.2').json()
        task = start(c, d['id'])
        state = settle(c, task)
        assert state['completed'][0]['result']['data']['duplicate_rows'] == 1
        data = state['completed'][-1]['result']['data']
        assert data['total'] == '0.4'
        assert next(r for r in data['table'] if r['group'] == 'B')['sum'] is None
        assert "'=cmd" in c.get(f'/tasks/{task}/report?format=csv', headers=AUTH).text


def test_date_validation_and_zero_baseline():
    data = {'id': 'x', 'sha256': 'x', 'row_count': 2, 'columns': [{'name': 'Date', 'missing': 0},
            {'name': 'Revenue', 'missing': 0}], 'rows': [['2026-01-01', '0'], ['2026-03-01', '4']]}
    result = aggregate(data, {'metric': 'Revenue', 'date_column': 'Date'}, True)
    assert result['table'][1]['change_percent'] is None
    data['rows'][0][0] = '2026-01-01junk'
    with pytest.raises(DataError, match='invalid date'):
        aggregate(data, {'metric': 'Revenue', 'date_column': 'Date'}, True)


def test_worker_deadline(tmp_path):
    result = asyncio.run(run_operation(tmp_path, '00000000-0000-0000-0000-000000000000', 'metadata', timeout=.00001))
    assert result['error']['code'] == 'timeout'
    assert result['error']['retryable']


def test_transient_analysis_retry(tmp_path, monkeypatch):
    from agent.tools.core import ToolRegistry
    original = ToolRegistry.invoke_dataset
    calls = []

    async def flaky(self, tool, text, dataset_id):
        calls.append(tool)
        if len(calls) == 1:
            return {'ok': False, 'tool': tool, 'error': {'code': 'timeout', 'message': 'Injected timeout', 'retryable': True}}
        return await original(self, tool, text, dataset_id)

    monkeypatch.setattr(ToolRegistry, 'invoke_dataset', flaky)
    with TestClient(create_app(settings(tmp_path))) as c:
        d = upload(c).json()
        state = settle(c, start(c, d['id']))
        assert state['status'] == 'completed'
        assert state['completed'][-1]['result']['data']['total'] == '10000'
        assert len(state['completed']) == 2
        assert any(e['decision'] == 'retry' for e in state['events'])


def test_missing_dataset_on_resume(tmp_path):
    cfg = settings(tmp_path)
    with TestClient(create_app(cfg)) as c:
        d = upload(c).json()
        task = start(c, d['id'], pause=True)
        assert settle(c, task)['status'] == 'interrupted'
        (tmp_path / 'datasets' / d['id'] / 'table.json').unlink()
        c.post(f'/tasks/{task}/resume', headers=AUTH)
        state = settle(c, task)
        assert state['status'] == 'escalated'
        assert 'missing_dataset' in state['final_answer']
