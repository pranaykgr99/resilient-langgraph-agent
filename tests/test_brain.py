import asyncio
import json

import httpx

from agent.brain import Brain
from agent.config import Settings
from agent.graph import initial


def test_real_llm_client_structured_plan_with_mock_http(monkeypatch):
    """Exercise the actual provider client/parser; no claim of a live LLM call."""

    async def send(self, request, **kwargs):
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["response_format"]["type"] == "json_schema"
        content = json.dumps({"steps": [{"tool": "calculator", "input": "6*7", "goal": "Compute"}]})
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    brain = Brain(Settings(llm_mode="api", llm_api_key="test-key", llm_model="test-model"))
    result = asyncio.run(brain.plan(initial("What is six times seven?")))
    assert result.steps[0].input == "6*7"
