"""ACL enforced end-to-end through real MCP servers pointed at the mock.

Uses the ``live_server`` fixture (a real ``uvicorn`` on the conftest SAMPLE corpus) and drives the
same MCP-server wiring the examples ship (``examples/using-mcp-with-agents/_servers.py``): a
document the admin can read is blocked for an ACL-restricted user — same tool, same object,
different identity.

- **Atlassian** (`mcp-atlassian`, Docker) — skipped unless Docker is available.
- **Notion** (`@notionhq/notion-mcp-server`, npx) — skipped unless ``npx`` (Node) is on PATH; the
  first run downloads the npm package.

Both require the ``mcp`` package.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("mcp")

# The examples' backend registry is the single source of the MCP-server wiring under test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples" / "using-mcp-with-agents"))
import _servers  # noqa: E402

from app import store, synth  # noqa: E402
from app.acl import Acl  # noqa: E402


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _restricted_doc(settings, user_token: str, source: str, where: str = "1=1"):
    """A doc of ``source`` the admin can read but this user cannot (per the mock's own ACL)."""
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    caller = acl.resolve(user_token)
    vids = acl.visible_ids(conn, caller)
    tbl = store.table(source)
    for row in conn.execute(f"SELECT * FROM {tbl} WHERE {where}"):
        if store.get_document(conn, source, row["doc_id"], visible_ids=vids) is None:
            return row, caller.email
    return None, caller.email


async def _call(backend, token, base, tool_pred, args_fn, ok_pred) -> bool:
    """Connect ``backend`` at ``base`` with ``token``, call the tool matched by ``tool_pred`` with
    ``args_fn(tools)``, and return ``ok_pred(text)`` over the response text."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(backend.params(base, token, "svc@example.com")) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = (await sess.list_tools()).tools
            tool = next(t for t in tools if tool_pred(t.name))
            res = await sess.call_tool(tool.name, args_fn(tools))
            text = "".join(getattr(c, "text", "") for c in res.content)
            return ok_pred(text)


# --------------------------------------------------------------------------- Atlassian

@pytest.mark.skipif(not _docker_available(), reason="Docker not available")
def test_mcp_atlassian_acl_enforced(live_server):
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "jira")
    assert row is not None, f"no Jira issue is ACL-restricted from {email} in the sample corpus"
    key = synth.jira_key(row["doc_id"], synth.jira_project_key(row["project"]))
    backend = _servers.BACKENDS["atlassian"]

    def reads(token):
        return asyncio.run(_call(
            backend, token, base,
            tool_pred=lambda n: n == "jira_get_issue",
            args_fn=lambda _tools: {"issue_key": key},
            ok_pred=lambda t: t.strip().startswith("{") and '"key"' in t))

    assert reads(settings.admin_token), "admin should read the issue through mcp-atlassian"
    assert not reads(user["token"]), f"{email} should be blocked from the issue via mcp-atlassian"


# --------------------------------------------------------------------------- Notion

@pytest.mark.skipif(shutil.which("npx") is None, reason="npx (Node) not available")
def test_mcp_notion_acl_enforced(live_server):
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    row, email = _restricted_doc(settings, user["token"], "notion", "subtype IS NOT 'database'")
    assert row is not None, f"no Notion page is ACL-restricted from {email} in the sample corpus"
    page_id = synth.notion_id(row["doc_id"])
    backend = _servers.BACKENDS["notion"]

    def reads(token):
        return asyncio.run(_call(
            backend, token, base,
            # the proxy names tools from OpenAPI operationIds (e.g. "API-retrieve-a-page")
            tool_pred=lambda n: "retrieve-a-page" in n or ("page" in n and "retrieve" in n),
            args_fn=lambda _tools: {"page_id": page_id},
            ok_pred=lambda t: '"object": "page"' in t or '"object":"page"' in t))

    assert reads(settings.admin_token), "admin should read the page through notion-mcp-server"
    assert not reads(user["token"]), f"{email} should be blocked from the page via notion-mcp-server"
