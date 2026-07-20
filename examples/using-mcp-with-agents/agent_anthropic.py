#!/usr/bin/env python3
"""Claude agent that retrieves enterprise data over MCP. Self-contained: run it directly.

Connects a real MCP server — `--server atlassian` (default, `mcp-atlassian` in Docker) or
`--server notion` (official `notion-mcp-server` via npx) — pointed at a `--url` mock if given
(and reachable), else a local one it spins up, then lets Claude answer a question by calling the
MCP tools. Retrieval is ACL-scoped by the token (default: the mock's admin token; pass `--token`
a per-user token from GET /_mock/users to scope it).

Prereqs: ANTHROPIC_API_KEY set; pip install -e ".[mcp]"; Docker (atlassian) or Node/npx (notion).
Run from the repo root:  python examples/using-mcp-with-agents/agent_anthropic.py [--server notion] [--url http://localhost:8000]
"""
from __future__ import annotations

import asyncio
import os

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from _mockserver import cli_token, serve_or_connect
from _servers import SERVER, mcp_params

# Two ready-made corpora + questions; `--server atlassian` (default, Docker) or `--server notion`
# (official notion-mcp-server via npx) picks which MCP server + corpus to use.
CORPORA = {
    "atlassian": [
        {"source_type": "jira", "project": "payments", "title": "SEV2: checkout latency spike",
         "content": "p95 checkout latency jumped to 2.1s after the payments migration; rolling back.",
         "status": "In Progress", "issuetype": "Incident", "priority": "High"},
        {"source_type": "confluence", "space": "runbooks",
         "title": "On-call Runbook: checkout latency & bad deploys",
         "content": "When a deploy or migration spikes checkout latency: check the payments "
                    "dashboards, roll back the last change, and page the on-call engineer."},
    ],
    "notion": [
        {"source_type": "notion", "teamspace": "payments", "title": "SEV2: checkout latency spike",
         "content": "# SEV2\n\np95 checkout latency jumped to 2.1s after the payments migration; "
                    "rolling back."},
        {"source_type": "notion", "teamspace": "runbooks",
         "title": "On-call Runbook: checkout latency & bad deploys",
         "content": "# On-call\n\nWhen a deploy or migration spikes checkout latency: check the "
                    "payments dashboards, roll back the last change, and page the on-call engineer."},
    ],
}
CORPUS = CORPORA[SERVER]

QUESTION = os.environ.get(
    "Q", "Find the incident about checkout latency and summarize it, then find the on-call "
         "runbook. Cite the titles."
)


async def main() -> None:
    client = AsyncAnthropic()
    with serve_or_connect(CORPUS) as mock:
        # Point the chosen MCP server at the mock. Atlassian → Docker (local host-gateway or a
        # remote --url, aliased + SSL-verify off; remote also needs --token/--username). Notion →
        # npx notion-mcp-server with BASE_URL (reaches a local mock directly).
        params = mcp_params(mock.base_url, cli_token(mock.token))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as mcp_client:
                await mcp_client.initialize()
                tools = await mcp_client.list_tools()

                runner = client.beta.messages.tool_runner(
                    model="claude-sonnet-4-6",
                    max_tokens=16000,
                    thinking={"type": "adaptive"},
                    messages=[{"role": "user", "content": QUESTION}],
                    tools=[async_mcp_tool(t, mcp_client) for t in tools.tools],
                )
                async for message in runner:
                    for block in message.content:
                        if block.type == "text":
                            print(block.text, end="", flush=True)
                print()


if __name__ == "__main__":
    asyncio.run(main())
