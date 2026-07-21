"""Read-only coverage: drive each official LlamaIndex reader against the mock.

Uses the `live_server` fixture (a real uvicorn on the conftest SAMPLE corpus) — readers make real
HTTP calls, so they need a listening port. One test per source; each self-skips if its reader
package is absent (installed via the `[llamaindex]` extra). Does not import from `examples/`
(repo rule) — the small point-at-the-mock setup is duplicated here.
"""
from __future__ import annotations

import pytest


def _base_token(live_server):
    base, settings = live_server
    return base, settings.admin_token


def test_github(live_server):
    pytest.importorskip("llama_index.readers.github")
    from llama_index.readers.github import GitHubRepositoryIssuesReader, GitHubIssuesClient

    base, admin = _base_token(live_server)
    client = GitHubIssuesClient(github_token=admin, base_url=f"{base}/github", verbose=False)
    reader = GitHubRepositoryIssuesReader(client, owner="acme", repo="gateway", verbose=False)
    docs = reader.load_data(
        state=GitHubRepositoryIssuesReader.IssueState.OPEN)
    assert docs, "expected at least one issue Document"
    assert any("refill is off by one tick" in d.text for d in docs)  # SAMPLE gh-issue-1 body (open)
    assert all("Corrects the refill tick" not in d.text for d in docs)  # gh-pr-1 (closed) excluded


def _patch_s3fs_walk() -> None:
    """Work around a multi-year fsspec/s3fs compatibility bug (present since at least the
    2023.x releases and reproducing on every version installable today, including fsspec/s3fs
    2026.6.0): `S3Reader.load_data()` in whole-bucket mode (no `key`) calls
    `SimpleDirectoryReader._add_files`, which always does
    `fs.walk(input_dir, topdown=True, maxdepth=...)`. The sync `AbstractFileSystem.walk` declares
    `topdown` as an explicit parameter (so it never reaches `ls`), but `S3FileSystem` is async and
    its `_walk` chain bottoms out in `AsyncFileSystem._walk`, which treats `topdown` as an opaque
    `**kwargs` entry and forwards it straight through to `_ls()` — which doesn't accept it,
    raising ``TypeError: S3FileSystem._ls() got an unexpected keyword argument 'topdown'``.
    Client-side bug independent of the mock (reproduces against real AWS S3 too), so no
    mock-side fix helps.

    Scoped to `S3FileSystem` only (not the shared `fsspec.asyn.AsyncFileSystem` base class) so
    other async fsspec backends (gcsfs, adlfs, ...) are unaffected. Self-verifies against
    `S3FileSystem._ls`'s signature first and no-ops if a future s3fs release has fixed the
    signature to accept `topdown` (directly or via a `**kwargs` catch-all), so we never silently
    drop a `topdown` a fixed s3fs would legitimately honor.

    Duplicated from `examples/using-llamaindex-readers/_llamaindex.py:patch_s3fs_walk` (tests
    don't import from examples). Idempotent."""
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


def test_s3(live_server):
    pytest.importorskip("llama_index.readers.s3")
    pytest.importorskip("s3fs")
    from llama_index.readers.s3 import S3Reader
    from app import synth

    _patch_s3fs_walk()
    base, admin = _base_token(live_server)
    reader = S3Reader(
        bucket="eng-artifacts", s3_endpoint_url=f"{base}/s3",
        aws_access_id=synth.s3_access_key_id(admin),
        aws_access_secret=synth.s3_secret_access_key(admin),
        region_name="us-east-1")
    docs = reader.load_data()
    assert docs, "expected at least one object Document"
    assert any("dashboards" in d.text for d in docs)  # SAMPLE s3-runbook body
