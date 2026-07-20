#!/usr/bin/env python3
"""Read GitHub through the official PyGithub. Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/github.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/github.py --url http://localhost:8000 --token <usr-token>
"""
import sys
from pathlib import Path

from _mockserver import cli_token, serve_or_connect

# This file is named github.py, so its own directory would shadow PyGithub's `github`
# package. Drop that directory now that the local helper is imported.
_here = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != Path(_here)]

from github import Auth, Github  # noqa: E402

CORPUS = [
    {"source_type": "github", "repo": "gateway", "title": "Rate limiter drops bursts under 50ms",
     "content": "The token-bucket refill is off by one tick.", "subtype": "issue"},
    {"source_type": "github", "repo": "gateway", "title": "Fix token-bucket refill off-by-one",
     "content": "Corrects the refill tick; adds a regression test.", "subtype": "pull_request"},
]

with serve_or_connect(CORPUS) as mock:
    # --token <usr-token> (from /_mock/users) → ACL-filtered to that user; else admin sees all
    gh = Github(auth=Auth.Token(cli_token(mock.token)), base_url=f"{mock.base_url}/github")

    # the repo owner is echoed back by the mock (it doesn't own an org concept), so any org works
    repos = list(gh.get_organization("acme").get_repos())
    if not repos:
        print("no repos visible to this identity")
    else:
        repo = gh.get_repo(f"acme/{repos[0].name}")
        issues = list(repo.get_issues(state="all")[:5])
        print(f"{len(repos)} repos; {repo.name} has these issues/PRs:")
        for issue in issues:
            kind = "PR" if issue.pull_request else "issue"
            print(f"  - #{issue.number} ({kind}) {issue.title}")
