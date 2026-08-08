"""Manual end-to-end check: spawn the server over stdio and list its tools.

Run with ``uv run python tests/smoke_stdio.py``. Deliberately not part of the
pytest suite -- it spawns a subprocess. It uses bogus credentials because
listing tools must not require a login.
"""

import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nemlig_mcp.server"],
        env={**os.environ, "NEMLIG_USER": "smoke@example.com", "NEMLIG_PASS": "not-a-real-password"},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected to {init.server_info.name} v{init.server_info.version}")

            tools = (await session.list_tools()).tools
            print(f"\n{len(tools)} tools:")
            for tool in sorted(tools, key=lambda t: t.name):
                read_only = bool(tool.annotations and tool.annotations.read_only_hint)
                marker = "read " if read_only else "WRITE"
                summary = (tool.description or "").strip().splitlines()[0]
                print(f"  [{marker}] {tool.name:22} {summary}")

            # A tool call with bad credentials must fail cleanly, not hang or
            # corrupt the stream.
            result = await session.call_tool("search_products", {"query": "cocio", "limit": 1})
            status = "error (expected: bogus credentials)" if result.is_error else "success"
            print(f"\ncall_tool(search_products) -> {status}")

            # The session survived a failing call, so the transport is intact.
            assert (await session.list_tools()).tools, "server died after tool error"
            print("transport still healthy after error")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
