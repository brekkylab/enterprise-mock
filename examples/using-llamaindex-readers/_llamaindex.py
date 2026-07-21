"""Point official LlamaIndex readers at an enterprise-mock server.

Each `llama-index-readers-*` package normally targets a real SaaS host. Four accept a custom
host via constructor args (GitHub `base_url`, Jira `PATauth.server_url`, Confluence `base_url`,
S3 `s3_endpoint_url`); four hardcode it and need a small shim, all isolated here:

  - Slack: set `reader._client.base_url` after construction (slack_sdk builds `base_url + method`).
  - Gmail/Drive: `point_gmail_at` / `point_drive_at` wrap the reader module's `build` symbol to
    inject `client_options(api_endpoint=...)` + `static_discovery=True` (as the SDK examples do).
  - Notion: `patch_notion_at` rebinds the module-level URL constants.

This module also re-exports the serve/credential helpers from the sibling
`using-official-sdk/_mockserver.py`, so these scripts share the same `--url` / `--token` behavior
and local-fallback mock.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Reuse the official-SDK examples' mock plumbing rather than duplicating it.
_SDK_DIR = Path(__file__).resolve().parent.parent / "using-official-sdk"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))

from _mockserver import (  # noqa: E402
    google_oauth_user,
    google_service_account_info,
    serve_or_connect,
)

__all__ = [
    "serve_or_connect", "google_oauth_user", "google_service_account_info",
    "slack_base_url", "notion_base_url", "s3_base_url", "github_base_url",
    "atlassian_base_url", "drop_self_from_syspath",
    "point_gmail_at", "point_drive_at", "patch_notion_at",
]


def slack_base_url(base_url: str) -> str:
    """Slack Web API base for `reader._client.base_url` — trailing slash required (slack_sdk
    builds request URLs as `base_url + method`, e.g. `conversations.history`)."""
    return f"{base_url.rstrip('/')}/slack/api/"


def notion_base_url(base_url: str) -> str:
    """Notion base for `patch_notion_at` — the reader appends the `/v1/...` path itself."""
    return f"{base_url.rstrip('/')}/notion"


def s3_base_url(base_url: str) -> str:
    """S3 endpoint for `S3Reader(s3_endpoint_url=...)` (path-style under `/s3`)."""
    return f"{base_url.rstrip('/')}/s3"


def github_base_url(base_url: str) -> str:
    """GitHub REST base for `GitHubIssuesClient(base_url=...)`."""
    return f"{base_url.rstrip('/')}/github"


def atlassian_base_url(base_url: str) -> str:
    """Atlassian base for Jira `PATauth.server_url` / `ConfluenceReader(base_url=...)`; the
    respective client appends `/rest/api/<ver>` (Jira) or `/wiki/rest/api` (Confluence)."""
    return f"{base_url.rstrip('/')}/atlassian"


def drop_self_from_syspath(file: str) -> None:
    """Remove a script's own directory from sys.path so a file named `jira.py` / `github.py`
    doesn't shadow the third-party `jira` / `github` package it (transitively) imports."""
    here = Path(file).resolve().parent
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != here]
