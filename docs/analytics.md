# Dataset analysis workflow

## What an analyst can do

Upload a sales table and choose a question using column selectors. The application
profiles the data first, computes the selected result, checks row coverage and
total reconciliation, and exposes the result with a trace. You still decide
whether the business definitions and source data are correct.

| Analysis | Output |
| --- | --- |
| Quality | Row and column counts, inferred numeric/text types, blanks, exact duplicates |
| Grouped measures | Sum, mean, row count, valid count and missing count per group |
| Monthly trend | Monthly sums/means/counts and percentage change from the previous observed month |

Built-in workflows use a deterministic planner and verifier even in API mode.
Custom questions require a configured LLM and are limited to the available tools.
They are not a promise of forecasting, joins, filtering, arbitrary pandas code,
charts, database connections or automatic data cleaning. No live LLM test was
performed for this upgrade. General calculator/Python/search tasks remain available.

## Data rules

- CSV must be UTF-8 (BOM accepted). Comma, semicolon, tab and pipe delimiters are
  detected. The first row is the header; blank/duplicate names and ragged rows fail.
- XLSX uses the selected sheet or first sheet. Legacy XLS and macro-enabled XLSM
  are not supported. Formula/error cells are rejected; export values first.
- Maximum: 5 MiB upload, 20,000 data rows, 100 columns, 200,000 cells, 2,000
  characters per cell, 500 result groups. Expanded XLSX parts are capped at 25 MiB.
- Text is preserved, including CSV identifiers such as `001`. Excel numeric cells
  with display-only leading zero formatting cannot recover those formatted zeros;
  store identifiers as text before export.
- Empty and whitespace-only cells become null. Literal strings such as `NA` are
  retained. Numeric analysis rejects nonblank invalid numbers, currency symbols,
  thousands separators, infinity and NaN. Numbers are bounded to magnitude 1e15
  and at most 10 decimal places. Decimal arithmetic avoids binary rounding in sums.
- Blank measures are excluded from sum and mean. All-blank groups have null
  results, not zero. Missing group labels form a separate null group. Exact
  duplicate rows remain included and are flagged.
- Dates must be ISO dates or ISO timestamps. Ambiguous dates are rejected.
  No timezone conversion is performed. Months absent from data are not filled.
  Percentage change uses `(current - previous) / abs(previous) * 100`;
  the first month and zero/missing baselines return null.

## Implementation and persistence

`POST /datasets` streams a raw request body into a generated UUID directory.
A bounded subprocess parses it with the standard CSV reader or openpyxl.
The original upload is discarded after validation. Normalized rows, schema and a
SHA-256 row fingerprint are stored under `DATA_DIR/datasets/<id>/table.json`.
The filename never controls the storage path. Dataset tools receive the bound
dataset ID from graph state; a model cannot override it using tool input.

The task checkpoint stores dataset identity, schema and selected analysis.
The planner emits dataset_profile followed by dataset_aggregate or dataset_trend.
Tools return structured evidence. The verifier checks identity, fingerprint,
row coverage and source/group totals. These checks verify consistency, not source
truth or the correctness of a business metric definition.

Dataset parsing and analysis run outside the API event loop with deadlines and
Linux CPU/memory limits. They are trusted fixed operations, separate from the
restricted general Python code tool. Transient timeouts use the existing bounded
retry path; invalid data eventually escalates. Read-only work may repeat after a
crash, but does not append rows or mutate the uploaded table.

The shared Docker data volume retains datasets and checkpoints across restarts.
There is no automatic retention/deletion policy or storage quota. This is a
single-user/trusted-team application: one API token grants access to all tasks
and datasets. Built-in analysis does not call an LLM or search API. Custom mode
can send schema, filenames and computed result evidence to the configured model.
Do not treat this prototype as a public multi-tenant upload service.

## API contract

All endpoints below require `Authorization: Bearer <API_TOKEN>` when configured.

1. `POST /datasets?filename=sales.csv` with raw file bytes, content type
   `application/octet-stream`. Optional `sheet=Sales` selects an XLSX sheet.
   Returns dataset metadata, ID and up to eight preview rows (201).
2. `GET /datasets/<dataset-id>` reconnects saved metadata.
3. `POST /tasks` with the body below creates a task (202).
4. `GET /tasks/<task-id>` polls state; `POST /tasks/<task-id>/resume` resumes a pause.
5. `GET /tasks/<task-id>/report?format=csv` downloads the last accepted result table.
   `format=json` includes quality results, calculations, warnings and trace.

```json
{
  "task": "Analyze sales",
  "dataset_id": "UUID from upload",
  "analysis": {"kind": "aggregate", "metric": "Revenue", "group_by": "Category"},
  "pause_after_plan": false
}
```

For trend use kind `trend`, metric and `date_column`. For quality use kind
`profile`. Built-in selectors define the task, rather than arbitrary prose.
For custom mode use kind `custom` and put the question in `task`.

CSV export escapes formula-like text labels for spreadsheet use. It contains
only the final table; keep the JSON report for warnings and verification evidence.
Normalized source rows are not included in the JSON report.

## Verification

Run `python -m ruff check .` and `python -m pytest -q` after installing requirements.
This upgrade passed Ruff and all 45 tests on Python 3.12 in the development
workspace. Three dependency warnings were reported. The UI JavaScript also passed
Node's syntax check; interactive browser testing was not performed here.
The dataset tests cover real CSV/XLSX parsing, exact grouped and monthly totals,
authenticated downloads, missing/duplicate values, invalid inputs, formula
rejection, deadlines and persisted pause/resume. Existing agent tests also run.
Docker image rebuild and live-provider behavior must be checked in your deployment.
