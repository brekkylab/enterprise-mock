#!/usr/bin/env python3
"""Claude agent that retrieves enterprise data over MCP. Self-contained: run it directly.

Connects the community-official Atlassian MCP server (`mcp-atlassian`, in Docker) — pointed at
a `--url` mock if given (and reachable), else a local one it spins up — then lets Claude answer
a question by calling the MCP tools. Retrieval is ACL-scoped by MOCK_MCP_TOKEN (default: the
mock's admin token; set a per-user token to scope it).

Prereqs: Docker available; ANTHROPIC_API_KEY set; pip install -e ".[mcp]".
Run from the repo root:  python examples/using-mcp-with-agents/agent_anthropic.py [--url http://localhost:8000]
"""
from __future__ import annotations

import asyncio
import os

import atlassian_server
from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from _mockserver import serve_or_connect

CORPUS = [
    {"source_type": "jira", "project": "payments", "title": "SEV2: checkout latency spike",
     "content": "p95 checkout latency jumped to 2.1s after the payments migration; rolling back.",
     "status": "In Progress", "issuetype": "Incident", "priority": "High"},
    {"source_type": "confluence", "space": "runbooks",
     "title": "On-call Runbook: checkout latency & bad deploys",
     "content": "When a deploy or migration spikes checkout latency: check the payments "
                "dashboards, roll back the last change, and page the on-call engineer."},
]

QUESTION = os.environ.get(
    "Q", "Find the Jira incident about checkout latency and summarize it, then find the "
         "on-call runbook in Confluence. Cite the issue key and the page title."
)


async def main() -> None:
    client = AsyncAnthropic()
    with serve_or_connect(CORPUS) as mock:
        # Point the MCP server at the mock: local (host-gateway) or a remote --url deployment
        # (aliased + SSL-verify off). A remote --url additionally requires --token and --username.
        atlassian_server.configure(mock.base_url, mock.token)
        params = StdioServerParameters(command="docker", args=atlassian_server.docker_args())
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
