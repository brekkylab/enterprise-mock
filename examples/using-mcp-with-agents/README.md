# Using MCP tools with agents

Drive an LLM agent that retrieves corpus data through a **real MCP server** pointed at the
mock. Works today for **Jira + Confluence** via the community-official
[`mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) server, with retrieval
**ACL-scoped** by the token you give it.

| File | What it is |
|---|---|
| `agent_anthropic.py` | Claude agent (Anthropic SDK + its beta MCP tool runner) |
| `agent_openai.py` | OpenAI agent (OpenAI Agents SDK) |
| `atlassian_server.py` | Builds the `docker run` args that point `mcp-atlassian` at the mock |
| `_mockserver.py` | Starts the mock (`app.main`) as a subprocess on a small in-code corpus |

Each script is **self-contained**: it spins up its own mock (via `_mockserver`) and connects
`mcp-atlassian` to it — nothing else needs to be running.

## Run

```bash
pip install -e ".[mcp]"          # mcp + openai-agents + anthropic[mcp]; Docker also required

# prove retrieval + ACL end-to-end through the real MCP server — no API key needed
python -m pytest tests/test_mcp.py
#   → an ACL-restricted Jira issue is readable by the admin token, blocked for a user token

# drive it with an LLM agent (needs an API key)
OPENAI_API_KEY=…    python examples/using-mcp-with-agents/agent_openai.py
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/agent_anthropic.py
```

Each agent spins up its own small mock by default, or pass `--url` to use an already-running one
(unreachable → it falls back to spinning up its own):

- **Local** — `--url http://localhost:PORT`. The container reaches it via `host-gateway`.
- **Remote** — `--url https://host --token <token> --username <email>`. Both `--token` and
  `--username` are **required** for a remote target: the **token** authenticates and scopes ACL
  (grab one from `GET /_mock/users` — don't silently reuse the built-in admin token against
  someone else's server), and mcp-atlassian additionally needs a Basic-auth **username** to
  enable Cloud tools. Since mcp-atlassian only speaks the Cloud API to a `*.atlassian.net` host,
  the agent aliases `mock.atlassian.net` → the deployment's resolved IP; because the deployment's
  TLS cert is for its own name (not `mock.atlassian.net`), cert verification is disabled for that
  hop (`*_SSL_VERIFY=false`) — fine for a test mock. Example:
  ```bash
  python examples/using-mcp-with-agents/agent_anthropic.py \
      --url https://your-mock-host.example.com --token <token> --username ava@redwoodinference.com
  ```

For a **local** mock, retrieval is ACL-scoped by `MOCK_MCP_TOKEN` (default: the mock's admin
token, which sees everything); set it (or `--token`) to a per-user token to scope it — the
token, not the username, authenticates.

## How it connects to the mock

`mcp-atlassian` runs in Docker and only classifies a host as Atlassian **Cloud** (the v3 +
`/wiki` API shape the mock speaks) when the hostname ends in `.atlassian.net`. So
`atlassian_server.py`:

- uses a fake host `mock.atlassian.net` and maps it to the host machine with Docker's
  `--add-host=mock.atlassian.net:host-gateway` (no `/etc/hosts` edits);
- sets `MCP_ALLOWED_URL_DOMAINS=atlassian.net` to pass the server's SSRF guard;
- authenticates with HTTP Basic where the **api-token is a mock token** — the mock resolves it
  to a user and enforces that user's ACL (set `MOCK_MCP_TOKEN` to a per-user token to scope
  retrieval; default is the admin token). The Basic-auth **username** (`MOCK_MCP_USERNAME`,
  default `svc@example.com`) is only a fallback identity for when the token is unknown, so with
  a valid token it's ignored — that's why a placeholder works regardless of the corpus's domain.

## Why only Atlassian

The other services' MCP servers **cannot** be pointed at a self-hosted mock, so no example is
provided — using them would require writing a base-URL-switchable MCP server against the mock's
endpoints (not included here):

- **GitHub** — the official `github/github-mcp-server` has `GITHUB_HOST`, but it strips the
  port (so needs port 80), forces GitHub-Enterprise paths (`/api/v3`, `/api/graphql`), and
  relies on GraphQL the mock doesn't implement.
- **Slack** — no API-base override in any maintained server (hard-wired to `slack.com`).
- **Gmail / Google Drive** — official and community servers hard-wire `googleapis.com` and
  require real Google OAuth; no endpoint override.
