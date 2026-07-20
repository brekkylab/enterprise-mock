"""Point the **official** Notion MCP server (`@notionhq/notion-mcp-server`) at this mock.

Unlike `mcp-atlassian` (Docker + a `*.atlassian.net` host trick), the Notion server runs on the
host via `npx` and takes a first-class **`BASE_URL`** override, so pointing it at the mock is just
one env var — it reaches a local `localhost` mock directly (no host-gateway aliasing).

- `BASE_URL` — the mock's Notion prefix (`…/notion`); the server appends the `/v1/...` paths from
  its bundled OpenAPI spec, so requests land on the mock's `/notion/v1/...` routes.
- `NOTION_TOKEN` — a **mock** token: the server sends it as `Authorization: Bearer <token>` and the
  mock resolves it to a user and enforces that user's ACL (a per-user token from `GET /_mock/users`
  scopes retrieval; the admin token sees everything).
- `NOTION_VERSION` — pinned to `2025-09-03` (the mock's default; the data-sources model).

Requires Node (`npx` on PATH). `stdio_params(base_url, token)` returns MCP StdioServerParameters
the agent scripts feed to `stdio_client`.
"""
from __future__ import annotations


def stdio_params(base_url: str, token: str):
    from mcp import StdioServerParameters
    return StdioServerParameters(
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        env={
            "BASE_URL": f"{base_url.rstrip('/')}/notion",
            "NOTION_TOKEN": token,
            "NOTION_VERSION": "2025-09-03",
        },
    )
