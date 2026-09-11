import asyncio
from pathlib import Path

from agent.config import Settings
from agent.graph import initial
from agent.runtime import config, runtime
from agent.tools.core import ToolRegistry


async def main():
    cfg = Settings()
    tools = ToolRegistry(cfg)

    async def delayed(tool, text):
        print("tool_executor entered; planner checkpoint committed; blocking tool", flush=True)
        (Path(cfg.data_dir) / "executing").write_text("ready")
        await asyncio.sleep(60)

    tools.execute = delayed
    async with runtime(cfg, registry=tools) as graph:
        await graph.ainvoke(initial("calculate: 7*8"), config("crash-proof"), durability="sync")


if __name__ == "__main__":
    asyncio.run(main())
