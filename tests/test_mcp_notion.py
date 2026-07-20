"""ACL enforced end-to-end through the official `@notionhq/notion-mcp-server`.

Drives the real Notion MCP server (via `npx`, pointed at the mock with `BASE_URL`) against the
`live_server` SAMPLE corpus: a group-restricted Notion page the admin can read is blocked for an
outsider — same tool, same page, different identity. Skipped unless `mcp` is installed and `npx`
(Node) is on PATH. First run downloads the npm package.
"""
from __future__ import annotations

import asyncio
import shutil

import pytest
import yaml

pytest.importorskip("mcp")

pytestmark = pytest.mark.skipif(shutil.which("npx") is None, reason="npx (Node) not available")

from app import store, synth  # noqa: E402
from app.acl import Acl  # noqa: E402


def _params(base_url: str, token: str):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                          / "examples" / "using-mcp-with-agents"))
    from notion_server import stdio_params
    return stdio_params(base_url, token)


def _pick_restricted_page(settings, user_token: str) -> tuple[str | None, str]:
    """A Notion page the admin can read but this user cannot (per the mock's own ACL)."""
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    caller = acl.resolve(user_token)
    vids = acl.visible_ids(conn, caller)
    for row in conn.execute("SELECT doc_id, subtype FROM notion_pages"):
        if row["subtype"] == "database":
            continue
        if store.get_document(conn, "notion", row["doc_id"], visible_ids=vids) is None:
            return synth.notion_id(row["doc_id"]), caller.email
    return None, caller.email


async def _reads_page(token: str, page_id: str, base_url: str) -> bool:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_params(base_url, token)) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = (await sess.list_tools()).tools
            # the proxy names tools from the OpenAPI operationIds (e.g. "API-retrieve-a-page")
            tool = next(t for t in tools
                        if "retrieve-a-page" in t.name or ("page" in t.name and "retrieve" in t.name))
            res = await sess.call_tool(tool.name, {"page_id": page_id})
            text = "".join(getattr(c, "text", "") for c in res.content)
            return '"object": "page"' in text or '"object":"page"' in text


def test_mcp_notion_acl_enforced(live_server):
    base, settings = live_server
    user = yaml.safe_load(settings.tokens_path.read_text())["users"][0]
    page_id, email = _pick_restricted_page(settings, user["token"])
    assert page_id, f"no Notion page is ACL-restricted from {email} in the sample corpus"

    admin_reads = asyncio.run(_reads_page(settings.admin_token, page_id, base))
    user_reads = asyncio.run(_reads_page(user["token"], page_id, base))

    assert admin_reads, "admin token should read the page through the Notion MCP server"
    assert not user_reads, f"{email} should be blocked from the page through the Notion MCP server"
