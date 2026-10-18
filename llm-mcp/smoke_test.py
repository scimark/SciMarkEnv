"""Смоук-тест mcp-llm по настоящему MCP-протоколу.

docker exec mcp-llm python /workspace/SciMarkEnv/llm-mcp/smoke_test.py
Задача ставится в одной MCP-сессии, а ответ забирается в другой - как у реального клиента.
"""
import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765/mcp"


@asynccontextmanager
async def session():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


async def call(s: ClientSession, tool: str, args: dict) -> str:
    result = await s.call_tool(tool, args)
    text = result.content[0].text if result.content else ""
    if result.isError:
        raise RuntimeError(f"{tool}: {text}")
    return text


async def main() -> None:
    start = time.time()
    async with session() as s:
        tools = await s.list_tools()
        print("tools:", sorted(t.name for t in tools.tools))
        print("ask:", await call(s, "ask", {"prompt": "Сколько будет 17*23? Ответь одним числом."}))
        job = json.loads(await call(s, "summarize_files_async", {
            "paths": [r"C:\Scimark\SciMarkEnv\docker-compose.yml"],
            "question": "Перечисли сервисы и их порты одной строкой.",
        }))
        print("submitted:", job)
        try:
            await call(s, "summarize_files_async", {"paths": [r"C:\Windows\*.ini"], "question": "x"})
        except RuntimeError as e:
            print("path guard ok:", str(e)[:80])

    # Новая сессия - задача должна пережить закрытие первой.
    async with session() as s:
        while True:
            res = json.loads(await call(s, "job_result", {"job_id": job["job_id"], "wait_seconds": 20}))
            print(f"poll: {res['status']} {res['elapsed_s']}s")
            if res["status"] != "running":
                print("result:", res.get("result") or res.get("error"))
                break
        print("jobs:", await call(s, "list_jobs", {}))
    print(f"time: {int(time.time() - start)}s")


asyncio.run(main())
