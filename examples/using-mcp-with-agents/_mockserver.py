"""Point an example at a mock server — a given ``--url`` if reachable, else a local one.

``serve_or_connect(records)`` is the entry point: if ``--url <URL>`` is on the command line
and that server's ``/health`` responds, the example talks to it directly; otherwise it falls
back to ``mock_server``, which builds a throwaway DB from ``records`` and runs ``uvicorn``
against it — so the example stays self-contained with no separate process to launch.

    from _mockserver import serve_or_connect

    CORPUS = [{"source_type": "slack", "content": "hi"}]
    with serve_or_connect(CORPUS) as mock:
        ...  # point an SDK at mock.base_url, using mock.token (admin: sees everything)
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve()
while not (ROOT / "pyproject.toml").exists():
    ROOT = ROOT.parent
TOKEN = "admin-service-token"  # Settings default; a per-user token is in <data>/tokens.yaml

# Point OpenSSL at certifi's CA bundle so an HTTPS `--url` (e.g. a real deployment behind an
# ACM cert) verifies — macOS Python's default context often has no CA file. certifi ships with
# the [mcp]/[examples] extras.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def s3_credentials(base_url: str, access_key: str | None = None,
                   secret_key: str | None = None, user: str | None = None) -> tuple[str, str]:
    """Resolve the (access_key_id, secret_access_key) an S3 client should use — S3 authenticates
    with an AWS keypair, not a bearer token.

    An explicit ``access_key`` + ``secret_key`` win. Otherwise a pair is fetched from
    ``GET {base_url}/_mock/users``: ``user`` (an email) picks that user's keys (responses are
    ACL-filtered to them), else the admin keypair (sees everything). The keys shown there are what
    the mock's SigV4 verifier accepts."""
    if access_key and secret_key:
        return access_key, secret_key
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/users") as r:
        data = json.load(r)
    if user:
        who = next((u for u in data["users"] if u["email"] == user), None)
        if who is None:
            raise SystemExit(f"--user {user!r} not found in /_mock/users")
        return who["s3_access_key_id"], who["s3_secret_access_key"]
    return data["admin_s3_access_key_id"], data["admin_s3_secret_access_key"]


def _healthy(url: str) -> bool:
    # generous timeout: a remote deployment may be a trans-continental HTTPS hop
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=10) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


@contextlib.contextmanager
def serve_or_connect(records: list[dict], url: str | None = None):
    """Use the given ``url`` mock if reachable; otherwise spin up a local one on ``records``."""
    url = (url or "").strip()
    if url and _healthy(url):
        print(f"using mock server at {url}")
        yield types.SimpleNamespace(base_url=url.rstrip("/"), token=TOKEN, data_dir=None)
        return
    if url:
        print(f"--url {url!r} is not reachable — falling back to a local mock")
    with mock_server(records) as mock:
        yield mock


@contextlib.contextmanager
def mock_server(records: list[dict]):
    with tempfile.TemporaryDirectory() as data_dir:
        corpus = Path(data_dir) / "corpus.jsonl"
        corpus.write_text("\n".join(json.dumps(r) for r in records))
        env = {**os.environ, "MOCK_DATA_DIR": data_dir}
        subprocess.run([sys.executable, "-m", "app.importer.byo", str(corpus)],
                       cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f"{base}/health", timeout=0.5) as r:
                        if r.status == 200:
                            break
                except Exception:  # noqa: BLE001
                    time.sleep(0.1)
            else:
                raise RuntimeError("mock server did not become ready")
            yield types.SimpleNamespace(base_url=base, token=TOKEN, data_dir=Path(data_dir))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
