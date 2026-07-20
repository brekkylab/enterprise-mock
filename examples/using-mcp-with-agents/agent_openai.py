#!/usr/bin/env python3
"""OpenAI agent that retrieves enterprise data over MCP. Self-contained: run it directly.

Connects a real MCP server — `--server atlassian` (default, `mcp-atlassian` in Docker) or
`--server notion` (official `notion-mcp-server` via npx) — pointed at a `--url` mock if given
(and reachable), else a local one it spins up, to the OpenAI Agents SDK. Retrieval is ACL-scoped
by the token (default: the mock's admin token; pass `--token` a per-user token to scope it).

Prereqs: OPENAI_API_KEY set; pip install -e ".[mcp]"; Docker (atlassian) or Node/npx (notion).
Run from the repo root:  python examples/using-mcp-with-agents/agent_openai.py [--server notion] [--url http://localhost:8000]
"""
from __future__ import annotations

import asyncio
import os

from agents import Agent, Runner
from agents.mcp import MCPServerStdio

from _mockserver import cli_token, serve_or_connect
from _servers import SERVER, mcp_params

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
    with serve_or_connect(CORPUS) as mock:
        # Point the chosen MCP server at the mock (atlassian → Docker, notion → npx BASE_URL).
        params = mcp_params(mock.base_url, cli_token(mock.token))
        async with MCPServerStdio(
            name=SERVER,
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
            result = await Runner.run(agent, QUESTION, max_turns=int(os.environ.get("MAX_TURNS", "20")))
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
