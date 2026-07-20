#!/usr/bin/env python3
"""OpenAI agent that retrieves enterprise data over MCP. Self-contained: run it directly.

Connects a real MCP server — `--server atlassian` (default, `mcp-atlassian` in Docker) or
`--server notion` (official `notion-mcp-server` via npx) — pointed at a `--url` mock if given
(and reachable), else a local one it spins up, to the OpenAI Agents SDK. Retrieval is ACL-scoped
by the token (default: the mock's admin token; pass `--token` a per-user token to scope it). The
backend's corpus + question live in `_servers.py`.

Prereqs: OPENAI_API_KEY set; pip install -e ".[mcp]"; Docker (atlassian) or Node/npx (notion).
Run from the repo root:
    python examples/using-mcp-with-agents/agent_openai.py [--server notion] [--url http://localhost:8000]
"""
from __future__ import annotations

import asyncio
import os

from agents import Agent, Runner
from agents.mcp import MCPServerStdio

import _servers
from _mockserver import cli_arg, cli_token, cli_username, serve_or_connect


async def main() -> None:
    backend = _servers.select(cli_arg("server"))
    with serve_or_connect(backend.corpus) as mock:
        params = backend.params(mock.base_url, cli_token(mock.token), cli_username())
        async with MCPServerStdio(
            name=backend.name,
            params={"command": params.command, "args": params.args, "env": params.env},
            client_session_timeout_seconds=30,
            cache_tools_list=True,
        ) as server:
            agent = Agent(
                name="Enterprise RAG agent",
                instructions=(
                    "You answer questions about the company using its knowledge base, reached "
                    "through the provided MCP tools. Be efficient: make at most a few tool calls "
                    "(one search, then fetch the single most relevant item), then answer. Only "
                    "use information returned by the tools; cite the titles."
                ),
                mcp_servers=[server],
                model=os.environ.get("OPENAI_MODEL", "gpt-5.5"),
            )
            result = await Runner.run(agent, backend.question,
                                      max_turns=int(os.environ.get("MAX_TURNS", "20")))
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
