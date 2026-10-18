"""Смоук-тест mcp-llm по настоящему MCP-протоколу.

docker exec mcp-llm python /workspace/SciMarkEnv/llm-mcp/smoke_test.py
"""
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765/mcp"


async def main() -> None:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            result = await session.call_tool("llm_health", {})
            print("error:" if result.isError else "ok:", result.content[0].text)
            result = await session.call_tool("ask", {"prompt": "Сколько будет 17*23? Ответь одним числом."})
            print("ask error:" if result.isError else "ask:", result.content[0].text)


asyncio.run(main())
