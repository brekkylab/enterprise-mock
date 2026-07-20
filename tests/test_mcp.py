"""ACL enforced end-to-end through the real mcp-atlassian MCP server.

Uses the ``live_server`` fixture (a real ``uvicorn`` on the conftest SAMPLE corpus) and drives
the community-official ``mcp-atlassian`` server (in Docker) against it: a Jira issue the admin
can read is blocked for an ACL-restricted user — same tool, same issue, different identity.
Skipped unless the ``mcp`` package is installed and Docker is available.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest
import yaml

pytest.importorskip("mcp")


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker not available")

from app import store, synth  # noqa: E402
from app.acl import Acl  # noqa: E402


def _docker_args(port: int, token: str) -> list[str]:
    """`docker run` args pointing mcp-atlassian at the mock (see examples/using-mcp-with-agents/atlassian_server.py).

    mcp-atlassian only classifies a host as Atlassian *Cloud* when it ends in `.atlassian.net`,
    so we use a fake `mock.atlassian.net` mapped to the host, and Basic auth where the api_token
    is a mock token (which the mock resolves to a user and enforces that user's ACL).
    """
    host, base = "mock.atlassian.net", f"http://mock.atlassian.net:{port}"
    return [
        "run", "-i", "--rm", f"--add-host={host}:host-gateway",
        "-e", f"JIRA_URL={base}/atlassian", "-e", "JIRA_USERNAME=svc@example.com",
        "-e", f"JIRA_API_TOKEN={token}",
        "-e", f"CONFLUENCE_URL={base}/atlassian/wiki", "-e", "CONFLUENCE_USERNAME=svc@example.com",
        "-e", f"CONFLUENCE_API_TOKEN={token}",
        "-e", "MCP_ALLOWED_URL_DOMAINS=atlassian.net", "-e", "READ_ONLY_MODE=true",
        "ghcr.io/sooperset/mcp-atlassian:latest", "--transport", "stdio",
    ]


def _pick_restricted_issue(settings, user_token: str) -> tuple[str | None, str]:
    """A Jira issue the admin can read but this user cannot (per the mock's own ACL)."""
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    caller = acl.resolve(user_token)
    vids = acl.visible_ids(conn, caller)
    for row in conn.execute("SELECT doc_id, project AS container FROM jira_issues"):
        if store.get_document(conn, "jira", row["doc_id"], visible_ids=vids) is None:
            return synth.jira_key(row["doc_id"], synth.jira_project_key(row["container"])), caller.email
    return None, caller.email


async def _reads_issue(token: str, key: str, port: int) -> bool:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command="docker", args=_docker_args(port, token))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            res = await sess.call_tool("jira_get_issue", {"issue_key": key})
            text = res.content[0].text
            return text.strip().startswith("{") and '"key"' in text


def test_mcp_acl_enforced(live_server):
    base, settings = live_server
    port = int(base.rsplit(":", 1)[1])  # Docker reaches the host mock via host-gateway
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    key, email = _pick_restricted_issue(settings, user["token"])
    assert key, f"no Jira issue is ACL-restricted from {email} in the sample corpus"

    admin_reads = asyncio.run(_reads_issue(settings.admin_token, key, port))
    user_reads = asyncio.run(_reads_issue(user["token"], key, port))

    assert admin_reads, "admin token should read the issue through the MCP server"
    assert not user_reads, f"{email} should be blocked from the issue through the MCP server"
