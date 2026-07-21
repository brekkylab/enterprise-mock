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
    "point_gmail_at", "point_drive_at", "patch_notion_at", "patch_s3fs_walk",
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


def patch_s3fs_walk() -> None:
    """Work around a multi-year fsspec/s3fs compatibility bug (present since at least the
    2023.x releases and reproducing on every version installable today, including fsspec/s3fs
    2026.6.0): `S3Reader.load_data()` in whole-bucket mode (no `key`) calls
    `SimpleDirectoryReader._add_files`, which always does
    `fs.walk(input_dir, topdown=True, maxdepth=...)`. The sync `AbstractFileSystem.walk` declares
    `topdown` as an explicit parameter (so it never reaches `ls`), but `S3FileSystem` is async and
    its `_walk` chain bottoms out in `AsyncFileSystem._walk`, which treats `topdown` as an opaque
    `**kwargs` entry and forwards it straight through to `_ls()` — which doesn't accept it,
    raising ``TypeError: S3FileSystem._ls() got an unexpected keyword argument 'topdown'``. This
    is a client-side bug independent of the mock (reproduces identically against real AWS S3 with
    the same library versions), so no mock-side change can fix it.

    Scoped to `S3FileSystem` only (not the shared `fsspec.asyn.AsyncFileSystem` base class) so
    other async fsspec backends (gcsfs, adlfs, ...) are unaffected. Self-verifies against
    `S3FileSystem._ls`'s signature first and no-ops if a future s3fs release has fixed the
    signature to accept `topdown` (directly or via a `**kwargs` catch-all), so we never silently
    drop a `topdown` a fixed s3fs would legitimately honor. Idempotent; safe to call more than
    once or from multiple scripts in the same process."""
    import inspect

    from fsspec.asyn import AsyncFileSystem
    from s3fs.core import S3FileSystem

    if getattr(S3FileSystem._walk, "_mock_patched", False):
        return

    ls_params = inspect.signature(S3FileSystem._ls).parameters
    if "topdown" in ls_params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in ls_params.values()
    ):
        return  # upstream fixed; the topdown-stripping shim is no longer needed

    async def _walk(self, path, maxdepth=None, on_error="omit", **kwargs):
        kwargs.pop("topdown", None)
        async for item in AsyncFileSystem._walk(
            self, path, maxdepth=maxdepth, on_error=on_error, **kwargs
        ):
            yield item

    _walk._mock_patched = True
    S3FileSystem._walk = _walk


def drop_self_from_syspath(file: str) -> None:
    """Remove a script's own directory from sys.path so a file named `jira.py` / `github.py`
    doesn't shadow the third-party `jira` / `github` package it (transitively) imports."""
    here = Path(file).resolve().parent
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != here]
