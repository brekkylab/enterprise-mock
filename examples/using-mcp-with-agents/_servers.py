"""Pick the MCP server the agent scripts drive: `--server atlassian` (default) or `--server notion`.

- **atlassian** → `mcp-atlassian` in Docker (see `atlassian_server.py`); a remote `--url` also
  needs `--token`/`--username`.
- **notion** → the official `@notionhq/notion-mcp-server` via `npx` (see `notion_server.py`),
  pointed at the mock with `BASE_URL`; reaches a local mock directly.

`mcp_params(base_url, token)` returns the `StdioServerParameters` for the chosen server.
"""
from __future__ import annotations

import sys


def _arg(name: str) -> str | None:
    argv = sys.argv[1:]
    flag = f"--{name}"
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


SERVER = (_arg("server") or "atlassian").lower()


def mcp_params(base_url: str, token: str):
    from mcp import StdioServerParameters
    if SERVER == "notion":
        import notion_server
        return notion_server.stdio_params(base_url, token)
    if SERVER != "atlassian":
        sys.exit(f"--server must be 'atlassian' or 'notion', got {SERVER!r}")
    import atlassian_server
    atlassian_server.configure(base_url, token)
    return StdioServerParameters(command="docker", args=atlassian_server.docker_args())
