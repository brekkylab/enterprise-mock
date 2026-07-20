#!/usr/bin/env python3
"""Read Confluence through the official atlassian-python-api. Self-contained.

    pip install -e ".[examples]"
    python examples/using-official-sdk/confluence.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/confluence.py --url http://localhost:8000 \
        --username <email> --password <usr-token>   # ACL-filtered to that user
"""
from atlassian import Confluence

from _mockserver import cli_basic_auth, serve_or_connect

CORPUS = [
    {"source_type": "confluence", "space": "handbook", "title": "Engineering Handbook",
     "content": "How we build software: coding standards, review process, on-call."},
    {"source_type": "confluence", "space": "handbook", "title": "On-call Runbook",
     "content": "Respond to gateway 502s: check dashboards, roll back, page on-call."},
]

with serve_or_connect(CORPUS) as mock:
    # --username <email> / --password <usr-token> (from /_mock/users) → ACL-filtered to that
    # user; either identifies them (mock resolves by token, else by username email). Default: admin.
    username, password = cli_basic_auth("svc@example.com", mock.token)
    confluence = Confluence(url=f"{mock.base_url}/atlassian/wiki", username=username, password=password)

    pages = confluence.get("rest/api/content", params={"limit": 5, "expand": "body.storage"})["results"]
    if not pages:
        print("no pages visible to this identity")
    else:
        page = confluence.get_page_by_id(pages[0]["id"], expand="body.storage")
        body = page["body"]["storage"]["value"]
        print(f"{len(pages)} pages; first page:")
        print(f"  title: {page['title']}")
        print(f"  body:  {body[:80]}")
