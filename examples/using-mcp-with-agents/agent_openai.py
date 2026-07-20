#!/usr/bin/env python3
"""OpenAI agent that retrieves enterprise data over MCP. Self-contained: run it directly.

Connects the community-official Atlassian MCP server (`mcp-atlassian`, in Docker) — pointed at
a `--url` mock if given (and reachable), else a local one it spins up — to the OpenAI Agents
SDK. Retrieval is ACL-scoped by MOCK_MCP_TOKEN (default: the mock's admin token).

Prereqs: Docker available; OPENAI_API_KEY set; pip install -e ".[mcp]".
Run from the repo root:  python examples/using-mcp-with-agents/agent_openai.py [--url http://localhost:8000]
"""
from __future__ import annotations

import asyncio
import os

import atlassian_server
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

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
    with serve_or_connect(CORPUS) as mock:
        # Point the MCP server at the mock: local (host-gateway) or a remote --url deployment
        # (aliased + SSL-verify off). A remote --url additionally requires --token and --username.
        atlassian_server.configure(mock.base_url, mock.token)
        async with MCPServerStdio(
            name="atlassian",
            params={"command": "docker", "args": atlassian_server.docker_args()},
            client_session_timeout_seconds=30,
            cache_tools_list=True,
        ) as server:
            agent = Agent(
                name="Enterprise RAG agent",
                instructions=(
                    "You answer questions about the company using its Jira and Confluence, "
                    "reached through the provided MCP tools. Be efficient: make at most a few "
                    "tool calls (one search, then fetch the single most relevant item), then "
                    "answer. Only use information returned by the tools; cite issue keys / page titles."
                ),
                mcp_servers=[server],
                model=os.environ.get("OPENAI_MODEL", "gpt-5.5"),
            )
            result = await Runner.run(agent, QUESTION, max_turns=int(os.environ.get("MAX_TURNS", "20")))
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
