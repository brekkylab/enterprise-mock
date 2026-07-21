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
