#!/usr/bin/env python3
"""Read a GitHub repo's code through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts a repo's git file tree as a filesystem — read it with plain ``ls`` / ``cat`` /
``grep``, same as the S3/Notion examples. Unlike Slack/Notion/S3, ``GitHubConfig`` (mirage 0.0.3)
has no ``base_url`` knob: the connector hardcodes ``mirage.core.github._client.API_BASE =
"https://api.github.com"``, so ``point_github_at`` monkeypatches that module constant before the
resource is built (mirrors ``point_google_at``'s approach for Google — see ``_mirage.py``).

mirage's GitHub connector only mirrors the *file tree* (git ``trees``/``blobs``), not issues/PRs —
use `examples/using-official-sdk/github.py` for those.

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/github.py                                  # local throwaway mock
    python examples/using-mirage/github.py --url http://localhost:8000
    python examples/using-mirage/github.py --url http://localhost:8000 --token <usr-token>
    python examples/using-mirage/github.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the tree is exposed as an actual filesystem (needs macFUSE/fuse3) and read with
plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""
import argparse
import os
import subprocess

from mirage import MountMode, Workspace
from mirage.resource.github import GitHubConfig, GitHubResource

from _mirage import FUSE_HELP, lines, point_github_at, run_mirage, serve_or_connect

OWNER = "acme"  # the mock echoes back whatever owner is asked for; any org works
REPO = "gateway"
CORPUS = [
    {"source_type": "github", "repo": REPO, "title": "Rate limiter drops bursts under 50ms",
     "content": "The token-bucket refill is off by one tick.", "subtype": "issue"},
    {"source_type": "github", "repo": REPO, "subtype": "file", "path": "README.md",
     "title": "README.md", "content": "# gateway\n\nToken-bucket rate limiter for inbound requests.\n"},
    {"source_type": "github", "repo": REPO, "subtype": "file", "path": "src/ratelimiter.py",
     "title": "ratelimiter.py",
     "content": "class TokenBucket:\n"
                "    def __init__(self, rate, burst):\n"
                "        self.rate = rate\n"
                "        self.tokens = burst\n\n"
                "    def refill(self, elapsed):\n"
                "        # BUG: off-by-one tick drops the last burst token\n"
                "        self.tokens = min(self.tokens + elapsed * self.rate, self.tokens)\n"},
    {"source_type": "github", "repo": REPO, "subtype": "file", "path": "src/utils/tokens.py",
     "title": "tokens.py",
     "content": "def clamp(value, low, high):\n"
                "    return max(low, min(value, high))\n"},
]


def build(mock, token):
    # GitHubConfig has no base_url field — redirect the hardcoded API_BASE constant first, then
    # construct the resource (its __init__ makes synchronous HTTP calls to fetch the default
    # branch and the recursive tree).
    point_github_at(mock.base_url)
    return GitHubResource(GitHubConfig(token=token, owner=OWNER, repo=REPO, ref="main"))


async def main(resource) -> None:
    ws = Workspace({"/github": resource}, mode=MountMode.READ)

    print("=== ls /github/ ===")
    print((await (await ws.execute("ls /github/")).stdout_str()).rstrip())

    print("\n=== ls /github/src/ ===")
    print((await (await ws.execute("ls /github/src/")).stdout_str()).rstrip())

    cat_path = "/github/src/ratelimiter.py"
    print(f"\n$ cat {cat_path}")
    print((await (await ws.execute(f'cat "{cat_path}"')).stdout_str()).rstrip())

    print("\n$ grep -r BUG /github/")
    print((await (await ws.execute("grep -r BUG /github/")).stdout_str()).rstrip())


def main_fuse(resource) -> None:
    """--fuse: mount the repo tree as a *real* filesystem, then read it with ordinary tools."""
    try:
        with Workspace({"/github": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/github")  # "/github" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            path = f"{mnt}/src/ratelimiter.py"
            print("\n$ head -c 200 src/ratelimiter.py")
            print("  " + open(path).read(200).replace("\n", " "))  # a genuine open() via FUSE
            count = subprocess.run(["grep", "-rc", "BUG", mnt], capture_output=True, text=True)
            print(f"\n$ grep -rc BUG {mnt}   # a separate process reads the mount → {count.stdout.strip()}")
            print(f"\nexplore it live in another terminal:  ls -R {mnt}")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read a GitHub repo's code through mirage against the mock.")
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument("--token", help="mock bearer token from GET /_mock/users "
                                   "(default: the admin token, which sees everything)")
    p.add_argument("--fuse", action="store_true", help="mount as a real FUSE filesystem (needs macFUSE/fuse3)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        resource = build(mock, args.token or mock.token)
        if args.fuse:
            main_fuse(resource)
        else:
            run_mirage(main(resource))
