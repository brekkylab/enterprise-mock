# Using MCP tools with agents

Drive an LLM agent that retrieves corpus data through a **real MCP server** pointed at the
mock, with retrieval **ACL-scoped** by the token you give it. Two servers are wired up:

- **Atlassian** (Jira + Confluence) via the community-official
  [`mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) (Docker) — the default.
- **Notion** via the **official**
  [`@notionhq/notion-mcp-server`](https://github.com/makenotion/notion-mcp-server) (npx/Node) —
  it takes a first-class `BASE_URL` override, so pointing it at the mock is one env var.

Pick one with `--server {atlassian,notion}` (default `atlassian`).

| File | What it is |
|---|---|
| `agent_anthropic.py` | Claude agent (Anthropic SDK + its beta MCP tool runner) |
| `agent_openai.py` | OpenAI agent (OpenAI Agents SDK) |
| `_servers.py` | Picks the MCP server from `--server` and builds its stdio params |
| `atlassian_server.py` | Builds the `docker run` args that point `mcp-atlassian` at the mock |
| `notion_server.py` | Builds the `npx` args that point `notion-mcp-server` at the mock (`BASE_URL`) |
| `_mockserver.py` | Starts the mock (`app.main`) as a subprocess on a small in-code corpus |

Each script is **self-contained**: it spins up its own mock (via `_mockserver`) and connects the
chosen MCP server to it — nothing else needs to be running.

## Run

```bash
pip install -e ".[mcp]"          # mcp + openai-agents + anthropic[mcp]
                                 # Atlassian needs Docker; Notion needs Node (npx)

# prove retrieval + ACL end-to-end through the real MCP servers — no API key needed
python -m pytest tests/test_mcp.py         # Atlassian (Docker): admin reads a Jira issue, user blocked
python -m pytest tests/test_mcp_notion.py  # Notion (npx): admin reads a page, outsider blocked

# drive it with an LLM agent (needs an API key). --server notion uses the Notion MCP server.
OPENAI_API_KEY=…    python examples/using-mcp-with-agents/agent_openai.py --server notion
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/agent_anthropic.py --server notion
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

## How the Notion server connects

Much simpler than Atlassian — the official `notion-mcp-server` reads a **`BASE_URL`** env var and
propagates it straight to its HTTP client, so `notion_server.py` just sets:

- `BASE_URL=<mock>/notion` — the server appends the `/v1/...` paths from its bundled OpenAPI spec,
  landing on the mock's `/notion/v1/...` routes. It runs on the host via `npx`, so a local
  `localhost` mock is reached directly (no Docker/host-gateway aliasing).
- `NOTION_TOKEN=<mock token>` — sent as `Authorization: Bearer …`; the mock resolves it to a user
  and enforces that user's ACL (pass `--token` a per-user token from `GET /_mock/users` to scope
  retrieval; default is the admin token = sees all).
- `NOTION_VERSION=2025-09-03` — the mock's default (data-sources model).

## Why not the other services

The remaining services' MCP servers **cannot** be pointed at a self-hosted mock, so no example is
provided — using them would require writing a base-URL-switchable MCP server against the mock's
endpoints (not included here):

- **GitHub** — the official `github/github-mcp-server` has `GITHUB_HOST`, but it strips the
  port (so needs port 80), forces GitHub-Enterprise paths (`/api/v3`, `/api/graphql`), and
  relies on GraphQL the mock doesn't implement.
- **Slack** — no API-base override in any maintained server (hard-wired to `slack.com`).
- **Gmail / Google Drive** — official and community servers hard-wire `googleapis.com` and
  require real Google OAuth; no endpoint override.
