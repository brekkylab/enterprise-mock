#!/usr/bin/env python3
"""Drive the awslabs aws-api MCP server against the mock's S3. Self-contained.

Runs `awslabs.aws-api-mcp-server` via **uvx** (Python) pointed at the mock's `/s3`, then lets an
LLM agent answer a question by calling its MCP tools (which shell the AWS CLI). The CLI's boto3
client honors a first-class `AWS_ENDPOINT_URL` override and SigV4-signs every call, so pointing it
at the mock is a handful of env vars — no Docker/host-gateway tricks.

S3 authenticates with an AWS access-key/secret pair (not a bearer token): pass `--access-key` /
`--secret-key` directly, or omit them to pull a pair from `GET /_mock/users` (`--user <email>` for
a specific user, else the admin keypair).

Prereqs: uvx (Astral `uv`); `pip install -e ".[mcp]"`; an LLM key for `--agent` (`ANTHROPIC_API_KEY`,
or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/s3.py \
        [--url … --access-key <AKIA…> --secret-key <secret> --agent openai]
"""
from __future__ import annotations

from mcp import StdioServerParameters

from _agent import run_agent
from _mockserver import cli_arg, s3_credentials, serve_or_connect

CORPUS = [
    {"source_type": "s3", "bucket": "payments", "key": "incidents/sev2.md",
     "title": "SEV2: checkout latency spike",
     "content": "p95 checkout latency jumped to 2.1s after the payments migration; rolling back."},
    {"source_type": "s3", "bucket": "runbooks", "key": "oncall/checkout.md",
     "title": "On-call Runbook: checkout latency & bad deploys",
     "content": "When a deploy or migration spikes checkout latency: check the payments "
                "dashboards, roll back the last change, and page the on-call engineer."},
]
QUESTION = ("Find the incident about checkout latency and summarize it, then find the on-call "
            "runbook. Cite the titles.")


def build_params(base_url: str, access_key: str, secret_key: str) -> StdioServerParameters:
    """`uvx` args pointing the awslabs aws-api MCP server at the mock via AWS_ENDPOINT_URL.
    READ_OPERATIONS_ONLY keeps it read-only."""
    return StdioServerParameters(
        command="uvx", args=["awslabs.aws-api-mcp-server@latest"],
        env={"AWS_ENDPOINT_URL": f"{base_url.rstrip('/')}/s3",
             "AWS_ACCESS_KEY_ID": access_key, "AWS_SECRET_ACCESS_KEY": secret_key,
             "AWS_REGION": "us-east-1", "READ_OPERATIONS_ONLY": "true"})


if __name__ == "__main__":
    with serve_or_connect(CORPUS) as mock:
        ak, sk = s3_credentials(mock.base_url)  # --access-key/--secret-key, or fetched from /_mock/users
        run_agent(cli_arg("agent"), build_params(mock.base_url, ak, sk), QUESTION)
