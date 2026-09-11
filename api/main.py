import asyncio
import hmac
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agent.config import Settings
from agent.graph import initial
from agent.runtime import config, runtime


def create_app(settings=None):
    settings = settings or Settings()
    active = {}
    lock = asyncio.Lock()
    capacity = asyncio.Semaphore(4)

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

    @app.post("/tasks", status_code=202, dependencies=[Depends(auth)])
    async def start(body: TaskRequest):
        async with lock:
            if len(active) >= 32:
                raise HTTPException(429, "Task capacity reached")
            thread = str(uuid.uuid4())
            await app.state.graph.aupdate_state(config(thread), initial(body.task), as_node="__start__")
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


app = create_app()
