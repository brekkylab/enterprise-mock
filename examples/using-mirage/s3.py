#!/usr/bin/env python3
"""Read S3 through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts an S3 bucket as a filesystem under ``/s3`` — read it with plain ``ls`` / ``cat`` /
``grep``. S3's endpoint is a config knob (``S3Config(endpoint_url=..., path_style=True)``), so we
point it straight at the mock — no monkeypatch (unlike Google). mirage/aioboto3 SigV4-signs every
request. S3 uses an AWS access-key/secret pair (not a bearer token): pass ``--access-key`` /
``--secret-key``, or omit them to pull a pair from ``GET /_mock/users`` (``--user <email>``, else
admin).

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/s3.py                              # local throwaway mock
    python examples/using-mirage/s3.py --url http://localhost:8000
    python examples/using-mirage/s3.py --url http://localhost:8000 --access-key <AKIA...> --secret-key <secret>
    python examples/using-mirage/s3.py --url http://localhost:8000 --user <email>
    python examples/using-mirage/s3.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the tree is exposed as an actual filesystem (needs macFUSE/fuse3) and read with
plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""
import os
import subprocess
import sys

from mirage import MountMode, Workspace
from mirage.resource.s3 import S3Config, S3Resource

from _mirage import (FUSE_HELP, lines, run_mirage, s3_base_url,
                     s3_credentials, serve_or_connect)

BUCKET = "eng-artifacts"
CORPUS = [
    {"source_type": "s3", "bucket": BUCKET, "key": "runbooks/oncall.md", "title": "On-call Runbook",
     "content": "# On-call\nCheck dashboards, roll back, page on-call.", "content_type": "text/markdown"},
    {"source_type": "s3", "bucket": BUCKET, "key": "design/architecture.md", "title": "Architecture",
     "content": "Gateway, workers, and the token bucket.", "content_type": "text/markdown"},
]


def build(mock):
    # AWS keypair from --access-key/--secret-key, else fetched from /_mock/users (--user or admin)
    ak, sk = s3_credentials(mock.base_url)
    return S3Resource(S3Config(bucket=BUCKET, endpoint_url=s3_base_url(mock.base_url),
                               path_style=True, region="us-east-1",
                               aws_access_key_id=ak, aws_secret_access_key=sk))


async def main(resource) -> None:
    ws = Workspace({"/s3": resource}, mode=MountMode.READ)
    print("=== ls /s3/ (recursive) ===")
    print((await (await ws.execute("ls /s3/")).stdout_str()).rstrip())
    for entry in lines(await (await ws.execute("ls /s3/runbooks/")).stdout_str()):
        path = f"/s3/runbooks/{entry.rstrip('/')}"
        print(f"\n$ cat {path}")
        print((await (await ws.execute(f'cat "{path}"')).stdout_str()).rstrip()[:400])
    print("\n$ grep -r dashboards /s3/")
    print((await (await ws.execute("grep -r dashboards /s3/")).stdout_str()).rstrip())


def main_fuse(resource) -> None:
    """--fuse: mount the S3 bucket as a *real* filesystem, then read it with ordinary tools."""
    try:
        with Workspace({"/s3": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/s3")  # "/s3" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            objs = [os.path.join(r, f) for r, _dirs, files in os.walk(mnt) for f in files]
            for p in objs:
                print("  " + p.replace(mnt, "/s3"))
            if objs:
                # a genuinely separate process reads the FUSE mount like any real directory
                hit = subprocess.run(["grep", "-rc", "dashboards", mnt], capture_output=True, text=True)
                print(f"\n$ grep -rc dashboards {mnt}   # a separate process reads the mount → "
                      f"{hit.stdout.strip()}")
            print(f"\nexplore it live in another terminal:  ls -R {mnt}")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


if __name__ == "__main__":
    with serve_or_connect(CORPUS) as mock:
        resource = build(mock)
        if "--fuse" in sys.argv:
            main_fuse(resource)
        else:
            run_mirage(main(resource))
