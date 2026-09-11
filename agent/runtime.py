from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent.brain import Brain
from agent.config import Settings
from agent.graph import build_graph
from agent.tools.core import ToolRegistry


def config(thread_id):
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 250}


@asynccontextmanager
async def runtime(settings=None, registry=None, brain=None, pause=False):
    settings = settings or Settings()
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(
        str(Path(settings.data_dir) / "checkpoints.sqlite")
    ) as saver:
        await saver.setup()
        yield build_graph(
            settings, saver, registry or ToolRegistry(settings), brain or Brain(settings), pause
        )
