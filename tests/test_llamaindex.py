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


def test_confluence(live_server):
    pytest.importorskip("llama_index.readers.confluence")
    from llama_index.readers.confluence import ConfluenceReader

    base, admin = _base_token(live_server)
    # atlassian-python-api 4.0.7 does not append `/wiki` itself regardless of `cloud`, so the
    # mock's `/atlassian/wiki/rest/api` root must be spelled out here (`cloud` only toggles
    # cloud-specific API shapes elsewhere, not the URL). `max_num_results` must be passed
    # explicitly: llama-index-readers-confluence 0.7.0's `load_data` forwards a bare `limit=None`
    # to `Confluence.get_all_pages_from_space`, which does `len(results) <= limit` and raises
    # `TypeError` when `limit` is None — a client-side bug independent of the mock/server.
    reader = ConfluenceReader(base_url=f"{base}/atlassian/wiki", cloud=False, api_token=admin)
    docs = reader.load_data(space_key="handbook", max_num_results=50)
    assert docs, "expected at least one page Document"
    assert any("How we build software" in d.text for d in docs)  # SAMPLE cf-handbook body
    assert all("Compensation Bands" not in d.text for d in docs)  # cf-comp (people-ops) excluded


def test_jira(live_server):
    pytest.importorskip("llama_index.readers.jira")
    from llama_index.readers.jira import JiraReader

    base, admin = _base_token(live_server)
    reader = JiraReader(PATauth={"server_url": f"{base}/atlassian", "api_token": admin})
    docs = reader.load_data(query="project = payments")
    assert docs, "expected at least one issue Document"
    assert any("checkout latency" in d.text for d in docs)  # SAMPLE jira-sev2 body

    # container scoping: an unresolvable project must yield zero issues, not the unfiltered
    # corpus (the same silent-no-op fidelity gap found & fixed for Confluence's space= handling
    # applied identically to Jira's project= handling -- see app/routers/atlassian.py).
    empty = reader.load_data(query="project = NOPE_DOES_NOT_EXIST")
    assert empty == []


