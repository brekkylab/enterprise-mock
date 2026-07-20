"""MCP backend registry — one place defining every MCP server the agent examples can drive.

Each :class:`Backend` bundles everything an agent needs: a self-contained ``corpus`` to seed the
mock, a demo ``question``, and ``params(base_url, token, username)`` returning the
``StdioServerParameters`` that point the real MCP server at the mock. Pick one with
``--server <name>`` (see :func:`select`).

Two backends ship:

- **atlassian** — the community-official `mcp-atlassian` (Jira + Confluence), run in **Docker**.
  It only classifies a host as Atlassian *Cloud* (the v3 + `/wiki` shape the mock speaks) when the
  hostname ends in `.atlassian.net`, so we always use a fake `mock.atlassian.net` mapped with
  Docker's `--add-host` — to the host machine (`host-gateway`) for a local mock, or to a remote
  deployment's resolved IP. Auth is HTTP Basic where the **api_token is a mock token** (the mock
  resolves it to a user and enforces that user's ACL); the **username** is required by
  mcp-atlassian but ignored by the mock once the token resolves.
- **notion** — the **official** `@notionhq/notion-mcp-server`, run via **npx** (Node). It takes a
  first-class `BASE_URL` override, so pointing it at the mock is one env var and a local
  `localhost` mock is reached directly (no Docker/host-gateway tricks).
"""
from __future__ import annotations

import socket
import sys
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

# Ordinary users referenced by the corpora (kept @acme.com so the derived org is "acme").
_ATLASSIAN_CORPUS = [
    {"source_type": "jira", "project": "payments", "title": "SEV2: checkout latency spike",
     "content": "p95 checkout latency jumped to 2.1s after the payments migration; rolling back.",
     "status": "In Progress", "issuetype": "Incident", "priority": "High"},
    {"source_type": "confluence", "space": "runbooks",
     "title": "On-call Runbook: checkout latency & bad deploys",
     "content": "When a deploy or migration spikes checkout latency: check the payments "
                "dashboards, roll back the last change, and page the on-call engineer."},
]
_NOTION_CORPUS = [
    {"source_type": "notion", "teamspace": "payments", "title": "SEV2: checkout latency spike",
     "content": "# SEV2\n\np95 checkout latency jumped to 2.1s after the payments migration; "
                "rolling back."},
    {"source_type": "notion", "teamspace": "runbooks",
     "title": "On-call Runbook: checkout latency & bad deploys",
     "content": "# On-call\n\nWhen a deploy or migration spikes checkout latency: check the "
                "payments dashboards, roll back the last change, and page the on-call engineer."},
]
_QUESTION = ("Find the incident about checkout latency and summarize it, then find the on-call "
             "runbook. Cite the titles.")

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0")


def _is_local(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "127.0.0.1") in _LOCAL_HOSTS


def _atlassian_params(base_url: str, token: str, username: str | None):
    """`docker run` args pointing mcp-atlassian at the mock (Cloud shape via mock.atlassian.net)."""
    from mcp import StdioServerParameters

    u = urlparse(base_url)
    host = "mock.atlassian.net"  # must end in .atlassian.net for Cloud detection
    if _is_local(base_url):
        scheme, port, addhost, ssl_verify = "http", (u.port or 80), "host-gateway", True
        user = username or "svc@example.com"  # placeholder; the mock ignores it once token resolves
    else:
        # remote deployment: alias the fake host to its IP, and require an explicit identity
        if not username:
            sys.exit(f"--url points at a remote deployment ({u.hostname}); also pass --username "
                     "(and --token) — mcp-atlassian needs a Basic-auth username for Cloud detection "
                     "and the token authenticates + scopes ACL (get one from GET /_mock/users).")
        scheme = u.scheme
        port = u.port or (443 if u.scheme == "https" else 80)
        addhost = socket.gethostbyname(u.hostname)
        ssl_verify = False  # cert is for the real host, not mock.atlassian.net
        user = username
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    base = f"{scheme}://{host}" if default_port else f"{scheme}://{host}:{port}"
    args = [
        "run", "-i", "--rm", f"--add-host={host}:{addhost}",
        "-e", f"JIRA_URL={base}/atlassian", "-e", f"JIRA_USERNAME={user}",
        "-e", f"JIRA_API_TOKEN={token}",
        "-e", f"CONFLUENCE_URL={base}/atlassian/wiki", "-e", f"CONFLUENCE_USERNAME={user}",
        "-e", f"CONFLUENCE_API_TOKEN={token}",
        "-e", "MCP_ALLOWED_URL_DOMAINS=atlassian.net", "-e", "READ_ONLY_MODE=true",
    ]
    if not ssl_verify:
        args += ["-e", "JIRA_SSL_VERIFY=false", "-e", "CONFLUENCE_SSL_VERIFY=false"]
    args += ["ghcr.io/sooperset/mcp-atlassian:latest", "--transport", "stdio"]
    return StdioServerParameters(command="docker", args=args)


def _notion_params(base_url: str, token: str, username: str | None):
    """`npx` args pointing the official notion-mcp-server at the mock via BASE_URL."""
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command="npx", args=["-y", "@notionhq/notion-mcp-server"],
        env={"BASE_URL": f"{base_url.rstrip('/')}/notion", "NOTION_TOKEN": token,
             "NOTION_VERSION": "2025-09-03"})


@dataclass(frozen=True)
class Backend:
    name: str
    corpus: list[dict]
    question: str
    _build: Callable[[str, str, str | None], object]

    def params(self, base_url: str, token: str, username: str | None = None):
        """The StdioServerParameters that point this MCP server at ``base_url``."""
        return self._build(base_url, token, username)


BACKENDS = {
    "atlassian": Backend("atlassian", _ATLASSIAN_CORPUS, _QUESTION, _atlassian_params),
    "notion": Backend("notion", _NOTION_CORPUS, _QUESTION, _notion_params),
}


def select(name: str | None) -> Backend:
    """Resolve a ``--server`` value to a Backend (default: atlassian)."""
    name = (name or "atlassian").lower()
    if name not in BACKENDS:
        sys.exit(f"--server must be one of {sorted(BACKENDS)}, got {name!r}")
    return BACKENDS[name]
