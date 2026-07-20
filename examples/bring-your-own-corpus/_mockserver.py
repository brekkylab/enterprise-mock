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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _url_from_argv(argv: list[str] | None = None) -> str | None:
    """The value of ``--url`` / ``--url=…`` on the command line (or None)."""
    argv = sys.argv[1:] if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--url" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--url="):
            return a.split("=", 1)[1]
    return None


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


@contextlib.contextmanager
def serve_or_connect(records: list[dict], url: str | None = None):
    """Use a ``--url`` mock if reachable; otherwise spin up a local one on ``records``."""
    url = (url or _url_from_argv() or "").strip()
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
