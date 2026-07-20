#!/usr/bin/env python3
"""Read Slack through the official slack_sdk. Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/slack.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/slack.py --url http://localhost:8000 --token <usr-token>
"""
from slack_sdk import WebClient

from _mockserver import cli_token, serve_or_connect

CORPUS = [
    {"source_type": "slack", "channel": "eng", "content": "Deploy freeze starts Friday 5pm."},
    {"source_type": "slack", "channel": "incidents", "content": "Anyone seeing 502s from the gateway?",
     "replies": [{"content": "Looking now."}, {"content": "Rolled back — clearing up."}]},
]

with serve_or_connect(CORPUS) as mock:
    # --token <usr-token> (from /_mock/users) → ACL-filtered to that user; else admin sees all
    client = WebClient(token=cli_token(mock.token), base_url=f"{mock.base_url}/slack/api/")

    channels = client.conversations_list()["channels"]
    if not channels:
        print("no channels visible to this identity")
    else:
        channel = channels[0]
        messages = client.conversations_history(channel=channel["id"], limit=10)["messages"]
        print(f"{len(channels)} channels; #{channel['name']} has these recent messages:")
        for m in messages:
            print(f"  - {m['text'].splitlines()[0]}")
