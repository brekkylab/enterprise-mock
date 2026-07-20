# Using official SDKs against the mock

One runnable, **self-contained** script per service — each spins up its own mock (via
`_mockserver`) on a tiny in-code corpus, points the official SDK at it, and prints what it read.
The only change from talking to the real service is the base URL.

```bash
pip install -e ".[examples]"
python examples/using-official-sdk/slack.py     # or gmail.py, gdrive.py, github.py, jira.py, confluence.py, notion.py
```

Pass `--url http://host:port` to point a script at an already-running mock instead; if it's
omitted or unreachable, the script falls back to spinning up its own.

### Testing per-user ACL

To see a **specific user's ACL-filtered view**, pass `--user <email>` (Google) or a token
(others):

```bash
# Google: gmail.py (authorized_user) & gdrive.py (service account) both take --user <email>
python examples/using-official-sdk/gmail.py --url http://localhost:8000 --user ava@acme.com

# bearer-token services: slack.py, github.py — grab a token from GET /_mock/users and pass it:
python examples/using-official-sdk/github.py --url http://localhost:8000 --token <usr-token>

# Atlassian Basic auth: jira.py, confluence.py take --username and --password
python examples/using-official-sdk/jira.py --url http://localhost:8000 \
    --username ava@acme.com --password <usr-token>
```

The response then contains only what that identity is allowed to read. Grab tokens/emails from
the running server's [`GET /_mock/users`](../../README.md#auth--tokens) directory. For
Jira/Confluence either `--password <token>` or `--username <email>` alone identifies the user
(the mock resolves by the api token, falling back to the username email). Pair
`--user`/`--token`/`--password` with `--url` so the identity exists on the server you're querying.

### How Google auth works here

The two Google examples show the **two credential shapes** real connectors use — the official
library's own token exchange runs against the mock's `POST /oauth2/token` in both:

- **`gmail.py` → authorized-user (3-legged OAuth)**: `client_id`/`client_secret` + a
  `refresh_token`. The shared `oauth_client` comes from
  [`GET /_mock/credentials`](../../README.md#oauth-client-config-google-style); the
  `refresh_token` is a user's token from `GET /_mock/users`. `--user <email>` picks the user
  (default: the first); there is no admin in this flow.
- **`gdrive.py` → service account**: the key from `/_mock/credentials` (standing in for the JSON
  you'd download from the Cloud Console) signs a JWT. `--user <email>` sets the impersonation
  subject (domain-wide delegation); without it the bare service account maps to the admin
  identity (sees everything).

## Base URL per SDK

| Service | SDK | How to point it at the mock |
|---|---|---|
| Slack | `slack_sdk` | `WebClient(token=T, base_url="http://localhost:8000/slack/api/")` |
| GitHub | `PyGithub` | `Github(auth=Auth.Token(T), base_url="http://localhost:8000/github")` |
| Jira | `atlassian-python-api` | `Jira(url="http://localhost:8000/atlassian", username="svc@x", password=T)` |
| Confluence | `atlassian-python-api` | `Confluence(url="http://localhost:8000/atlassian/wiki", username="svc@x", password=T)` |
| Gmail | `google-api-python-client` | `build("gmail","v1", …, client_options=ClientOptions(api_endpoint="http://localhost:8000"))` |
| Drive | `google-api-python-client` | `build("drive","v3", …, client_options=ClientOptions(api_endpoint="http://localhost:8000/drive/v3"))` |
| Notion | `notion-client` | `Client(auth=T, base_url="http://localhost:8000/notion")` (SDK appends `/v1/`) |

(`T` is a token from `data/tokens.yaml` — the admin token sees everything; a per-user token is
scoped to that user's ACL. For Google, credentials come from a service account issued by
`/_mock/credentials`; pass `static_discovery=True`. A raw `Credentials(token=T)` also still works.)

## Coverage

[`tests/test_sdk.py`](../../tests/test_sdk.py) drives every SDK's read methods — including the
real-world +α (threads, comments, reactions, attachments, doc types, hierarchy, PR reviews) —
against a mock it starts itself, asserting all 39 checks pass across the 6 SDKs:

```bash
python -m pytest tests/test_sdk.py
```
