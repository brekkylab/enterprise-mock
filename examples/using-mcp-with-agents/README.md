# Using MCP tools with agents

Drive an LLM agent that retrieves corpus data through a **real MCP server** pointed at the
mock, with retrieval **ACL-scoped** by the credentials you give it. One self-contained file per
service (like the other `examples/` dirs) — run the one you want:

- **`atlassian.py`** (Jira + Confluence) via the community-official
  [`mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) (Docker).
- **`notion.py`** via the **official**
  [`@notionhq/notion-mcp-server`](https://github.com/makenotion/notion-mcp-server) (npx/Node) —
  it takes a first-class `BASE_URL` override, so pointing it at the mock is one env var.
- **`s3.py`** via the **official**
  [`awslabs.aws-api-mcp-server`](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)
  (uvx/Python) — it shells the AWS CLI, whose boto3 client honors a first-class
  `AWS_ENDPOINT_URL` override and SigV4-signs every call; a broad AWS-CLI wrapper, so the agent
  runs `aws s3api …` commands.

Each service file builds its own MCP `StdioServerParameters` and calls `run_agent(...)`. Two shared
helpers:

| File | What it is |
|---|---|
| `_agent.py` | The agent loop for both backends: `--agent anthropic` (default, Anthropic SDK + its beta MCP tool runner) or `--agent openai` (OpenAI Agents SDK) |
| `_mockserver.py` | Starts the mock (`app.main`) on a small corpus, or connects to a `--url` one |

Each service file declares its own CLI options with `argparse` — run `python <file> --help` to see
exactly what that provider takes (e.g. `s3.py` takes `--access-key`/`--secret-key`, required with
`--url`; `atlassian.py` takes `--token`/`--username`). All accept `--url` and `--agent {anthropic,openai}`.

Each example spins up its own small mock by default, or pass `--url` to use an already-running one
(unreachable → it falls back to spinning up its own). Note the demo question is tuned to each
example's own seed corpus; against a `--url` server holding *different* data it may have no exact
match, so the agent answers from the closest documents and notes what's missing (it's told to be
decisive rather than exhaustively hunt).

## Run

```bash
pip install -e ".[mcp]"          # mcp + openai-agents + anthropic[mcp]
                                 # Atlassian needs Docker; Notion needs Node (npx); S3 needs uvx

# prove retrieval + ACL end-to-end through the real MCP servers — no API key needed.
# One test per service (each skips if its runtime — Docker / npx / uvx — is absent):
python -m pytest tests/test_mcp.py
#   Atlassian: admin reads an ACL-restricted Jira issue, a user token is blocked
#   Notion:    admin reads an ACL-restricted page, an outsider is blocked
#   S3:        admin lists bucket objects through a signed AWS CLI call

# drive it with an LLM agent (needs an API key). --agent defaults to anthropic; add --agent openai.
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/atlassian.py
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/notion.py
OPENAI_API_KEY=…    python examples/using-mcp-with-agents/notion.py --agent openai
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/s3.py
```

**Auth is per-service.** Retrieval is ACL-scoped by the identity you pass:

- **Atlassian / Notion** use a mock **token**: default is the admin token (sees everything); pass
  `--token` a per-user token from `GET /_mock/users` to scope it (the token, not the username,
  authenticates).
- **S3** uses an AWS **access-key/secret pair** (not a token): pass `--access-key` / `--secret-key`
  — **required with `--url`** (real AWS keys, or a pair from `GET <url>/_mock/users`, where each
  user and the admin has an `s3_access_key_id` / `s3_secret_access_key`). Without `--url` the local
  throwaway mock uses its own admin keypair.

- **Local** — `--url http://localhost:PORT`.
- **Remote** — `--url https://host` plus the service's credentials. Grab them from
  `GET /_mock/users` (don't reuse the built-in admin token/keys against someone else's server).
  `atlassian.py` additionally **requires** `--username` for a remote target (see below).

## How `atlassian.py` connects

`mcp-atlassian` runs in Docker and only classifies a host as Atlassian **Cloud** (the v3 + `/wiki`
API shape the mock speaks) when the hostname ends in `.atlassian.net`. So the example:

- uses a fake host `mock.atlassian.net`, mapped with Docker's `--add-host` — to the host machine
  (`host-gateway`) for a local mock, or to a **remote** deployment's resolved IP;
- sets `MCP_ALLOWED_URL_DOMAINS=atlassian.net` to pass the server's SSRF guard;
- authenticates with HTTP Basic where the **api-token is a mock token** — the mock resolves it to a
  user and enforces that user's ACL. The Basic-auth **username** is required by mcp-atlassian but
  ignored by the mock once the token resolves, so a placeholder (`svc@example.com`) works for a
  local mock. For a **remote** target it must be explicit (`--username`), and because the
  deployment's TLS cert is for its own name (not `mock.atlassian.net`), cert verification is
  disabled for that hop (`*_SSL_VERIFY=false`) — fine for a test mock.

## How `notion.py` connects

Much simpler — the official `notion-mcp-server` reads a **`BASE_URL`** env var and propagates it
straight to its HTTP client, so the example just sets:

- `BASE_URL=<mock>/notion` — the server appends the `/v1/...` paths from its bundled OpenAPI spec,
  landing on the mock's `/notion/v1/...` routes. It runs on the host via `npx`, so a local
  `localhost` mock is reached directly (no Docker/host-gateway aliasing).
- `NOTION_TOKEN=<mock token>` — sent as `Authorization: Bearer …`; the mock resolves it to a user
  and enforces that user's ACL.
- `NOTION_VERSION=2025-09-03` — the mock's default (data-sources model).

## How `s3.py` connects

`awslabs.aws-api-mcp-server` shells the AWS CLI (botocore underneath), which takes a first-class
endpoint override, so the example just sets:

- `AWS_ENDPOINT_URL=<mock>/s3` — every AWS CLI call the server runs is routed at the mock instead
  of real AWS.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — the required `--access-key` / `--secret-key` (the
  keys the mock's SigV4 verifier accepts; grab a pair from `GET /_mock/users`), so botocore's
  signature resolves back to that identity and the mock enforces its ACL.
- `AWS_REGION=us-east-1` — any region works (the mock's verifier reads the region back out of the
  client's own credential scope); this just has to be *some* valid region.

We intentionally do **not** set `READ_OPERATIONS_ONLY`. It sounds right for a read-only mock, but
it blocks `aws s3 cp s3://<bucket>/<key> -` — the one command that streams an object's **body**
back to the model. (A read-only `s3api get-object` writes the bytes to a sandboxed file and returns
only metadata; `… /dev/stdout` is path-blocked/deadlocks.) So with it on, the agent can *list*
objects but never *read* them, and it thrashes. The mock has no write endpoints, so dropping the
guard is safe here. The example's question therefore tells the agent to read via `s3 cp … -`.

Note this server is a **broad AWS-CLI wrapper**, not S3-specific — under the hood the agent runs
`aws s3api …` commands (e.g. `list-objects-v2`, `get-object`) via the server's `call_aws` tool.
Because it exposes *all* of the AWS CLI (not a domain search tool like the Notion/Atlassian
servers), the agent has no way to know the corpus lives in S3 — left unguided it wanders off to
AWS's actual search/config services (Kendra, SSM, …). So `s3.py`'s question **explicitly tells it
to search S3 only** (list buckets → list objects → get object). This steering is the price of
using a generic AWS-CLI MCP for retrieval.

**Gotcha — loopback-only endpoint:** awslabs' server has an SSRF guard (`_validate_endpoint` in
its command parser) that only accepts a **loopback** endpoint — `localhost` / `127.0.0.1` / `::1`.
A hostname `--url` (e.g. an ALB-fronted `https://…` deployment) is rejected with `Could not resolve
endpoint …`, and even a non-loopback IP is rejected with `Local endpoint was not a loopback
address`. To drive a remote deployment, tunnel it to loopback and point `--url` there:
`ssh -fN -L 18000:127.0.0.1:8000 user@host`, then
`--url http://127.0.0.1:18000 --access-key … --secret-key …`. (boto3 and mirage have no such
restriction — they take the hostname directly.)

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
