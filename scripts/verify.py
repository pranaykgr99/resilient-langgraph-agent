"""Reproducible real executions; LLM decisions are explicitly offline demo mode."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from agent.config import Settings
from agent.graph import initial
from agent.runtime import config, runtime
from agent.tools.core import ToolFailure, ToolRegistry


async def recovery(directory):
    cfg = Settings(llm_mode="demo", data_dir=directory, backoff_base=0.01)
    tools = ToolRegistry(cfg)
    original = tools.execute

    async def forced(tool, text):
        if tool == "web_search":
            raise ToolFailure("rate_limit", "Mocked HTTP 429", True)
        return await original(tool, text)

    tools.execute = forced
    async with runtime(cfg, registry=tools) as graph:
        async for update in graph.astream(
            initial("search: exponential backoff retries"), config("forced-failure"), stream_mode="updates"
        ):
            for value in update.values():
                event = value["events"][-1]
                print(f"{event['node']} -> {event['decision']}: {event['reason']}", flush=True)
        state = await graph.aget_state(config("forced-failure"))
        print(state.values["final_answer"], flush=True)


def main():
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, "LLM_MODE": "demo", "DATA_DIR": directory}
        print("=== NORMAL TASK (offline planner, real calculator) ===", flush=True)
        subprocess.run(
            [sys.executable, "-m", "agent.run", "calculate: 125 * 1.08", "--id", "normal"],
            env=env,
            check=True,
        )
        print("=== FORCED SEARCH FAILURE (real fallback retrieval) ===", flush=True)
        asyncio.run(recovery(directory))
        print("=== KILL AND RESUME ===", flush=True)
        process = subprocess.Popen([sys.executable, "-m", "scripts.crash_worker"], env=env)
        marker = Path(directory) / "executing"
        try:
            deadline = time.monotonic() + 20
            while not marker.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Crash worker did not enter tool")
                time.sleep(0.02)
            process.kill()
            process.wait(timeout=5)
            print(f"Killed worker while tool running: returncode={process.returncode}", flush=True)
            resumed = subprocess.run(
                [sys.executable, "-m", "agent.run", "--id", "crash-proof", "--resume"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            print(resumed.stdout, end="", flush=True)
            assert "saved_events=1 next=('tool_executor',)" in resumed.stdout
            assert "planner ->" not in resumed.stdout
            assert resumed.stdout.rstrip().endswith("56")
            print("PASS: saved plan reused; resumed answer = 56", flush=True)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
