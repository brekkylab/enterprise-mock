# Using MCP tools with agents

Drive an LLM agent that retrieves corpus data through a **real MCP server** pointed at the
mock, with retrieval **ACL-scoped** by the token you give it. Three servers are wired up:

- **Atlassian** (Jira + Confluence) via the community-official
  [`mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) (Docker).
- **Notion** via the **official**
  [`@notionhq/notion-mcp-server`](https://github.com/makenotion/notion-mcp-server) (npx/Node) —
  it takes a first-class `BASE_URL` override, so pointing it at the mock is one env var.
- **S3** via the **official**
  [`awslabs.aws-api-mcp-server`](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)
  (uvx/Python) — it shells the AWS CLI, whose boto3 client honors a first-class
  `AWS_ENDPOINT_URL` override and SigV4-signs every call, so pointing it at the mock is a
  handful of env vars; it's a broad AWS-CLI wrapper, so the agent runs `aws s3api …` commands.

Pick one with `--server {atlassian,notion,s3}` — **required** (there is no default).

| File | What it is |
|---|---|
| `agent_anthropic.py` | Claude agent (Anthropic SDK + its beta MCP tool runner) — a thin runner |
| `agent_openai.py` | OpenAI agent (OpenAI Agents SDK) — a thin runner |
| `_servers.py` | The **backend registry**: each backend bundles its seed corpus, demo question, and `params(base_url, token, username)` → the MCP server's stdio params |
| `_mockserver.py` | Starts the mock (`app.main`) on a small corpus, plus the shared `--url`/`--token`/`--username`/`--server` CLI parsing |

Both agents are **self-contained** and nearly identical: pick the backend from `--server`, spin up
their own mock (via `_mockserver`, or a `--url` one), connect the chosen MCP server, and run the
backend's question. Everything server-specific — corpus, question, how to launch and point the MCP
server — lives once in `_servers.py`, so adding a backend is one entry there.

## Run

```bash
pip install -e ".[mcp]"          # mcp + openai-agents + anthropic[mcp]
                                 # Atlassian needs Docker; Notion needs Node (npx); S3 needs uvx

# prove retrieval + ACL end-to-end through the real MCP servers — no API key needed.
# One file, one test per backend (each skips if its runtime — Docker / npx / uvx — is absent):
python -m pytest tests/test_mcp.py
#   Atlassian: admin reads an ACL-restricted Jira issue, a user token is blocked
#   Notion:    admin reads an ACL-restricted page, an outsider is blocked
#   S3:        admin lists bucket objects through a signed AWS CLI call

# drive it with an LLM agent (needs an API key). --server is required (atlassian | notion | s3).
OPENAI_API_KEY=…    python examples/using-mcp-with-agents/agent_openai.py --server notion
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/agent_anthropic.py --server atlassian
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/agent_anthropic.py --server s3
```

Each agent spins up its own small mock by default, or pass `--url` to use an already-running one
(unreachable → it falls back to spinning up its own). Retrieval is ACL-scoped by the token:
default is the mock's admin token (sees everything); pass `--token` a per-user token from
`GET /_mock/users` to scope it to that user (the token, not the username, authenticates).

- **Local** — `--url http://localhost:PORT`.
- **Remote** — `--url https://host --token <token> [--username <email>]`. Grab the token from
  `GET /_mock/users` (don't reuse the built-in admin token against someone else's server). The
  Atlassian backend additionally **requires** `--username` for a remote target (see below).

## How the Atlassian backend connects (`_servers.py`)

`mcp-atlassian` runs in Docker and only classifies a host as Atlassian **Cloud** (the v3 + `/wiki`
API shape the mock speaks) when the hostname ends in `.atlassian.net`. So the atlassian backend:

- uses a fake host `mock.atlassian.net`, mapped with Docker's `--add-host` — to the host machine
  (`host-gateway`) for a local mock, or to a **remote** deployment's resolved IP;
- sets `MCP_ALLOWED_URL_DOMAINS=atlassian.net` to pass the server's SSRF guard;
- authenticates with HTTP Basic where the **api-token is a mock token** — the mock resolves it to a
  user and enforces that user's ACL. The Basic-auth **username** is required by mcp-atlassian but
  ignored by the mock once the token resolves, so a placeholder (`svc@example.com`) works for a
  local mock. For a **remote** target it must be explicit (`--username`), and because the
  deployment's TLS cert is for its own name (not `mock.atlassian.net`), cert verification is
  disabled for that hop (`*_SSL_VERIFY=false`) — fine for a test mock.

## How the Notion backend connects (`_servers.py`)

Much simpler — the official `notion-mcp-server` reads a **`BASE_URL`** env var and propagates it
straight to its HTTP client, so the notion backend just sets:

- `BASE_URL=<mock>/notion` — the server appends the `/v1/...` paths from its bundled OpenAPI spec,
  landing on the mock's `/notion/v1/...` routes. It runs on the host via `npx`, so a local
  `localhost` mock is reached directly (no Docker/host-gateway aliasing).
- `NOTION_TOKEN=<mock token>` — sent as `Authorization: Bearer …`; the mock resolves it to a user
  and enforces that user's ACL.
- `NOTION_VERSION=2025-09-03` — the mock's default (data-sources model).

## How the S3 backend connects (`_servers.py`)

`awslabs.aws-api-mcp-server` shells the AWS CLI (botocore underneath), which takes a first-class
endpoint override, so the s3 backend just sets:

- `AWS_ENDPOINT_URL=<mock>/s3` — every AWS CLI call the server runs is routed at the mock instead
  of real AWS.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — derived from the mock token via
  `app.synth.s3_access_key_id` / `s3_secret_access_key` (the same derivation the mock's SigV4
  verifier uses), so botocore's signature resolves back to that token's identity and the mock
  enforces that user's ACL.
- `AWS_REGION=us-east-1` — any region works (the mock's verifier reads the region back out of the
  client's own credential scope); this just has to be *some* valid region.
- `READ_OPERATIONS_ONLY=true` — the server refuses to run mutating AWS CLI commands.

Note this server is a **broad AWS-CLI wrapper**, not S3-specific — under the hood the agent runs
`aws s3api …` commands (e.g. `list-objects-v2`, `get-object`) via the server's `call_aws` tool.

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
