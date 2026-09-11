import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from agent.state import AgentState


def initial(task):
    return dict(
        task=task,
        plan=[],
        completed=[],
        step_index=0,
        retries={},
        errors=[],
        final_answer="",
        tool_calls=0,
        replans=0,
        fallback_used=False,
        status="running",
        events=[],
    )


def build_graph(settings, saver, registry, brain, pause=False):
    log_dir = Path(settings.data_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    def emit(state, config, node, decision, reason, **updates):
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "thread_id": config["configurable"]["thread_id"],
            "node": node,
            "decision": decision,
            "reason": reason,
            "phase": "decision",
        }
        with (log_dir / "transitions.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        return dict(updates, decision=decision, reason=reason, events=state["events"] + [event])

    async def planner(s, config):
        try:
            async with asyncio.timeout(settings.llm_timeout):
                plan = await brain.plan(s)
            return emit(
                s,
                config,
                "planner",
                "execute",
                f"Planned {len(plan.steps)} step(s)",
                plan=[x.model_dump() for x in plan.steps],
                step_index=0,
                fallback_used=False,
            )
        except Exception as exc:
            return emit(
                s,
                config,
                "planner",
                "escalate",
                f"Planner unavailable: {type(exc).__name__}",
                errors=s["errors"] + [{"code": "planner_error", "message": type(exc).__name__}],
            )

    async def tool_executor(s, config):
        if s["tool_calls"] >= settings.max_tool_calls:
            return emit(
                s,
                config,
                "tool_executor",
                "error",
                "Total tool-call budget exhausted",
                result={
                    "ok": False,
                    "error": {
                        "code": "budget",
                        "message": "Total tool-call budget exhausted",
                        "retryable": False,
                    },
                },
            )
        step = s["plan"][s["step_index"]]
        key = f"{s['replans']}:{s['step_index']}:{step['tool']}"
        count = s["retries"].get(key, 0) + 1
        if count > 1:
            await asyncio.sleep(min(settings.backoff_cap, settings.backoff_base * 2 ** (count - 2)))
        result = await registry.invoke(step["tool"], step["input"])
        error = [] if result["ok"] else [dict(result["error"], tool=step["tool"], attempt=count)]
        return emit(
            s,
            config,
            "tool_executor",
            "reflect" if result["ok"] else "error",
            f"{step['tool']} attempt {count}: " + ("ok" if result["ok"] else result["error"]["code"]),
            tool_calls=s["tool_calls"] + 1,
            result=result,
            retries={**s["retries"], key: count},
            errors=s["errors"] + error,
        )

    async def reflector(s, config):
        try:
            async with asyncio.timeout(settings.llm_timeout):
                review = await brain.review(s)
        except Exception as exc:
            return emit(
                s, config, "reflector", "escalate", f"Unable to verify evidence: {type(exc).__name__}"
            )
        if review.decision == "finish" and review.answer.strip():
            return emit(
                s,
                config,
                "reflector",
                "finish",
                review.reason,
                final_answer=review.answer,
                status="completed",
                completed=s["completed"] + [{"step": s["plan"][s["step_index"]], "result": s["result"]}],
            )
        if review.decision == "continue" and s["step_index"] + 1 < len(s["plan"]):
            return emit(
                s,
                config,
                "reflector",
                "continue",
                review.reason,
                step_index=s["step_index"] + 1,
                fallback_used=False,
                completed=s["completed"] + [{"step": s["plan"][s["step_index"]], "result": s["result"]}],
            )
        if review.decision == "escalate":
            return emit(s, config, "reflector", "escalate", review.reason)
        return emit(
            s,
            config,
            "reflector",
            "error",
            review.reason,
            result={
                "ok": False,
                "error": {
                    "code": "rejected_result",
                    "message": review.reason,
                    "retryable": review.decision == "retry",
                },
            },
            errors=s["errors"] + [{"code": "rejected_result", "message": review.reason}],
            completed=s["completed"]
            + [
                {
                    "step": s["plan"][s["step_index"]],
                    "result": s["result"],
                    "accepted": review.accepted,
                    "review": review.reason,
                }
            ],
        )

    async def error_handler(s, config):
        if s["tool_calls"] >= settings.max_tool_calls:
            return emit(s, config, "error_handler", "escalate", "Total tool-call budget exhausted")
        step = s["plan"][s["step_index"]]
        key = f"{s['replans']}:{s['step_index']}:{step['tool']}"
        if s["result"]["error"]["retryable"] and s["retries"].get(key, 0) < settings.max_attempts:
            return emit(s, config, "error_handler", "retry", "Transient failure; bounded exponential backoff")
        if step["tool"] == "web_search" and not s["fallback_used"]:
            plan = [dict(x) for x in s["plan"]]
            plan[s["step_index"]]["tool"] = "knowledge_base"
            return emit(
                s,
                config,
                "error_handler",
                "fallback",
                "Search unavailable; try local evidence",
                plan=plan,
                fallback_used=True,
            )
        if s["replans"] < settings.max_replans:
            return emit(
                s,
                config,
                "error_handler",
                "replan",
                "Revise remaining work using failure history",
                replans=s["replans"] + 1,
            )
        return emit(s, config, "error_handler", "escalate", "Recovery budget exhausted")

    async def human_escalation(s, config):
        attempts = "; ".join(f"{e.get('tool', 'verification')}: {e['code']}" for e in s["errors"])
        answer = f"Could not complete the task. {s['reason']}. Tried: {attempts or 'planning/verification'}. "
        answer += "Provide a reliable source, correct the input, or restore the configured service and start a new task."
        return emit(
            s, config, "human_escalation", "end", s["reason"], final_answer=answer, status="escalated"
        )

    graph = StateGraph(AgentState)
    for name, fn in [
        ("planner", planner),
        ("tool_executor", tool_executor),
        ("reflector", reflector),
        ("error_handler", error_handler),
        ("human_escalation", human_escalation),
    ]:

        def traced(node_name, function):
            async def node(state, config):
                with (log_dir / "transitions.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "time": datetime.now(timezone.utc).isoformat(),
                                "thread_id": config["configurable"]["thread_id"],
                                "node": node_name,
                                "phase": "entered",
                            }
                        )
                        + "\n"
                    )
                return await function(state, config)

            return node

        graph.add_node(name, traced(name, fn))
    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner", lambda s: s["decision"], {"execute": "tool_executor", "escalate": "human_escalation"}
    )
    graph.add_conditional_edges(
        "tool_executor", lambda s: s["decision"], {"reflect": "reflector", "error": "error_handler"}
    )
    graph.add_conditional_edges(
        "reflector",
        lambda s: s["decision"],
        {
            "continue": "tool_executor",
            "error": "error_handler",
            "finish": END,
            "escalate": "human_escalation",
        },
    )
    graph.add_conditional_edges(
        "error_handler",
        lambda s: s["decision"],
        {
            "retry": "tool_executor",
            "fallback": "tool_executor",
            "replan": "planner",
            "escalate": "human_escalation",
        },
    )
    graph.add_edge("human_escalation", END)
    return graph.compile(checkpointer=saver, interrupt_before=["tool_executor"] if pause else [])
