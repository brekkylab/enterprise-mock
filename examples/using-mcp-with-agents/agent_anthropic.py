#!/usr/bin/env python3
"""Claude agent that retrieves enterprise data over MCP. Self-contained: run it directly.

Connects a real MCP server — `--server atlassian` (`mcp-atlassian` in Docker) or `--server notion`
(official `notion-mcp-server` via npx) — pointed at a `--url` mock if given (and reachable), else a
local one it spins up, then lets Claude answer a question by calling the MCP tools. `--server` is
required. Retrieval is ACL-scoped by the token (default: the mock's admin token; pass `--token` a
per-user token from GET /_mock/users to scope it). The backend's corpus + question live in
`_servers.py`.

Prereqs: ANTHROPIC_API_KEY set; pip install -e ".[mcp]"; Docker (atlassian) or Node/npx (notion).
Run from the repo root:
    python examples/using-mcp-with-agents/agent_anthropic.py --server notion [--url http://localhost:8000]
"""
from __future__ import annotations

import asyncio

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.stdio import stdio_client

import _servers
from _mockserver import cli_arg, cli_token, cli_username, serve_or_connect


async def main() -> None:
    backend = _servers.select(cli_arg("server"))
    client = AsyncAnthropic()
    with serve_or_connect(backend.corpus) as mock:
        params = backend.params(mock.base_url, cli_token(mock.token), cli_username())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as mcp_client:
                await mcp_client.initialize()
                tools = await mcp_client.list_tools()

                runner = client.beta.messages.tool_runner(
                    model="claude-sonnet-4-6",
                    max_tokens=16000,
                    thinking={"type": "adaptive"},
                    messages=[{"role": "user", "content": backend.question}],
                    tools=[async_mcp_tool(t, mcp_client) for t in tools.tools],
                )
                async for message in runner:
                    for block in message.content:
                        if block.type == "text":
                            print(block.text, end="", flush=True)
                print()


if __name__ == "__main__":
    asyncio.run(main())
