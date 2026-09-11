import argparse
import asyncio
import uuid

from agent.graph import initial
from agent.runtime import config, runtime


async def run(args):
    thread = args.id or str(uuid.uuid4())
    print(f"TASK_ID={thread}", flush=True)
    async with runtime(pause=args.pause) as graph:
        cfg = config(thread)
        snapshot = await graph.aget_state(cfg)
        if args.resume:
            if not snapshot.values:
                raise SystemExit("Unknown task id")
            print(f"RESUME saved_events={len(snapshot.values['events'])} next={snapshot.next}", flush=True)
            payload = None
        else:
            if snapshot.values:
                raise SystemExit("Task already exists; use --resume")
            if not args.task:
                raise SystemExit("A task is required")
            payload = initial(args.task)
        async for update in graph.astream(payload, cfg, stream_mode="updates", durability="sync"):
            for value in update.values():
                if isinstance(value, dict) and value.get("events"):
                    e = value["events"][-1]
                    print(f"{e['node']} -> {e['decision']}: {e['reason']}", flush=True)
        final = await graph.aget_state(cfg)
        print("PAUSED" if final.next else final.values["final_answer"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task", nargs="?")
    parser.add_argument("--id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--pause", action="store_true")
    asyncio.run(run(parser.parse_args()))
