#!/usr/bin/env python3
"""Read S3 through the official AWS SDK (boto3). Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/s3.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/s3.py --url http://localhost:8000 --token <usr-token>

The only changes from talking to real S3 are ``endpoint_url`` (point it at the mock's ``/s3``) and
path-style addressing (so the bucket stays in the path, not the hostname). boto3 SigV4-signs every
request; the mock verifies it against the access-key/secret derived from your bearer token.
"""
import boto3
from botocore.config import Config

from _mockserver import cli_token, s3_credentials, serve_or_connect

CORPUS = [
    {"source_type": "s3", "bucket": "eng-artifacts", "key": "runbooks/oncall.md",
     "title": "On-call Runbook", "content": "# On-call\nCheck dashboards, roll back, page on-call.",
     "content_type": "text/markdown"},
    {"source_type": "s3", "bucket": "eng-artifacts", "key": "design/architecture.md",
     "title": "Architecture", "content": "Gateway, workers, and the token bucket.",
     "content_type": "text/markdown"},
]

with serve_or_connect(CORPUS) as mock:
    # --token <usr-token> (from /_mock/users) → ACL-filtered to that user; else admin sees all
    token = cli_token(mock.token)
    ak, sk = s3_credentials(token)
    s3 = boto3.client("s3", endpoint_url=f"{mock.base_url}/s3",
                      aws_access_key_id=ak, aws_secret_access_key=sk, region_name="us-east-1",
                      config=Config(s3={"addressing_style": "path"}))

    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    print(f"buckets → {buckets}")

    for b in buckets:
        objs = s3.list_objects_v2(Bucket=b).get("Contents", [])
        print(f"\ns3://{b}/ → {len(objs)} object(s):")
        for o in objs:
            print(f"  - {o['Key']}  ({o['Size']} bytes, {o['ETag']})")

    if buckets:
        first = s3.list_objects_v2(Bucket=buckets[0]).get("Contents", [])
        if first:
            key = first[0]["Key"]
            body = s3.get_object(Bucket=buckets[0], Key=key)["Body"].read().decode()
            print(f"\nget s3://{buckets[0]}/{key}:\n  " + body.replace("\n", "\n  "))
            head = s3.head_object(Bucket=buckets[0], Key=key)
            print(f"\nhead → {head['ContentType']}, {head['ContentLength']} bytes")
            part = s3.get_object(Bucket=buckets[0], Key=key, Range="bytes=0-6")["Body"].read()
            print(f"range bytes=0-6 → {part!r}")
