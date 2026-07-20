"""Auth helpers shared by the vendor routers.

Each vendor carries credentials differently (Slack bearer/query token, Google/GitHub
bearer, Atlassian Basic email:api_token). These helpers extract the raw token, resolve
it to a :class:`~app.acl.Caller` via the app's ACL, and compute the caller's visible
principal set. Error *shaping* (Slack's ``ok:false`` vs a real 401) stays in the routers.
"""
from __future__ import annotations

import base64
import sqlite3

from fastapi import Request

from app.acl import Acl, Caller


def conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def acl(request: Request) -> Acl:
    return request.app.state.acl


def _authorization(request: Request) -> str | None:
    return request.headers.get("authorization")


def bearer_token(request: Request) -> str | None:
    """Parse ``Authorization: Bearer <t>`` or GitHub's legacy ``token <t>``."""
    hdr = _authorization(request)
    if not hdr:
        return None
    parts = hdr.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
        return parts[1].strip()
    return None


def basic_password(request: Request) -> tuple[str | None, str | None]:
    """Parse ``Authorization: Basic base64(user:pass)`` -> (user, pass)."""
    hdr = _authorization(request)
    if not hdr:
        return None, None
    parts = hdr.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "basic":
        try:
            decoded = base64.b64decode(parts[1]).decode("utf-8", "replace")
            user, _, pw = decoded.partition(":")
            return user, pw
        except (ValueError, UnicodeDecodeError):
            return None, None
    return None, None


def slack_token(request: Request) -> str | None:
    """Slack accepts the token as a bearer header, query param, or form field. The official
    slack-go SDK (and Slack's own clients) post it as the ``token`` form field, so fall back to
    the form stashed on ``request.state._form`` by the slack-form middleware."""
    form = getattr(request.state, "_form", None)
    form_field = form.get("token") if form else None
    return bearer_token(request) or request.query_params.get("token") or form_field


def resolve_bearer(request: Request) -> Caller | None:
    return acl(request).resolve(bearer_token(request))


def resolve_basic(request: Request) -> Caller | None:
    """Atlassian: resolve by the api_token (password); fall back to the username email."""
    a = acl(request)
    user, pw = basic_password(request)
    caller = a.resolve(pw)
    if caller is not None:
        return caller
    # allow username=email as an identity shortcut (mock convenience)
    if user and "@" in user:
        from app import store
        if store.get_user(conn(request), user):
            return Caller(email=user, is_admin=False)
    return None


def visible_ids(request: Request, caller: Caller) -> set[str] | None:
    return acl(request).visible_ids(conn(request), caller)
