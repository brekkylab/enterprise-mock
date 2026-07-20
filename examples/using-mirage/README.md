# Using mirage against the mock

[mirage](https://github.com/strukto-ai/mirage) (`mirage-ai`) is a **virtual filesystem for AI
agents**: it mounts a SaaS backend and lets you read it with plain bash — `ls`, `cat`, `grep`,
`find`, `jq`. These scripts point mirage at enterprise-mock, so you can exercise a mirage-based
agent over a corpus **you** supply, entirely offline.

```bash
pip install -e ".[examples,mirage]"
python examples/using-mirage/slack.py       # or gmail.py, gdrive.py, unified.py
```

Each script spins up its own throwaway mock on a tiny in-code corpus, points a mirage
`Resource` at it, and runs a few filesystem commands. Pass `--url http://host:port` to use an
already-running mock instead (it falls back to a local one if that's unreachable).

```bash
python examples/using-mirage/unified.py --url https://your-mock-host.example.com
```

Add **`--fuse`** to any provider script to expose the mount as a **real OS filesystem** instead
of driving it in-process (see [FUSE mode](#fuse-mode-fuse) below).

## Providers

| Script | mirage resource | mount | what it shows |
|---|---|---|---|
| `slack.py` | `SlackResource` | `/slack` | `ls` channels → dated `chat.jsonl`; `cat` + scoped `grep` |
| `gmail.py` | `GmailResource` | `/gmail` | `ls` labels → dates → messages; `cat` + `jq .subject` |
| `gdrive.py` | `GoogleDriveResource` | `/gdrive` | `ls -F` folders → files; `cat` a native Google doc |
| `unified.py` | all three | `/slack` `/gmail` `/gdrive` | one Workspace, one set of commands, three backends |

The scripts navigate **top-down** (`ls` one level, `cat` one file) rather than walking a whole
mount with `find` / `grep -r`. mirage materializes each directory by calling the API, so a
whole-mount walk over a large corpus (like the live deploy) means thousands of round-trips —
bounded navigation keeps them fast. `find`/`grep -r` still work; just scope them to a subtree.

**Not included:** Jira / Confluence (mirage has no connector for them) and GitHub (mirage's
GitHub connector mirrors a repo's *source-file tree* via the git `trees`/`blobs` API, whereas
the mock's GitHub serves issues/PRs/readme as documents — the models don't line up). Use
[`examples/using-official-sdk/`](../using-official-sdk/) for those.

## The one piece of glue: `point_mirage_at`

The official SDKs take a `base_url`; **mirage does not** — its Slack/Google connectors hardcode
the API host (`slack.com`, `googleapis.com`) in module constants. So `_mirage.py` exposes
`point_mirage_at(base_url)`, which rewrites those constants to the mock before the resources are
built:

```python
from _mirage import point_mirage_at, serve_or_connect
with serve_or_connect(CORPUS) as mock:
    point_mirage_at(mock.base_url)          # slack.com / googleapis.com  ->  the mock
    resource = SlackResource(SlackConfig(token=mock.token))
    ws = Workspace({"/slack": resource}, mode=MountMode.READ)
    print(await (await ws.execute("ls /slack/channels/")).stdout_str())
```

It redirects the Slack API, the OAuth token endpoint, the Drive API, and the Docs/Sheets/Slides
APIs (mirage reads native Google docs structurally through those, not via Drive export).
`_mirage.py` also re-exports `serve_or_connect` / `google_oauth_user` / `cli_token` from
[`../using-official-sdk/_mockserver.py`](../using-official-sdk/_mockserver.py), so the `--url` /
`--user` / `--token` flags behave exactly as in those examples.

## FUSE mode (`--fuse`)

Everything above drives the filesystem **in-process** with `ws.execute("ls …")`. mirage can also
expose a mount as a **real filesystem** via FUSE, so *any* process — `cat`, `grep`, `rg`, an
editor, an indexer — reads the mock's data as ordinary files:

```bash
python examples/using-mirage/slack.py  --url https://your-mock-host.example.com --fuse
python examples/using-mirage/gmail.py  --url https://your-mock-host.example.com --user ceo@acme.com --fuse
python examples/using-mirage/gdrive.py --url https://your-mock-host.example.com --fuse
```

Each prints a real mountpoint (e.g. `/tmp/mirage-xxxx`), reads a file through the kernel's FUSE
layer with plain `os`/`open()`, and runs an external `grep` against it to prove it's a genuine
filesystem. While it's running you can `ls`/`cat`/`grep` the mountpoint from another terminal.

`unified.py --fuse` mounts the **whole workspace root at a single mountpoint**, so `slack/`,
`gmail/`, and `gdrive/` show up as subdirectories of one mount — all three at once, on macOS
included. (macFUSE limits a process to one *mountpoint*, not one source, so a single mount that
serves several sources is fine; what fails is opening several separate mountpoints in one
process.)

**Requirements:**

- `pip install -e ".[mirage]"` already pulls `mirage-ai[fuse]` (the `mfusepy` binding).
- An **OS FUSE driver**: [macFUSE](https://macfuse.io) on macOS, `fuse3` on Linux. Without it,
  `--fuse` prints install guidance and exits cleanly (the non-`--fuse` path needs no driver).

## Performance notes

Against a remote deploy the cost is dominated by (a) TLS/round-trip latency and (b) how many
entries a directory has, because mirage materializes a directory by calling the API for every
entry:

- **Connection reuse** — `run_mirage()` (used by every script here) shares one keep-alive
  connection across mirage's many calls; without it each call pays a fresh TLS handshake
  (≈3x slower end to end over a remote hop).
- **Navigate, don't sweep** — `ls` one level and `cat` one file. `find` / `grep -r` over a
  whole mount force mirage to fetch every file; scope them to a subtree.
- **Listing a huge directory is inherently proportional to its size** — e.g. `ls` of a Drive
  folder with thousands of files, or a large mailbox label (mirage fetches every message to
  group by date). Prefer a `--user` whose ACL-scoped view is smaller, or a narrower path.

The mock side of these paths was tuned alongside these examples (see git history): Slack
`conversations.list` is memoized and its `created` comes from an aggregate (was a full
per-channel message scan); Drive folder listings are SQL-scoped/paginated, resolve folder ids
without an ACL scan, batch the per-file ACL lookup, and honor the `fields` mask.

## Testing per-user ACL

Same as the official-SDK examples: pair the identity flag with `--url`.

```bash
# Slack — a bearer token from GET /_mock/users
python examples/using-mirage/slack.py --url http://localhost:8000 --token <usr-token>

# Gmail / Drive — a user email (authorized-user credential, impersonating that user)
python examples/using-mirage/gdrive.py --url http://localhost:8000 --user mia@acme.com
```

The mounted filesystem then contains only what that identity is allowed to read.

## Mock endpoints this exercises

Pointing mirage at the mock surfaced a few gaps that are now part of the mock (see the git
history): Drive's root is navigable (`'root' in parents` returns folder objects; shared-drives
enumeration is present-but-empty), the Docs/Sheets/Slides read APIs serve native-doc content, a
Slack channel's `created` never postdates its messages, and `conversations.history` honors
`oldest`/`latest` so a per-day fetch returns only that day (not the whole channel). Coverage
lives in [`tests/test_editor_apis.py`](../../tests/test_editor_apis.py).