def _slack_reader_at(base: str, token: str):
    """Build a `SlackReader` pointed at the mock.

    `SlackReader.__init__` doesn't just stash the slack_sdk `WebClient` — it eagerly calls
    `client.api_test()` *during construction*, before the caller gets any object back to set
    `_client.base_url` on. Left alone, that call goes to the real `https://slack.com/api/`
    default, so "set base_url after construction" (as the interface note says, and as slack_sdk's
    own `WebClient(base_url=...)` constructor arg would suggest) can't work here: construction
    itself fails/network-egresses first. `SlackReader.__init__` does a *local*
    `from slack_sdk import WebClient` on every call, so temporarily swapping the `slack_sdk`
    module's `WebClient` attribute for a subclass that defaults to the mock's base_url — for the
    duration of construction only — redirects that eager call to the mock instead (which now
    serves `api.test`, see `app/routers/slack.py`). Restored in `finally` so nothing leaks past
    this one construction. Duplicated from `examples/using-llamaindex-readers/slack.py` (tests
    don't import from examples).
    """
    import slack_sdk
    from llama_index.readers.slack import SlackReader

    mocked_url = f"{base}/slack/api/"  # trailing slash required (base_url + method)
    real_web_client = slack_sdk.WebClient

    class _WebClientAtMock(real_web_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("base_url", mocked_url)
            super().__init__(*args, **kwargs)

    slack_sdk.WebClient = _WebClientAtMock
    try:
        reader = SlackReader(slack_token=token)
    finally:
        slack_sdk.WebClient = real_web_client
    reader._client.base_url = mocked_url  # already set via the patched default; explicit for clarity
    return reader


def test_slack(live_server):
    pytest.importorskip("llama_index.readers.slack")

    base, admin = _base_token(live_server)
    reader = _slack_reader_at(base, admin)
    channels = reader._client.conversations_list(limit=200)["channels"]
    ids = [c["id"] for c in channels]
    docs = reader.load_data(channel_ids=ids)
    assert docs, "expected at least one channel Document"
    assert any("502" in d.text for d in docs)  # SAMPLE incidents channel


def _patch_s3fs_walk() -> None:
    """Work around a multi-year fsspec/s3fs compatibility bug (present since at least the
    2023.x releases and reproducing on every version installable today, including fsspec/s3fs
    2026.6.0): `S3Reader.load_data()` in whole-bucket mode (no `key`) calls
    `SimpleDirectoryReader._add_files`, which always does
    `fs.walk(input_dir, topdown=True, maxdepth=...)`. The sync `AbstractFileSystem.walk` declares
    `topdown` as an explicit parameter (so it never reaches `ls`), but `S3FileSystem` is async and
    its `_walk` chain bottoms out in `_ls()`, which doesn't accept `topdown`, raising
    ``TypeError: S3FileSystem._ls() got an unexpected keyword argument 'topdown'``. Client-side
    bug independent of the mock (reproduces against real AWS S3 too), so no mock-side fix helps.

    Wraps the *original* `S3FileSystem._walk` (captured before patching) rather than delegating
    to the shared `fsspec.asyn.AsyncFileSystem._walk` base implementation: `S3FileSystem` defines
    its own `_walk` with S3-specific logic (e.g. a guard raising `ValueError("Cannot crawl all of
    S3")` for bucket-less/root crawls) before calling up to the async base — bypassing it via
    `AsyncFileSystem._walk` directly would silently drop that guard (and any other S3-specific
    behavior) for every whole-bucket walk. Wrapping the original preserves all of it; the wrapper
    only strips the offending `topdown` kwarg. Scoped to `S3FileSystem` only, so other async
    fsspec backends (gcsfs, adlfs, ...) are unaffected. Self-verifies against `S3FileSystem._ls`'s
    signature first and no-ops if a future s3fs release has fixed the signature to accept
    `topdown` (directly or via a `**kwargs` catch-all), so we never silently drop a `topdown` a
    fixed s3fs would legitimately honor.

    Duplicated from `examples/using-llamaindex-readers/_llamaindex.py:patch_s3fs_walk` (tests
    don't import from examples). Idempotent."""
    import inspect

    from s3fs.core import S3FileSystem

    if getattr(S3FileSystem._walk, "_mock_patched", False):
        return

    ls_params = inspect.signature(S3FileSystem._ls).parameters
    if "topdown" in ls_params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in ls_params.values()
    ):
        return  # upstream fixed; the topdown-stripping shim is no longer needed

    _original_walk = S3FileSystem._walk  # own definition if present, else inherited

    async def _walk(self, path, *args, **kwargs):
        kwargs.pop("topdown", None)
        async for item in _original_walk(self, path, *args, **kwargs):
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


def _patch_notion_at(base_url: str) -> None:
    """Redirect NotionPageReader at the mock. The reader hardcodes the Notion host in module-level
    URL constants (no base_url arg); rebind every one that points at api.notion.com. Fails loudly
    if the expected constants are gone (a reader upgrade), rather than hitting the real host.

    Duplicated from `examples/using-llamaindex-readers/_llamaindex.py:patch_notion_at` (tests
    don't import from examples)."""
    import llama_index.readers.notion.base as nb

    base = base_url.rstrip("/")
    overrides = {
        "BLOCK_CHILD_URL_TMPL": base + "/v1/blocks/{block_id}/children",
        "DATABASE_URL_TMPL": base + "/v1/databases/{database_id}/query",
        "SEARCH_URL": base + "/v1/search",
    }
    patched = 0
    for name, value in overrides.items():
        if hasattr(nb, name):
            setattr(nb, name, value)
            patched += 1
    # Catch any other hardcoded api.notion.com occurrence (e.g. single-page retrieval) the version
    # may add, so nothing silently escapes to the real host.
    for name in dir(nb):
        val = getattr(nb, name)
        if isinstance(val, str) and "api.notion.com" in val:
            setattr(nb, name, val.replace("https://api.notion.com", base))
            patched += 1
    if patched == 0:
        raise RuntimeError("patch_notion_at found no Notion URL constants to rebind — reader layout "
                           "changed; update the shim before it silently hits api.notion.com")


def test_notion(live_server):
    pytest.importorskip("llama_index.readers.notion")
    from llama_index.readers.notion import NotionPageReader
    from app import synth

    base, admin = _base_token(live_server)
    _patch_notion_at(f"{base}/notion")
    reader = NotionPageReader(integration_token=admin)
    docs = reader.load_data(page_ids=[synth.notion_id("nt-runbook")])
    assert docs, "expected the runbook page as a Document"
    assert any("Check dashboards" in d.text for d in docs)  # SAMPLE nt-runbook body, case-correct
