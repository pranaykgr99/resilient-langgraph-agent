import asyncio
import csv
import hmac
import io
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from agent.config import Settings
from agent.graph import initial
from agent.runtime import config, runtime
from agent.state import AnalysisRequest
from agent.datasets import MAX_BYTES, directory, run_operation, number


def create_app(settings=None):
    settings = settings or Settings()
    active = {}
    lock = asyncio.Lock()
    capacity = asyncio.Semaphore(4)
    uploads = asyncio.Semaphore(2)

    @asynccontextmanager
    async def lifespan(app):
        async with runtime(settings) as graph:
            app.state.graph = graph
            yield
            for task in list(active.values()):
                task.cancel()
            await asyncio.gather(*list(active.values()), return_exceptions=True)

    app = FastAPI(title="Resilient Agent", lifespan=lifespan)

    async def auth(authorization: str = Header(default="")):
        if settings.api_token and not hmac.compare_digest(authorization, "Bearer " + settings.api_token):
            raise HTTPException(401, "Invalid API token")

    async def worker(thread, pause):
        try:
            async with capacity:
                async for _ in app.state.graph.astream(
                    None,
                    config(thread),
                    stream_mode="updates",
                    interrupt_after=["planner"] if pause else None,
                    durability="sync",
                ):
                    pass
        finally:
            active.pop(thread, None)

    def schedule(thread, pause=False):
        task = asyncio.create_task(worker(thread, pause))
        active[thread] = task
        # Retrieve background exceptions; pending checkpoints remain resumable.
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parent.parent / "ui" / "index.html")

    async def dataset_info(dataset_id):
        result = await run_operation(settings.data_dir, dataset_id, "metadata")
        if not result["ok"]:
            raise HTTPException(404 if result["error"]["code"] == "missing_dataset" else 422, result["error"])
        return result["data"]

    @app.post("/datasets", status_code=201, dependencies=[Depends(auth)])
    async def upload(request: Request, filename: str = Query(min_length=1, max_length=100),
                     sheet: str | None = Query(default=None, max_length=100)):
        """Raw binary body avoids multipart spool exhaustion; stream enforces the limit."""
        if Path(filename).suffix.lower() not in (".csv", ".xlsx"):
            raise HTTPException(415, {"code": "unsupported_format", "message": "Upload .csv or .xlsx; convert legacy .xls first."})
        async with uploads:
            thread = str(uuid.uuid4())
            folder = directory(settings.data_dir, thread)
            folder.mkdir(parents=True)
            success = False
            try:
                length = 0
                async with asyncio.timeout(30):
                    with (folder / "source").open("wb") as target:
                        async for chunk in request.stream():
                            length += len(chunk)
                            if length > MAX_BYTES:
                                raise HTTPException(413, {"code": "size_limit", "message": "Maximum upload is 5 MiB."})
                            target.write(chunk)
                result = await run_operation(settings.data_dir, thread, "ingest", {"filename": filename, "sheet": sheet})
                if not result["ok"]:
                    raise HTTPException(422, result["error"])
                success = True
                return result["data"]
            except TimeoutError:
                raise HTTPException(408, "Upload timed out") from None
            finally:
                if success:
                    (folder / "source").unlink(missing_ok=True)
                else:
                    shutil.rmtree(folder)

    @app.get("/datasets/{dataset_id}", dependencies=[Depends(auth)])
    async def get_dataset(dataset_id: uuid.UUID):
        return await dataset_info(str(dataset_id))

    @app.post("/tasks", status_code=202, dependencies=[Depends(auth)])
    async def start(body: TaskRequest):
        if body.analysis is not None and body.dataset_id is None:
            raise HTTPException(422, "Upload/select a dataset before requesting analysis.")
        dataset = await dataset_info(str(body.dataset_id)) if body.dataset_id else None
        analysis = (body.analysis or AnalysisRequest()).model_dump() if dataset else None
        task_text = body.task
        if dataset and analysis["kind"] != "custom":
            # Built-in choices, not arbitrary prose, define the task contract.
            task_text = f"Analyze {dataset['filename']}: " + json.dumps(analysis)
        if dataset and analysis["kind"] == "custom" and settings.llm_mode == "demo":
            raise HTTPException(422, "Custom questions need LLM_MODE=api. Choose a built-in analysis for offline use.")
        async with lock:
            if len(active) >= 32:
                raise HTTPException(429, "Task capacity reached")
            thread = str(uuid.uuid4())
            await app.state.graph.aupdate_state(config(thread), initial(task_text, dataset, analysis), as_node="__start__")
            schedule(thread, body.pause_after_plan)
        return {"id": thread}

    @app.get("/tasks/{thread}", dependencies=[Depends(auth)])
    async def status(thread: uuid.UUID):
        thread = str(thread)
        snapshot = await app.state.graph.aget_state(config(thread))
        if not snapshot.values:
            raise HTTPException(404, "Unknown task")
        state = dict(snapshot.values)
        state["status"] = (
            "running" if thread in active else ("interrupted" if snapshot.next else state["status"])
        )
        return {"id": thread, **state, "next": list(snapshot.next)}

    @app.get("/tasks/{thread}/report", dependencies=[Depends(auth)])
    async def report(thread: uuid.UUID, format: str = Query(default="json", pattern="^(json|csv)$")):
        snapshot = await app.state.graph.aget_state(config(str(thread)))
        if not snapshot.values:
            raise HTTPException(404, "Unknown task")
        state = snapshot.values
        if state.get("status") != "completed" or not state.get("dataset_id"):
            raise HTTPException(409, "Report is available after dataset analysis completes.")
        if format == "json":
            return Response(json.dumps(state, indent=2, allow_nan=False), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="analysis-{thread}.json"'})
        evidence = [e["result"]["data"] for e in state["completed"]
                    if e.get("accepted", True) and e["result"].get("ok")
                    and e["result"].get("tool", "").startswith("dataset_")]
        if not evidence:
            raise HTTPException(409, "No accepted tabular result")
        latest = evidence[-1]
        rows = latest.get("table", latest.get("columns", []))
        output = io.StringIO()
        writer = csv.writer(output)

        def safe(value):
            text = "" if value is None else str(value)
            if text.lstrip().startswith(("=", "+", "-", "@")) and number(text) is None:
                return "'" + text
            return text
        if rows:
            writer.writerow([safe(k) for k in rows[0]])
            writer.writerows([[safe(v) for v in row.values()] for row in rows])
        return Response("\ufeff" + output.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="analysis-{thread}.csv"'})

    @app.post("/tasks/{thread}/resume", status_code=202, dependencies=[Depends(auth)])
    async def resume(thread: uuid.UUID):
        thread = str(thread)
        async with lock:
            if thread in active:
                raise HTTPException(409, "Task is already running")
            snapshot = await app.state.graph.aget_state(config(thread))
            if not snapshot.values:
                raise HTTPException(404, "Unknown task")
            if not snapshot.next:
                raise HTTPException(409, "Task is already terminal")
            if len(active) >= 32:
                raise HTTPException(429, "Task capacity reached")
            schedule(thread)
        return {"id": thread}

    return app


class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=6000, pattern=r"\S")
    pause_after_plan: bool = False
    dataset_id: uuid.UUID | None = None
    analysis: AnalysisRequest | None = None


app = create_app()
