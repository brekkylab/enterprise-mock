"""Point mirage's connectors at an enterprise-mock server.

Mirage (https://github.com/strukto-ai/mirage) is a virtual filesystem for AI agents: you mount
a SaaS backend and read it with bash-style commands (``ls``, ``cat``, ``grep``, ``find``).
Slack is configured directly: ``SlackConfig(base_url=...)`` points the Slack connector at the
mock, so no monkeypatch is needed for it.

Google has no such knob — its connectors read the API host from module-level constants that the
base helpers (``drive_base``/``gmail_base``/…) return verbatim, ignoring the config:

    mirage.core.google._client.TOKEN_URL       = "https://oauth2.googleapis.com/token"
    mirage.core.google._client.GMAIL_API_BASE  = "https://gmail.googleapis.com/gmail/v1"
    mirage.core.google._client.DRIVE_API_BASE  = "https://www.googleapis.com/drive/v3"
    # native Drive docs are read via the Docs/Sheets/Slides APIs, not Drive export:
    mirage.core.google._client.DOCS_API_BASE   = "https://docs.googleapis.com/v1"
    mirage.core.google._client.SHEETS_API_BASE = "https://sheets.googleapis.com/v4"
    mirage.core.google._client.SLIDES_API_BASE = "https://slides.googleapis.com/v1"

Those constants are also imported *by value* into consuming submodules
(``from ..._client import GMAIL_API_BASE``), so ``point_google_at`` patches both the source
constant (which any module imported later will read) **and** every already-imported module that
carries a copy. That makes the redirect order-independent — call it once before building the
Google resources.

This module also re-exports the serve/credential helpers from the sibling
``using-official-sdk/_mockserver.py`` so the mirage scripts share the same ``--url`` /
``--user`` / ``--token`` behavior and the same local-fallback mock.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Reuse the official-SDK examples' mock plumbing rather than duplicating it.
_SDK_DIR = Path(__file__).resolve().parent.parent / "using-official-sdk"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))

from _mockserver import (  # noqa: E402
    google_oauth_user,
    serve_or_connect,
)

# aiohttp (mirage's HTTP client) builds a *verified* SSL context once, at import, and caches it
# in `aiohttp.connector._SSL_CONTEXT_VERIFIED`. On macOS that context has no CA bundle unless
# SSL_CERT_FILE was set before aiohttp was imported — so if `mirage` (hence aiohttp) was
# imported before this module, an HTTPS `--url` (e.g. the ACM-fronted deployment) fails with
# CERTIFICATE_VERIFY_FAILED. `_mockserver` sets SSL_CERT_FILE for urllib/requests; here we also
# load certifi's CAs straight into aiohttp's cached context, so the redirect works regardless of
# import order.
try:  # best-effort; env-var path still applies if internals change
    import certifi as _certifi
    from aiohttp import connector as _aiohttp_connector

    _aiohttp_connector._SSL_CONTEXT_VERIFIED.load_verify_locations(_certifi.where())
except Exception:  # noqa: BLE001
    pass

__all__ = ["point_google_at", "slack_base_url", "notion_base_url", "s3_base_url",
           "serve_or_connect", "google_oauth_user", "lines", "run_mirage", "FUSE_HELP"]


def slack_base_url(base_url: str) -> str:
    """The mock's Slack Web API base, for ``SlackConfig(base_url=...)``."""
    return f"{base_url.rstrip('/')}/slack/api"


def notion_base_url(base_url: str) -> str:
    """The mock's Notion API base, for ``NotionConfig(base_url=...)``.

    mirage's Notion connector hits ``{base_url}/<endpoint>`` under the REST ``/v1`` namespace, so
    point it at the mock's ``/notion/v1``. Like Slack (and unlike Google) the host is a plain
    config field — no monkeypatch needed. mirage sends ``Notion-Version: 2022-06-28``, which the
    mock's version-aware router serves (the legacy inline-properties / ``databases.query`` shape)."""
    return f"{base_url.rstrip('/')}/notion/v1"


def s3_base_url(base_url: str) -> str:
    """The mock's S3 endpoint, for ``S3Config(endpoint_url=...)``. Path-style: the bucket is the
    first path segment under ``/s3`` (S3Config(path_style=True) keeps it out of the hostname)."""
    return f"{base_url.rstrip('/')}/s3"


# Message the examples show when a --fuse run can't mount (missing mfusepy or OS FUSE driver),
# so they exit with guidance instead of a traceback. Format with the caught exception as {err}.
FUSE_HELP = (
    "FUSE mount unavailable ({err}).\n"
    "  1. pip install -e '.[mirage]'   (installs mirage-ai[fuse] → mfusepy)\n"
    "  2. install the OS FUSE driver: macFUSE (macOS, https://macfuse.io) or fuse3 (Linux).\n"
    "  Then re-run with --fuse. Without --fuse the example runs in-process (no driver needed)."
)


def lines(text: str) -> list[str]:
    """Split ``ls`` output into entries by line — names can contain spaces, so never ``split()``."""
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()]


def run_mirage(coro):
    """Run a mirage coroutine with HTTP connection reuse — a big win against a remote ``--url``.

    mirage opens a fresh ``aiohttp.ClientSession`` (hence a new TCP + TLS handshake) for *every*
    API call. Over a remote HTTPS hop that handshake dominates (~0.85s/call here), and mirage
    makes many calls (pagination, Gmail's per-message fetch). We inject one shared keep-alive
    connector so those calls reuse pooled connections — ~3x fewer round-trip stalls end to end.
    Harmless locally. The connector is created and closed inside this one event loop, so there's
    no cross-loop binding and no "unclosed connector" warning.
    """
    import aiohttp

    async def _run():
        shared = aiohttp.TCPConnector(limit=32, keepalive_timeout=60, ttl_dns_cache=300)
        original_init = aiohttp.ClientSession.__init__

        def _init(self, *args, **kwargs):  # route every session through the shared connector
            if kwargs.get("connector") is None:
                kwargs["connector"] = shared
                kwargs["connector_owner"] = False  # a per-call session must not close the pool
            original_init(self, *args, **kwargs)

        aiohttp.ClientSession.__init__ = _init
        try:
            return await coro
        finally:
            aiohttp.ClientSession.__init__ = original_init
            await shared.close()

    return asyncio.run(_run())


def point_google_at(base_url: str) -> None:
    """Redirect mirage's Google connectors (Gmail/Drive/Docs/Sheets/Slides + OAuth) at the mock.

    Google exposes no host config (unlike Slack's ``base_url``), so we patch the ``_client``
    constants directly. Idempotent and order-independent: patches the source constants and every
    already-imported module that copied one. Call once, before constructing the Google resources.
    """
    base = base_url.rstrip("/")

    # value each hardcoded constant name should take against the mock
    overrides = {
        "TOKEN_URL": f"{base}/oauth2/token",
        "GMAIL_API_BASE": f"{base}/gmail/v1",
        "DRIVE_API_BASE": f"{base}/drive/v3",
        "DOCS_API_BASE": f"{base}/docs/v1",
        "SHEETS_API_BASE": f"{base}/sheets/v4",
        "SLIDES_API_BASE": f"{base}/slides/v1",
    }

    # Ensure the source-of-truth module exists so late by-value imports read the patched value.
    import mirage.core.google._client  # noqa: F401

    # Patch the source constants + any mirage.core.* module already holding a copy.
    for mod in list(sys.modules.values()):
        if not getattr(mod, "__name__", "").startswith("mirage.core."):
            continue
        for const, value in overrides.items():
            if hasattr(mod, const):
                setattr(mod, const, value)
