#!/usr/bin/env python3
"""Read Jira through the official atlassian-python-api. Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/jira.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/jira.py --url http://localhost:8000 \
        --username <email> --password <usr-token>   # ACL-filtered to that user
"""
from atlassian import Jira

from _mockserver import cli_basic_auth, serve_or_connect

CORPUS = [
    {"source_type": "jira", "project": "payments", "title": "SEV2: checkout latency spike",
     "content": "p95 checkout latency jumped to 2.1s after the payments migration.",
     "status": "In Progress", "issuetype": "Incident", "priority": "High"},
    {"source_type": "jira", "project": "payments", "title": "Write the postmortem",
     "content": "Draft the postmortem and action items.", "status": "To Do"},
]

with serve_or_connect(CORPUS) as mock:
    # --username <email> / --password <usr-token> (from /_mock/users) → ACL-filtered to that
    # user; either identifies them (mock resolves by token, else by username email). Default: admin.
    username, password = cli_basic_auth("svc@example.com", mock.token)
    jira = Jira(url=f"{mock.base_url}/atlassian", username=username, password=password)

    issues = jira.get("rest/api/3/search/jql", params={"maxResults": 5})["issues"]
    if not issues:
        print("no issues visible to this identity")
    else:
        issue = jira.get(f"rest/api/3/issue/{issues[0]['key']}")
        print(f"{len(issues)} issues; first issue:")
        print(f"  {issue['key']}: {issue['fields']['summary']}")
        print(f"  status: {issue['fields']['status']['name']}")
