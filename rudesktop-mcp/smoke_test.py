"""Смоук через настоящий MCP-клиент: python smoke_test.py <id> [mode]

Поднимает сервер по stdio, как это делает Claude, и гоняет connect/run/read/close.
Пароль и подтверждения вводятся в окошках.
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
if len(sys.argv) < 2:
    sys.exit("usage: python smoke_test.py <ID> [confirm|smart|auto]")
TARGET = sys.argv[1]
MODE = sys.argv[2] if len(sys.argv) > 2 else "smart"


async def call(s: ClientSession, tool: str, **args):
    res = await s.call_tool(tool, args)
    texts = [c.text for c in res.content if hasattr(c, "text")]
    try:
        data = json.loads(texts[0]) if len(texts) == 1 else [json.loads(t) for t in texts]
    except (ValueError, IndexError):
        data = {"raw": texts, "is_error": res.isError}
    print(f"\n>>> {tool} {json.dumps(args, ensure_ascii=False)}")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


async def main():
    params = StdioServerParameters(
        command=str(HERE / ".venv" / "Scripts" / "python.exe"),
        args=["-m", "rudesktop_mcp.server"],
        cwd=str(HERE),
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        print("tools:", [t.name for t in (await s.list_tools()).tools])
        c = await call(s, "connect", target_id=TARGET, mode=MODE)
        if not c.get("connected"):
            return
        sid = c["sid"]
        await call(s, "run", sid=sid, command="hostname; whoami; Get-Date")      # read-only
        await call(s, "run", sid=sid, command="New-Item $env:TEMP\\rdmcp_smoke -ItemType Directory -Force | Select FullName")  # запись
        await call(s, "run", sid=sid, command="Start-Sleep 5; 'slept'", timeout=2)
        await call(s, "read", sid=sid, timeout=10)
        await call(s, "list_sessions")
        await call(s, "close", sid=sid)


asyncio.run(main())
