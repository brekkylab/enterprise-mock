# Using LlamaIndex readers with the mock

Point official [LlamaIndex readers](https://docs.llamaindex.ai/en/stable/module_guides/loading/connector/)
(`llama-index-readers-*`) at the mock and load your enterprise corpus as `Document` objects — the
first step of any LlamaIndex ingestion / RAG pipeline. Each script is self-contained:

    pip install -e ".[examples,llamaindex]"
    python examples/using-llamaindex-readers/github.py            # local throwaway mock
    python examples/using-llamaindex-readers/github.py --url http://localhost:8000 --token <usr-token>

The only difference from talking to the real SaaS is where the reader points. Four readers take a
host argument directly; four hardcode the host, so a tiny shim in `_llamaindex.py` redirects them.

| Source | Reader class | How it's pointed at the mock |
|--------|--------------|------------------------------|
| GitHub | `GitHubRepositoryIssuesReader` | `GitHubIssuesClient(base_url=...)` |
| Jira | `JiraReader` | `PATauth={"server_url": ...}` |
| Confluence | `ConfluenceReader` | `base_url=...` |
| S3 | `S3Reader` | `s3_endpoint_url=...` |
| Slack | `SlackReader` | set `reader._client.base_url` |
| Notion | `NotionPageReader` | `patch_notion_at()` (rebinds URL constants) |
| Gmail | `GmailReader` | `point_gmail_at()` (wraps `build`) |
| Drive | `GoogleDriveReader` | `point_drive_at()` (wraps `build`) |

All reads are ACL-scoped by the credential you pass (`--token`, or the admin token by default),
exactly as against the real API.

**S3 note:** `S3Reader.load_data()` in whole-bucket mode hits a client-side fsspec/s3fs
compatibility bug that has existed since at least the 2023.x releases of both libraries — it
predates and reproduces on every version installable today, including fsspec/s3fs 2026.6.0 —
where `SimpleDirectoryReader`'s directory walk passes a `topdown` kwarg that `S3FileSystem`'s
async `_ls()` doesn't accept. It reproduces against real AWS S3 too — unrelated to the mock.
Because no released version of fsspec/s3fs avoids this bug, pinning to an older release isn't a
viable workaround, so `s3.py` calls `_llamaindex.patch_s3fs_walk()` (a small monkeypatch scoped to
`S3FileSystem`, self-disabling if a future release fixes the signature) before constructing the
reader; `tests/test_llamaindex.py` duplicates the same patch inline. No path-style-addressing
workaround was needed — s3fs auto-selects path-style against a `localhost` endpoint.
