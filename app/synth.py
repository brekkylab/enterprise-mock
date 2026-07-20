"""Deterministic synthesis of structural metadata.

The published dataset only carries ``{doc_id, source_type, title, content}``. Every
structural field a real API returns (ids, timestamps, users, keys, ...) is derived
here from ``sha256(doc_id)`` so responses are stable and self-consistent across calls
and across paginated fetches.

All functions are pure and depend only on their arguments.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone

BASE_EPOCH = 1_672_531_200  # 2023-01-01T00:00:00Z
TIME_RANGE = 63_072_000  # ~2 years


def _digest(doc_id: str) -> str:
    return hashlib.sha256(doc_id.encode("utf-8")).hexdigest()


def hnum(doc_id: str, start: int = 0, length: int = 8, salt: str = "") -> int:
    """A stable non-negative integer derived from a hex slice of the digest."""
    h = _digest(salt + doc_id) if salt else _digest(doc_id)
    start %= 64
    return int(h[start : start + length] or h[:length], 16)


def pick(doc_id: str, seq, salt: str = ""):
    """Deterministically choose one element of ``seq`` for this doc."""
    seq = list(seq)
    if not seq:
        return None
    return seq[hnum(doc_id, salt=salt) % len(seq)]


# --- timestamps -----------------------------------------------------------------

def epoch(doc_id: str, base: int = BASE_EPOCH, span: int = TIME_RANGE) -> int:
    """Stable unix-second timestamp within [base, base+span)."""
    return base + (hnum(doc_id, 0, 8) % span)


def rfc3339(ts: int) -> str:
    """e.g. 2024-04-05T17:00:00Z (Drive / GitHub / Confluence)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rfc3339_millis(ts: int) -> str:
    """e.g. 2024-04-05T17:00:00.000Z (Confluence version.when)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jira_datetime(ts: int) -> str:
    """e.g. 2024-04-05T17:00:00.000+0000 (Jira)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def rfc2822(ts: int) -> str:
    """e.g. Fri, 05 Apr 2024 17:00:00 +0000 (Gmail Date header)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


# --- per-vendor identifiers -----------------------------------------------------

def slack_channel_id(channel_name: str) -> str:
    """Stable ``C…`` id keyed on the channel name (shared by all docs in it)."""
    h = _digest("chan:" + channel_name)
    return "C" + h[:10].upper()


def slack_user_id(email: str) -> str:
    h = _digest("user:" + email)
    return "U" + h[:10].upper()


def slack_fmt_ts(epoch_sec: int, key: str) -> str:
    """Format a Slack ts ``<epoch>.<6 digits>`` for a given second, with the
    micro-fraction keyed on ``key`` so every message in a thread shares it."""
    micro = hnum(key, 12, 6) % 1_000_000
    return f"{int(epoch_sec)}.{micro:06d}"


def slack_ts(doc_id: str) -> str:
    """Slack message id == timestamp: ``<epoch>.<6 digits>`` (unique per doc)."""
    return slack_fmt_ts(epoch(doc_id), doc_id)


def slack_thread_ts(root_doc_id: str, seq: int) -> str:
    """ts for a message in a thread: root (seq 0) equals ``slack_ts(root)``; each
    reply is ``seq`` seconds later, so replies sort after the root and share the
    root's ts as their thread_ts."""
    return slack_fmt_ts(epoch(root_doc_id) + int(seq), root_doc_id)


def gmail_id(doc_id: str, salt: str = "msg") -> str:
    return hnum(doc_id, salt=salt, length=16).__format__("016x")


def drive_file_id(doc_id: str) -> str:
    # Drive ids are opaque; reuse the doc_id so the id is reversible for get/export.
    return doc_id


def drive_folder_id(container: str) -> str:
    return "0A" + _digest("folder:" + container)[:26]


def github_number(doc_id: str) -> int:
    return hnum(doc_id, 0, 8) % 90_000 + 1


def jira_numeric_id(doc_id: str) -> int:
    return 10_000 + hnum(doc_id, 8, 8) % 900_000


def jira_key(doc_id: str, project_key: str) -> str:
    return f"{project_key}-{hnum(doc_id, 16, 6) % 9000 + 1}"


def confluence_id(doc_id: str) -> int:
    return 100_000 + hnum(doc_id, 24, 8) % 9_000_000


def atlassian_account_id(email: str) -> str:
    return "5b" + _digest("acct:" + email)[:22]


def github_login(email: str) -> str:
    return email.split("@", 1)[0].replace(".", "-")


def github_user_id(email: str) -> int:
    return 1000 + int(_digest("ghid:" + email)[:6], 16) % 9_000_000


def node_id(kind: str, num) -> str:
    """A GitHub-style base64 GraphQL global node id, e.g. ``MDU6SXNzdWUx``.
    Deterministic and opaque — enough for a v4-id-keyed connector to have *a* stable id."""
    return base64.b64encode(f"012:{kind}{num}".encode()).decode().rstrip("=")


def github_avatar(user_id: int) -> str:
    return f"https://avatars.githubusercontent.com/u/{user_id}?v=4"


def avatar_urls(account_id: str) -> dict:
    """Atlassian-style avatarUrls map (four square sizes)."""
    base = f"https://avatar.example.com/{account_id}"
    return {f"{s}x{s}": f"{base}?size={s}" for s in (48, 24, 16, 32)}


def _key(container: str, fallback: str) -> str:
    """A realistic project/space key: word initials, but always >= 2 chars.

    Multi-word containers use initials (``customer-support`` -> ``CS``); single-word ones
    take the first letters (``payments`` -> ``PAY``), since real Jira/Confluence keys — and
    strict clients like mcp-atlassian — reject single-character keys.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", container.lower()) if w]
    initials = "".join(w[0] for w in words).upper()
    if len(initials) >= 2:
        return initials
    if words:
        return words[0][:3].upper()
    return fallback


def jira_project_key(container: str) -> str:
    return _key(container, "PROJ")


def confluence_space_key(container: str) -> str:
    return _key(container, "SPACE")

