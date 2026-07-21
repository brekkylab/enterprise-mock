"""ACL enforced end-to-end through real MCP servers pointed at the mock.

Uses the ``live_server`` fixture (a real ``uvicorn`` on the conftest SAMPLE corpus) and drives a
real MCP server against it: a document the admin can read is blocked for an ACL-restricted user —
same tool, same object, different identity.

- **Atlassian** (`mcp-atlassian`, Docker) — skipped unless Docker is available.
- **Notion** (`@notionhq/notion-mcp-server`, npx) — skipped unless ``npx`` (Node) is on PATH; the
  first run downloads the npm package.
- **S3** (`awslabs.aws-api-mcp-server`, uvx) — skipped unless ``uvx`` is on PATH; the first run
  downloads the package. This one isn't an ACL test (it's a broad AWS-CLI wrapper, not read-one-
  object-at-a-time like the others): it just proves the server, pointed at the mock via
  ``AWS_ENDPOINT_URL``, lists a bucket's objects through a real signed AWS CLI call.

All require the ``mcp`` package. The stdio params below intentionally **duplicate** the wiring in
``examples/using-mcp-with-agents/_servers.py`` rather than importing it — a test must not reach
into ``examples/`` (no ``sys.path`` hacks); a little copied setup is the lesser evil.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest
import yaml

pytest.importorskip("mcp")

from app import store, synth  # noqa: E402
from app.acl import Acl  # noqa: E402


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _atlassian_params(base: str, token: str):
    """`docker run` args pointing mcp-atlassian at a local mock (see examples/.../_servers.py).

    mcp-atlassian only classifies a host as Atlassian *Cloud* when it ends in `.atlassian.net`, so
    use a fake `mock.atlassian.net` mapped to the host via `--add-host`, and Basic auth where the
    api_token is a mock token (the mock resolves it to a user and enforces that user's ACL)."""
    from mcp import StdioServerParameters

    port = base.rsplit(":", 1)[1]  # Docker reaches the host mock via host-gateway
    host, url = "mock.atlassian.net", f"http://mock.atlassian.net:{port}"
    return StdioServerParameters(command="docker", args=[
        "run", "-i", "--rm", f"--add-host={host}:host-gateway",
        "-e", f"JIRA_URL={url}/atlassian", "-e", "JIRA_USERNAME=svc@example.com",
        "-e", f"JIRA_API_TOKEN={token}",
        "-e", f"CONFLUENCE_URL={url}/atlassian/wiki", "-e", "CONFLUENCE_USERNAME=svc@example.com",
        "-e", f"CONFLUENCE_API_TOKEN={token}",
        "-e", "MCP_ALLOWED_URL_DOMAINS=atlassian.net", "-e", "READ_ONLY_MODE=true",
        "ghcr.io/sooperset/mcp-atlassian:latest", "--transport", "stdio",
    ])


def _notion_params(base: str, token: str):
    """`npx` args pointing the official notion-mcp-server at the mock via BASE_URL."""
    from mcp import StdioServerParameters

    return StdioServerParameters(command="npx", args=["-y", "@notionhq/notion-mcp-server"],
                                 env={"BASE_URL": f"{base.rstrip('/')}/notion",
                                      "NOTION_TOKEN": token, "NOTION_VERSION": "2025-09-03"})


def _restricted_doc(settings, user_token: str, source: str, where: str = "1=1"):
    """A doc of ``source`` the admin can read but this user cannot (per the mock's own ACL)."""
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    caller = acl.resolve(user_token)
    vids = acl.visible_ids(conn, caller)
    for row in conn.execute(f"SELECT * FROM {store.table(source)} WHERE {where}"):
        if store.get_document(conn, source, row["doc_id"], visible_ids=vids) is None:
            return row, caller.email
    return None, caller.email


async def _call(params, tool_pred, args, ok_pred) -> bool:
    """Connect via ``params``, call the tool matched by ``tool_pred`` with ``args``, and return
    ``ok_pred(text)`` over the response text."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = (await sess.list_tools()).tools
            tool = next(t for t in tools if tool_pred(t.name))
            res = await sess.call_tool(tool.name, args)
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

    def reads(token):
        return asyncio.run(_call(
            _atlassian_params(base, token),
            tool_pred=lambda n: n == "jira_get_issue",
            args={"issue_key": key},
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

    def reads(token):
        return asyncio.run(_call(
            _notion_params(base, token),
            # the proxy names tools from OpenAPI operationIds (e.g. "API-retrieve-a-page")
            tool_pred=lambda n: "retrieve-a-page" in n or ("page" in n and "retrieve" in n),
            args={"page_id": page_id},
            ok_pred=lambda t: '"object": "page"' in t or '"object":"page"' in t))

    assert reads(settings.admin_token), "admin should read the page through notion-mcp-server"
    assert not reads(user["token"]), f"{email} should be blocked from the page via notion-mcp-server"


# --------------------------------------------------------------------------- S3

def _s3_params(base: str, token: str):
    """`uvx` args pointing the awslabs aws-api MCP server at the mock via AWS_ENDPOINT_URL (see
    examples/.../_servers.py). The server shells the AWS CLI, whose boto3 client SigV4-signs each
    call; the mock verifies the signature against the access-key/secret derived from ``token``."""
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command="uvx", args=["awslabs.aws-api-mcp-server@latest"],
        env={"AWS_ENDPOINT_URL": f"{base.rstrip('/')}/s3",
             "AWS_ACCESS_KEY_ID": synth.s3_access_key_id(token),
             "AWS_SECRET_ACCESS_KEY": synth.s3_secret_access_key(token),
             "AWS_REGION": "us-east-1", "READ_OPERATIONS_ONLY": "true"})


@pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")
def test_mcp_s3_lists_objects(live_server):
    """The awslabs aws-api MCP server, pointed at the mock, lists objects via a signed AWS CLI call."""
    base, settings = live_server
    params = _s3_params(base, settings.admin_token)
    out = asyncio.run(_call(
        params,
        # the server also exposes a "suggest_aws_commands" tool; pick the one that runs a command.
        tool_pred=lambda name: name == "call_aws",
        args={"cli_command": f"aws s3api list-objects-v2 --bucket eng-artifacts "
                             f"--endpoint-url {base}/s3"},
        ok_pred=lambda text: "runbooks/oncall.md" in text))
    assert out, "expected the SAMPLE eng-artifacts/runbooks/oncall.md key in the listing"
