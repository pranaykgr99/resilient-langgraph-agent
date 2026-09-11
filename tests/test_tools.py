import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.config import Settings
from agent.tools.core import ToolRegistry


def call(tool, text, **kwargs):
    return asyncio.run(ToolRegistry(Settings(**kwargs)).invoke(tool, text))


def test_calculator():
    assert call("calculator", "(12 + 8) / 4")["data"] == "5.0"


@pytest.mark.parametrize("value", ["1/0", '__import__("os")', "2**10000", "2**0.5j"])
def test_calculator_bad_input(value):
    assert call("calculator", value)["error"]["code"] == "bad_input"


def test_kb():
    result = call("knowledge_base", "exponential backoff retries")
    assert result["data"][0]["id"] == "retries"


def test_kb_invalid():
    assert call("knowledge_base", "")["error"]["code"] == "bad_input"


def test_python():
    assert call("python", "print(sum(i*i for i in range(10)))")["data"].strip() == "285"


@pytest.mark.parametrize(
    "code", ["open('/etc/passwd').read()", "import socket; socket.socket()", "open('/tmp/agent-escape', 'w')"]
)
def test_sandbox_denies_io(code):
    result = call("python", code)
    assert not result["ok"]
    assert result["error"]["code"] == "execution_error"


def test_python_timeout():
    assert call("python", "while True: pass", tool_timeout=0.1)["error"]["code"] == "timeout"


def test_python_output_bound():
    assert not call("python", "print('x' * 20000)")["ok"]


def test_web_missing_key():
    assert call("web_search", "test", tavily_api_key="")["error"]["code"] == "configuration"


def test_web_success(monkeypatch):
    response = httpx.Response(
        200,
        json={"results": [{"url": "https://example.org", "content": "Evidence"}]},
        request=httpx.Request("POST", "https://api.tavily.com/search"),
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=response))
    assert call("web_search", "test", tavily_api_key="test")["data"][0]["text"] == "Evidence"


@pytest.mark.parametrize("status,code", [(429, "rate_limit"), (503, "api_error"), (401, "api_error")])
def test_web_http_failures(monkeypatch, status, code):
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.tavily.com/search"))
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=response))
    result = call("web_search", "test", tavily_api_key="test")
    assert result["error"]["code"] == code
    assert result["error"]["retryable"] == (status != 401)


def test_web_timeout(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(side_effect=httpx.ReadTimeout("forced")))
    assert call("web_search", "test", tavily_api_key="test")["error"]["code"] == "timeout"
