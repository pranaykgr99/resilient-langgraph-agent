import asyncio

from agent.brain import Brain
from agent.config import Settings
from agent.graph import initial
from agent.runtime import config, runtime
from agent.state import Plan, Review
from agent.tools.core import ToolFailure, ToolRegistry


def settings(tmp_path, **kwargs):
    return Settings(llm_mode="demo", data_dir=str(tmp_path), backoff_base=0, **kwargs)


def test_end_to_end(tmp_path):
    async def scenario():
        async with runtime(settings(tmp_path)) as graph:
            state = await graph.ainvoke(initial("calculate: 6*7"), config("normal"))
            assert state["status"] == "completed"
            assert state["final_answer"] == "42"
            assert [x["node"] for x in state["events"]] == ["planner", "tool_executor", "reflector"]

    asyncio.run(scenario())


def test_retry_fallback(tmp_path):
    async def scenario():
        cfg = settings(tmp_path)
        tools = ToolRegistry(cfg)
        original = tools.execute

        async def forced(tool, text):
            if tool == "web_search":
                raise ToolFailure("rate_limit", "Forced 429", True)
            return await original(tool, text)

        tools.execute = forced
        async with runtime(cfg, registry=tools) as graph:
            state = await graph.ainvoke(initial("search: exponential backoff retries"), config("failure"))
            decisions = [e["decision"] for e in state["events"]]
            assert decisions.count("retry") == 2
            assert "fallback" in decisions
            assert state["status"] == "completed"
            assert "[retries]" in state["final_answer"]

    asyncio.run(scenario())


def test_fallback_cannot_prove_current_fact(tmp_path):
    async def scenario():
        async with runtime(settings(tmp_path, tavily_api_key="")) as graph:
            state = await graph.ainvoke(initial("search: latest checkpoint changes today"), config("fresh"))
            assert state["status"] == "escalated"
            assert "cannot verify current facts" in state["final_answer"]

    asyncio.run(scenario())


def test_all_tools_fail_bounded(tmp_path):
    async def scenario():
        cfg = settings(tmp_path, max_replans=1)
        tools = ToolRegistry(cfg)

        async def fail(*args):
            raise ToolFailure("timeout", "Forced timeout", True)

        tools.execute = fail
        async with runtime(cfg, registry=tools) as graph:
            state = await graph.ainvoke(initial("search: retries"), config("all-fail"))
            assert state["status"] == "escalated"
            assert max(state["retries"].values()) == 3
            assert state["replans"] == 1
            assert len(state["events"]) < 40

    asyncio.run(scenario())


def test_pause_reopen_resume(tmp_path):
    async def scenario():
        cfg = settings(tmp_path)
        async with runtime(cfg, pause=True) as graph:
            await graph.ainvoke(initial("calculate: 9*9"), config("persist"))
            snapshot = await graph.aget_state(config("persist"))
            assert snapshot.next == ("tool_executor",)
        async with runtime(cfg) as graph:
            state = await graph.ainvoke(None, config("persist"))
            assert state["final_answer"] == "81"
            assert sum(e["node"] == "planner" for e in state["events"]) == 1

    asyncio.run(scenario())


def test_rejected_result_replans(tmp_path):
    class RejectOnce(Brain):
        async def review(self, state):
            if state["replans"] == 0:
                return Review(decision="replan", reason="Need independent verification")
            return await super().review(state)

    async def scenario():
        cfg = settings(tmp_path)
        async with runtime(cfg, brain=RejectOnce(cfg)) as graph:
            state = await graph.ainvoke(initial("calculate: 4+4"), config("replan"))
            assert state["replans"] == 1
            assert state["status"] == "completed"
            assert any(e["code"] == "rejected_result" for e in state["errors"])

    asyncio.run(scenario())


def test_multistep_continue(tmp_path):
    class TwoSteps(Brain):
        async def plan(self, state):
            return Plan(
                steps=[
                    {"tool": "calculator", "input": "2+2", "goal": "first"},
                    {"tool": "python", "input": "print(4*3)", "goal": "second"},
                ]
            )

        async def review(self, state):
            if state["step_index"] == 0:
                return Review(decision="continue", reason="First calculation verified")
            return Review(decision="finish", reason="Both results verified", answer="4 and 12")

    async def scenario():
        cfg = settings(tmp_path)
        async with runtime(cfg, brain=TwoSteps(cfg)) as graph:
            state = await graph.ainvoke(initial("Two calculations"), config("multi"))
            assert len(state["completed"]) == 2
            assert state["final_answer"] == "4 and 12"

    asyncio.run(scenario())


def test_missing_llm_escalates(tmp_path):
    async def scenario():
        cfg = Settings(llm_mode="api", llm_api_key="", data_dir=str(tmp_path))
        async with runtime(cfg) as graph:
            state = await graph.ainvoke(initial("Do some work"), config("no-key"))
            assert state["status"] == "escalated"

    asyncio.run(scenario())


def test_global_budget(tmp_path):
    async def scenario():
        cfg = settings(tmp_path, max_tool_calls=1)
        async with runtime(cfg) as graph:
            state = await graph.ainvoke(initial("calculate: 1/0"), config("budget"))
            assert state["status"] == "escalated"
            assert state["tool_calls"] == 1
            assert "budget exhausted" in state["final_answer"]

    asyncio.run(scenario())


def test_reflector_unavailable_cannot_claim_success(tmp_path):
    class BrokenReview(Brain):
        async def review(self, state):
            raise TimeoutError("Provider unavailable")

    async def scenario():
        cfg = settings(tmp_path)
        async with runtime(cfg, brain=BrokenReview(cfg)) as graph:
            state = await graph.ainvoke(initial("calculate: 2+2"), config("no-review"))
            assert state["status"] == "escalated"
            assert "Unable to verify evidence" in state["final_answer"]

    asyncio.run(scenario())
