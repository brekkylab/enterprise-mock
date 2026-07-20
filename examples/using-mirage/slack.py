#!/usr/bin/env python3
"""Read Slack through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts the mock's Slack API as a filesystem — channels, dates, and a ``chat.jsonl`` per
day — so an agent reads it with plain ``ls`` / ``cat``. The only mirage-specific glue is
``point_mirage_at`` (mirage hardcodes ``slack.com``; we redirect it at the mock).

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/slack.py                                  # local throwaway mock
    python examples/using-mirage/slack.py --url http://localhost:8000
    python examples/using-mirage/slack.py --url http://localhost:8000 --token <usr-token>
    python examples/using-mirage/slack.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the channel tree is exposed as an actual filesystem (needs macFUSE/fuse3) and
read with plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""
import os
import subprocess
import sys

from mirage import MountMode, Workspace
from mirage.resource.slack import SlackConfig, SlackResource

from _mirage import (FUSE_HELP, cli_token, lines, point_mirage_at, run_mirage,
                     serve_or_connect)

CORPUS = [  # `created` keeps the throwaway channels' dates tight (one day) rather than synthesized
    {"source_type": "slack", "channel": "eng", "content": "Deploy freeze starts Friday 5pm.",
     "created": "2024-08-01T09:00:00Z"},
    {"source_type": "slack", "channel": "incidents", "content": "Anyone seeing 502s from the gateway?",
     "created": "2024-08-01T14:30:00Z",
     "replies": [{"content": "Looking now."}, {"content": "Rolled back — clearing up."}]},
]


def build(mock):
    # Redirect mirage's hardcoded Slack host at the mock, then build the resource.
    # --token <usr-token> (from /_mock/users) → ACL-filtered to that user; else admin sees all.
    point_mirage_at(mock.base_url)
    return SlackResource(SlackConfig(token=cli_token(mock.token)))


async def main(resource) -> None:
    ws = Workspace({"/slack": resource}, mode=MountMode.READ)

    print("=== ls /slack/ ===")
    print((await (await ws.execute("ls /slack/")).stdout_str()).rstrip())

    r = await ws.execute("ls /slack/channels/")
    channels = lines(await r.stdout_str())
    if not channels:
        print("no channels visible to this identity")
        return
    print(f"\n=== {len(channels)} channel(s); reading #{channels[0]} ===")

    # channels/<name>/<date>/chat.jsonl — grab the most recent day's transcript.
    base = f"/slack/channels/{channels[0]}"
    dates = lines(await (await ws.execute(f'ls "{base}/"')).stdout_str())
    if not dates:
        print("  channel has no dated messages")
        return
    day = dates[-1].rstrip("/")
    chat = f"{base}/{day}/chat.jsonl"
    print(f"$ cat {chat}")
    print((await (await ws.execute(f'cat "{chat}"')).stdout_str()).rstrip()[:600])

    # grep is scoped to the one day's transcript (walking every channel/day would be huge).
    print(f"\n=== grep -c message {base}/{day}/ ===")
    r = await ws.execute(f'grep -rc message "{base}/{day}/"')
    print("  " + ((await r.stdout_str()).rstrip() or "(no matches)"))


def main_fuse(resource) -> None:
    """--fuse: mount the channel tree as a *real* filesystem, then read it with ordinary tools —
    any process (grep, an editor, an indexer) can open the files. Needs macFUSE/fuse3."""
    try:
        with Workspace({"/slack": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/slack")  # "/slack" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            channel = sorted(os.listdir(f"{mnt}/channels"))[0]
            day = sorted(os.listdir(f"{mnt}/channels/{channel}"))[-1]  # most recent day
            chat = f"{mnt}/channels/{channel}/{day}/chat.jsonl"
            print(f"\n$ head -c 160 channels/{channel}/{day}/chat.jsonl")
            print("  " + open(chat).read(160).replace("\n", " "))  # a genuine open() via FUSE
            count = subprocess.run(["grep", "-c", ".", chat], capture_output=True, text=True)
            print(f"\n$ grep -c . <that file>   # a separate process reads the mount → {count.stdout.strip()}")
            print(f"\nexplore it live in another terminal:  ls {mnt}/channels")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


if __name__ == "__main__":
    with serve_or_connect(CORPUS) as mock:
        resource = build(mock)
        if "--fuse" in sys.argv:
            main_fuse(resource)
        else:
            run_mirage(main(resource))
