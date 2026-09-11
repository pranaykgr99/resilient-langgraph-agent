import time
from fastapi.testclient import TestClient

from agent.config import Settings
from api.main import create_app


def wait(client, thread, headers):
    for _ in range(100):
        state = client.get("/tasks/" + thread, headers=headers).json()
        if state["status"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("Task did not settle")


def test_api_pause_restart_resume(tmp_path):
    cfg = Settings(llm_mode="demo", data_dir=str(tmp_path), api_token="secret")
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_app(cfg)) as client:
        assert client.post("/tasks", json={"task": "x"}).status_code == 401
        assert client.get("/").status_code == 200
        r = client.post(
            "/tasks", json={"task": "calculate: 10*10", "pause_after_plan": True}, headers=headers
        )
        assert r.status_code == 202
        thread = r.json()["id"]
        assert wait(client, thread, headers)["status"] == "interrupted"
    with TestClient(create_app(cfg)) as client:
        assert client.post(f"/tasks/{thread}/resume", headers=headers).status_code == 202
        assert wait(client, thread, headers)["final_answer"] == "100"
        assert client.post(f"/tasks/{thread}/resume", headers=headers).status_code == 409
