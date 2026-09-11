import ast
import asyncio
import json
import math
import operator
import sys
from pathlib import Path

import httpx
from agent.tools.vector_store import LocalVectorStore


class ToolFailure(Exception):
    def __init__(self, code, message, retryable=False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


def calculate(expression):
    if len(expression) > 300:
        raise ValueError("Expression too long")
    tree = ast.parse(expression, mode="eval")
    ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in ops:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent exceeds limit")
            value = ops[type(node.op)](left, right)
        else:
            raise ValueError("Only numeric arithmetic is allowed")
        if not isinstance(value, (int, float)) or abs(value) > 1e100 or not math.isfinite(value):
            raise ValueError("Numeric range exceeded")
        return value

    return str(visit(tree.body))


class ToolRegistry:
    def __init__(self, settings):
        self.settings = settings
        self.store = LocalVectorStore(settings.data_dir)

    async def invoke_dataset(self, tool, text, dataset_id):
        from agent.datasets import DataError, run_operation
        try:
            options = json.loads(text)
            if not isinstance(options, dict) or set(options) - {"metric", "group_by", "date_column"}:
                raise DataError("bad_input", "Dataset tools accept only metric, group_by and date_column.")
            result = await run_operation(self.settings.data_dir, dataset_id, tool, options,
                                         timeout=self.settings.tool_timeout)
            return {**result, "tool": tool}
        except Exception as exc:
            return {"ok": False, "tool": tool, "error": {
                "code": getattr(exc, "code", "bad_input"), "message": str(exc)[:350], "retryable": False}}

    async def invoke(self, tool, text):
        try:
            async with asyncio.timeout(self.settings.tool_timeout):
                if not isinstance(text, str) or not text.strip() or len(text) > 6000:
                    raise ToolFailure("bad_input", "Expected 1–6000 characters")
                data = await self.execute(tool, text)
                return {"ok": True, "data": data, "tool": tool}
        except TimeoutError:
            error = ToolFailure("timeout", "Tool deadline exceeded", True)
        except ToolFailure as exc:
            error = exc
        except httpx.TimeoutException:
            error = ToolFailure("timeout", "HTTP deadline exceeded", True)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error = ToolFailure(
                "rate_limit" if status == 429 else "api_error",
                f"HTTP {status}",
                status == 429 or status >= 500,
            )
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            error = ToolFailure("bad_input", str(exc)[:300])
        except Exception as exc:
            error = ToolFailure("tool_error", type(exc).__name__)
        return {
            "ok": False,
            "tool": tool,
            "error": {"code": error.code, "message": error.message, "retryable": error.retryable},
        }

    async def execute(self, tool, text):
        if tool == "calculator":
            return calculate(text)
        if tool == "knowledge_base":
            return self.store.search(text)
        if tool == "web_search":
            if not self.settings.tavily_api_key:
                raise ToolFailure("configuration", "TAVILY_API_KEY is missing")
            async with httpx.AsyncClient(timeout=self.settings.tool_timeout) as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": "Bearer " + self.settings.tavily_api_key},
                    json={
                        "query": text,
                        "max_results": 3,
                        "include_answer": False,
                    },
                )
                response.raise_for_status()
                return [{"url": r["url"], "text": r["content"][:2500]} for r in response.json()["results"]]
        if tool == "python":
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).with_name("sandbox.py")),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={},
            )
            try:
                process.stdin.write(text.encode())
                await process.stdin.drain()
                process.stdin.close()
                output = await process.stdout.read(16385)
                if len(output) > 16384:
                    raise ToolFailure("output_limit", "Output exceeds 16 KiB")
                await process.wait()
                if process.returncode:
                    raise ToolFailure(
                        "execution_error",
                        output.decode(errors="replace")[:500] or f"Sandbox exited {process.returncode}",
                    )
                return output.decode(errors="replace")
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
        raise ToolFailure("bad_input", "Unknown tool")
