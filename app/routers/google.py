"""Mock Google APIs (read-only): Gmail (``/gmail/v1``), Drive (``/drive/v3``), and the
Workspace editor read APIs — Docs (``/docs/v1``), Sheets (``/sheets/v4``), Slides
(``/slides/v1``) — for clients that read native docs structurally instead of via Drive export.

Client base-URL override: point the Gmail client at ``http://<host>/gmail`` and the
Drive client at ``http://<host>/drive`` (google-api-python-client ``api_endpoint``).
All authenticate with ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import re
from email.parser import BytesParser
from http import HTTPStatus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict

from app import auth, store, synth
from app.acl import Caller
from app.config import get_settings
from app.pagination import decode_cursor, next_page_token

router = APIRouter(tags=["google"])


# --- OpenAPI enrichment (issue #4 bridge) --------------------------------------------------
# Query params are read query-only (via _int/request.query_params); documenting them with
# openapi_extra keeps the handler bodies untouched and merges cleanly with the auto-generated
# path params. Response models use extra="allow" so builders' full field set passes through.

class _GLoose(BaseModel):
    model_config = ConfigDict(extra="allow")


class GmailMessageList(_GLoose):
    messages: list[dict] = []
    resultSizeEstimate: int = 0


class GmailThreadList(_GLoose):
    threads: list[dict] = []
    resultSizeEstimate: int = 0


class GmailMessage(_GLoose):
    id: str


class GmailThread(_GLoose):
    id: str
    messages: list[dict] = []


class GmailAttachment(_GLoose):
    attachmentId: str
    size: int
    data: str


def _gqp(name: str, typ: str = "string", required: bool = False) -> dict:
    return {"name": name, "in": "query", "required": required, "schema": {"type": typ}}


_P_GMAIL_LIST = [_gqp("maxResults", "integer"), _gqp("pageToken"), _gqp("q")]
_P_GMAIL_FORMAT = [_gqp("format")]


class DriveFileList(_GLoose):
    kind: str = "drive#fileList"
    files: list[dict] = []


class DrivePermissionList(_GLoose):
    kind: str = "drive#permissionList"
    permissions: list[dict] = []


# drive_files_get / .export return raw Response/PlainTextResponse on some branches — they get
# openapi_extra params only (no JSON response_model, which would mis-serialize the raw body).
_P_DRIVE_LIST = [_gqp("pageSize", "integer"), _gqp("pageToken"), _gqp("q"), _gqp("fields")]
_P_DRIVE_ALT = [_gqp("alt")]
_P_DRIVE_EXPORT = [_gqp("mimeType", required=True)]

DRIVE_DOC_MIME = "application/vnd.google-apps.document"

# --- Google-style multipart/mixed batch (google-api-python-client BatchHttpRequest) -------------
# The client POSTs one multipart/mixed body to a single batch_uri; each part is an application/http
# sub-request (which carries, or inherits from the outer request, its own Authorization). Google
# runs each and returns a multipart/mixed of application/http sub-responses matched by Content-ID.
# We emulate that by dispatching each sub-request in-process through this app (normal auth + routers)
# and reassembling the response, echoing each Content-ID so the client can pair them.
_BATCH_BOUNDARY = "erb_batch_boundary_9f2a7c"
_BATCH_DROP_HEADERS = {"host", "content-length", "content-transfer-encoding", "connection"}


def _batch_reason(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Status"


def _parse_batch_subrequest(payload: str):
    """An application/http payload -> (method, target, headers, body)."""
    head, sep, body = payload.partition("\r\n\r\n")
    if not sep:
        head, sep, body = payload.partition("\n\n")
    lines = head.strip().splitlines()
    first = (lines[0].split(" ") + ["", ""])[:3] if lines else ["", "", ""]
    method, target = first[0], first[1]
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            if k.strip().lower() not in _BATCH_DROP_HEADERS:
                headers[k.strip()] = v.strip()
    return method, target, headers, body


@router.post("/batch")
@router.post("/batch/{api}/{version}")
async def batch(request: Request, api: str = "", version: str = "") -> Response:
    raw = await request.body()
    ctype = request.headers.get("content-type", "")
    if "multipart/mixed" not in ctype:
        return Response("expected multipart/mixed", status_code=400)
    # the email parser needs the Content-Type (with the boundary) as a header to split the parts
    parsed = BytesParser().parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
    if not parsed.is_multipart():
        return Response("not multipart/mixed", status_code=400)

    import httpx  # lazy: keep httpx out of app-import so a runtime image lacking it degrades only
    #               /batch, not the whole server (it's a test-time dep, not baked into the image)

    # Google applies the outer credential to any sub-request without its own; do the same so a batch
    # authenticates whether the client set per-sub-request auth or only the outer request.
    outer_auth = request.headers.get("authorization")
    transport = httpx.ASGITransport(app=request.app, raise_app_exceptions=False)
    out_parts: list[tuple[str, str]] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://mock.batch") as client:
        for part in parsed.get_payload():
            cid = part.get("Content-ID", "")
            method, target, sub_headers, sub_body = _parse_batch_subrequest(part.get_payload(decode=False))
            if outer_auth and not any(k.lower() == "authorization" for k in sub_headers):
                sub_headers["Authorization"] = outer_auth
            if not method or not target:
                sub_resp = "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nmalformed sub-request"
            else:
                r = await client.request(method, target, headers=sub_headers,
                                         content=sub_body.encode() if sub_body else None)
                sub_resp = (f"HTTP/1.1 {r.status_code} {_batch_reason(r.status_code)}\r\n"
                            f"Content-Type: {r.headers.get('content-type', 'application/json')}\r\n"
                            f"\r\n{r.text}")
            out_parts.append((cid, sub_resp))

    body = ""
    for cid, sub_resp in out_parts:
        body += f"--{_BATCH_BOUNDARY}\r\nContent-Type: application/http\r\n"
        if cid:
            body += f"Content-ID: {cid}\r\n"
        body += "\r\n" + sub_resp + "\r\n"
    body += f"--{_BATCH_BOUNDARY}--\r\n"
    return Response(content=body, media_type=f'multipart/mixed; boundary="{_BATCH_BOUNDARY}"')


def _require(request: Request) -> Caller:
    caller = auth.resolve_bearer(request)
    if caller is None:
        raise HTTPException(status_code=401, detail="Invalid Credentials")
    return caller


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# ================================ Gmail =========================================

def _mailbox_email(caller: Caller, user_id: str) -> str | None:
    """Resolve the mailbox owner email; None means 'all mailboxes' (admin, 'me')."""
    if user_id == "me":
        return caller.email  # None for admin
    return user_id if "@" in user_id else None


def _mailbox_slug(caller: Caller, user_id: str) -> str | None:
    """Resolve the requested mailbox to its container slug (the ``gmail_messages.mailbox`` value
    the importer derived from the owner's name). None = all mailboxes (admin ``me``). A concrete
    address (``me`` for a user, or an explicit email) maps its local-part to the slug so the WHOLE
    mailbox — received and sent — is scoped, not just messages that address happened to author."""
    email = caller.email if user_id == "me" else (user_id if "@" in user_id else None)
    if not email:
        return None
    return re.sub(r"[^a-z0-9]+", "_", email.split("@")[0].lower()).strip("_")


def _service_email(request: Request) -> str:
    """The identity to report for an admin/service caller that has no single mailbox
    (a bare service account / full-crawl token). Real Gmail always reports a concrete
    address here — never the literal ``me`` path segment — so we use the service account's
    email, falling back to a service address on the org domain."""
    oauth = getattr(request.app.state, "oauth", None)
    if oauth is not None and oauth.client_email:
        return oauth.client_email
    return f"service@{get_settings().org_domain}"


@router.get("/gmail/v1/users/{user_id}/profile")
async def gmail_profile(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    # A concrete mailbox (``me`` -> caller.email, or an explicit address) if we have one;
    # otherwise the admin/service identity — never echo the raw ``me`` path segment.
    email = _mailbox_email(caller, user_id) or caller.email or _service_email(request)
    ids = auth.visible_ids(request, caller)
    total = store.count_documents(conn, "gmail",
                                  container=_mailbox_slug(caller, user_id), visible_ids=ids)
    return {"emailAddress": email, "messagesTotal": total, "threadsTotal": total,
            "historyId": "1"}


# The system labels Gmail always exposes (users.labels.list).
_SYSTEM_LABELS = ["INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT",
                  "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_UPDATES",
                  "CATEGORY_FORUMS", "CATEGORY_PROMOTIONS"]


def _label_obj(lid: str, total: int = 0) -> dict:
    hide = lid in ("SPAM", "TRASH", "CHAT")
    return {"id": lid, "name": lid, "type": "system",
            "messageListVisibility": "hide" if hide else "show",
            "labelListVisibility": "labelHide" if lid.startswith("CATEGORY_") else "labelShow",
            "messagesTotal": total, "messagesUnread": 0,
            "threadsTotal": total, "threadsUnread": 0}


@router.get("/gmail/v1/users/{user_id}/labels")
async def gmail_labels(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    total = store.count_documents(conn, "gmail", container=_mailbox_slug(caller, user_id),
                                  visible_ids=ids)
    labels = [_label_obj(lid, total if lid == "INBOX" else 0) for lid in _SYSTEM_LABELS]
    return {"labels": labels}


@router.get("/gmail/v1/users/{user_id}/labels/{label_id}")
async def gmail_label_get(user_id: str, label_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    if label_id not in _SYSTEM_LABELS:
        raise HTTPException(status_code=404, detail="Not Found")
    ids = auth.visible_ids(request, caller)
    total = store.count_documents(conn, "gmail", author_email=_mailbox_email(caller, user_id),
                                  visible_ids=ids)
    return _label_obj(label_id, total if label_id == "INBOX" else 0)


_GMAIL_OP = re.compile(r'(\w+):("[^"]*"|\S+)')
# operators we honor; anything else stays as free text
_GMAIL_KEYS = {"from", "to", "subject", "after", "before", "label", "has"}


def _parse_gmail_q(q: str) -> tuple[str, dict]:
    """Split a Gmail search `q` into (free_text, operators). Honors from:/to:/subject:/
    after:/before:/label:/has: — the rest is free text matched full-text."""
    ops: dict[str, list[str]] = {}

    def _take(m):
        key = m.group(1).lower()
        if key in _GMAIL_KEYS:
            ops.setdefault(key, []).append(m.group(2).strip('"'))
            return " "
        return m.group(0)

    free = re.sub(r"\s+", " ", _GMAIL_OP.sub(_take, q)).strip()
    return free, ops


def _gmail_date(v: str) -> int | None:
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(v, fmt)
                       .replace(tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            continue
    try:
        return int(v)  # epoch seconds
    except ValueError:
        return None


def _gmail_op_match(row, ops: dict) -> bool:
    for v in ops.get("from", []):
        if v.lower() not in (row["author_email"] or "").lower():
            return False
    for v in ops.get("to", []):
        if v.lower() not in (row["to_addr"] or "").lower():
            return False
    for v in ops.get("subject", []):
        if v.lower() not in (row["title"] or "").lower():
            return False
    for v in ops.get("label", []):
        if v.lower() not in [x.lower() for x in store.jcol(row, "label_ids")]:
            return False
    if any(v.lower() == "attachment" for v in ops.get("has", [])) and not store.jcol(row, "attachments"):
        return False
    ts = _gmail_ts(row)
    for v in ops.get("after", []):
        d = _gmail_date(v)
        if d is not None and ts < d:
            return False
    for v in ops.get("before", []):
        d = _gmail_date(v)
        if d is not None and ts >= d:
            return False
    return True


def _gmail_query(conn, mailbox, ids, q: str) -> list:
    """Full ACL+mailbox-filtered match set for a Gmail `q` (FTS-ranked when free text is
    present; otherwise the mailbox listing). The caller paginates the returned rows."""
    free, ops = _parse_gmail_q(q)
    if free:
        # Honor a fully "quoted" free-text term as a phrase (Gmail's quote semantics): match the
        # tokens adjacently AND rank docs literally containing the phrase first, so a grep push-down
        # for e.g. "upload.csv" surfaces the one doc that contains it instead of burying it under
        # coincidental "upload csv" mentions. Unquoted free text stays an AND of terms.
        phrase = len(free) >= 2 and free[0] == '"' and free[-1] == '"'
        term = free[1:-1] if phrase else free
        cand = store.search_documents(conn, term, "gmail", ids, limit=10_000, offset=0,
                                      container=mailbox, phrase=phrase)
    else:
        # No free text. If the query pins a date range (after:/before:), filter created_ts in SQL —
        # a date-dir listing otherwise materialized the whole mailbox (~100k rows) then date-filtered
        # in Python. after: -> ts >= d (inclusive lo), before: -> ts < d (exclusive hi), matching
        # _gmail_op_match; the remaining ops still filter the (now small) candidate set below.
        lo = max((d for v in ops.get("after", []) if (d := _gmail_date(v)) is not None), default=None)
        hi = min((d for v in ops.get("before", []) if (d := _gmail_date(v)) is not None), default=None)
        if lo is not None or hi is not None:
            cand = store.list_gmail_in_range(conn, mailbox, lo, hi, ids, limit=100_000)
        else:
            cand = store.list_documents(conn, "gmail", container=mailbox, visible_ids=ids,
                                        limit=100_000)
    return [r for r in cand if _gmail_op_match(r, ops)]


@router.get("/gmail/v1/users/{user_id}/messages", response_model=GmailMessageList,
            openapi_extra={"parameters": _P_GMAIL_LIST})
async def gmail_messages_list(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    mailbox = _mailbox_slug(caller, user_id)  # container slug (None = all mailboxes)
    limit = _int(request, "maxResults", get_settings().default_page_size)
    offset = decode_cursor(request.query_params.get("pageToken"))
    q = request.query_params.get("q", "") or ""
    if q.strip():  # search: filter the ACL-visible set by the query, then paginate
        matched = _gmail_query(conn, mailbox, ids, q)
        total = len(matched)
        rows = matched[offset:offset + limit]
    else:
        total = store.count_documents(conn, "gmail", container=mailbox, visible_ids=ids)
        rows = store.list_documents(conn, "gmail", container=mailbox, visible_ids=ids,
                                    limit=limit, offset=offset)
    # threadId must agree with messages.get (a reply belongs to its root's thread)
    messages = [{"id": r["doc_id"], "threadId": r["thread_id"] or r["doc_id"]} for r in rows]
    body = {"messages": messages, "resultSizeEstimate": total}
    token = next_page_token(offset, len(rows), total)
    if token:
        body["nextPageToken"] = token
    return body


@router.get("/gmail/v1/users/{user_id}/messages/{msg_id}", response_model=GmailMessage,
            openapi_extra={"parameters": _P_GMAIL_FORMAT})
async def gmail_messages_get(user_id: str, msg_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.get_document(conn, "gmail", msg_id, visible_ids=ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _gmail_message(row, request.query_params.get("format", "full"))


@router.get("/gmail/v1/users/{user_id}/messages/{msg_id}/attachments/{att_id}",
            response_model=GmailAttachment)
async def gmail_attachment(user_id: str, msg_id: str, att_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.get_document(conn, "gmail", msg_id, visible_ids=ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    att = next((a for i, a in enumerate(store.jcol(row, "attachments"))
                if _att_id(msg_id, i) == att_id), None)
    body = (att or {}).get("content", f"attachment {att_id}")
    data = _b64url(body)
    return {"attachmentId": att_id, "size": len(body), "data": data}


@router.get("/gmail/v1/users/{user_id}/threads", response_model=GmailThreadList,
            openapi_extra={"parameters": _P_GMAIL_LIST})
async def gmail_threads_list(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    mailbox = _mailbox_email(caller, user_id)
    limit = _int(request, "maxResults", get_settings().default_page_size)
    offset = decode_cursor(request.query_params.get("pageToken"))
    q = request.query_params.get("q", "") or ""
    if q.strip():
        matched = _gmail_query(conn, mailbox, ids, q)
        total = len(matched)
        rows = matched[offset:offset + limit]
    else:
        total = store.count_documents(conn, "gmail", author_email=mailbox, visible_ids=ids)
        rows = store.list_documents(conn, "gmail", author_email=mailbox, visible_ids=ids,
                                    limit=limit, offset=offset)
    # a thread is keyed by its root; reply rows (thread_seq>0) aren't separate threads
    threads = [{"id": r["thread_id"] or r["doc_id"], "snippet": r["content"][:200], "historyId": "1"}
               for r in rows if (r["thread_seq"] or 0) == 0]
    body = {"threads": threads, "resultSizeEstimate": total}
    token = next_page_token(offset, len(rows), total)
    if token:
        body["nextPageToken"] = token
    return body


@router.get("/gmail/v1/users/{user_id}/threads/{thread_id}", response_model=GmailThread,
            openapi_extra={"parameters": _P_GMAIL_FORMAT})
async def gmail_thread_get(user_id: str, thread_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    msgs = store.gmail_thread(conn, thread_id, visible_ids=ids)
    if not msgs:
        row = store.get_document(conn, "gmail", thread_id, visible_ids=ids)
        if row is None:
            raise HTTPException(status_code=404, detail="Not Found")
        msgs = [row]
    fmt = request.query_params.get("format", "full")
    return {"id": thread_id, "snippet": msgs[0]["content"][:200], "historyId": "1",
            "messages": [_gmail_message(m, fmt) for m in msgs]}


def _att_id(doc_id: str, i: int) -> str:
    return "ANGjdJ" + synth.gmail_id(doc_id, salt=f"att{i}")


def _leaf(mime: str, part_id: str, data: str) -> dict:
    return {"partId": part_id, "mimeType": mime, "filename": "",
            "body": {"size": len(data), "data": _b64url(data)}}


def _gmail_ts(row) -> int:
    """A message's unix ts. A real per-message created_ts (its parsed Date header) is used
    verbatim; only when it's missing do we synthesize a thread base and spread replies an hour
    apart so a thread still reads in order. Both the served Date and the after/before filter use
    this, so they agree."""
    if row["created_ts"]:
        return row["created_ts"]
    return synth.epoch(row["thread_id"] or row["doc_id"]) + (row["thread_seq"] or 0) * 3600


def _gmail_message(row, fmt: str) -> dict:
    ts = _gmail_ts(row)
    author = row["author_email"]
    display = author.split("@")[0].replace(".", " ").title()
    msg_id = row["message_id"] or f"<{row['doc_id']}@{get_settings().org_domain}>"
    # a fetched (received) message carries transport/MIME headers but NOT Bcc (stripped in transit)
    headers = [
        {"name": "Delivered-To", "value": row["to_addr"] or f"{row['mailbox']}@{get_settings().org_domain}"},
        {"name": "MIME-Version", "value": "1.0"},
        {"name": "Subject", "value": row["title"]},
        {"name": "From", "value": f"{display} <{author}>"},
        {"name": "To", "value": row["to_addr"] or f"{row['mailbox']}@{get_settings().org_domain}"},
        {"name": "Date", "value": synth.rfc2822(ts)},
        {"name": "Message-ID", "value": msg_id},
    ]
    for hname, col in (("Cc", "cc"), ("Reply-To", "reply_to"),
                       ("In-Reply-To", "in_reply_to"), ("References", "refs")):
        if row[col]:
            headers.append({"name": hname, "value": row[col]})
    attachments = store.jcol(row, "attachments")
    top_mime = "multipart/mixed" if attachments else "multipart/alternative"
    boundary = f'b_{row["doc_id"][:12]}'
    headers.append({"name": "Content-Type", "value": f'{top_mime}; boundary="{boundary}"'})

    msg = {
        "id": row["doc_id"], "threadId": row["thread_id"] or row["doc_id"],
        "labelIds": store.jcol(row, "label_ids") or ["INBOX"],
        "snippet": row["content"][:200], "historyId": "1",
        "internalDate": str(ts * 1000), "sizeEstimate": len(row["content"]) + 400,
    }
    html = row["body_html"] or f"<html><body><p>{row['content']}</p></body></html>"
    if fmt == "raw":
        # RFC 2822 message, base64url — a genuine boundary-delimited MIME body matching the
        # declared multipart Content-Type above (previously this just appended the plain-text
        # content under a `multipart/...` header with no boundary anywhere in the body: real
        # Gmail never produces that — invalid MIME — and Python's `email` parser flags it with
        # StartBoundaryNotFoundDefect/MultipartInvariantViolationDefect, which readers built on
        # it, e.g. llama-index's GmailReader, choke on since `get_payload()` degrades to a bare
        # string instead of a list of sub-messages). Mirrors the same flat text/plain + text/html
        # (+ attachment) leaves the `full` format exposes via `parts` below.
        leaves = [
            f'Content-Type: text/plain; charset="UTF-8"\r\n\r\n{row["content"]}',
            f'Content-Type: text/html; charset="UTF-8"\r\n\r\n{html}',
        ]
        for att in attachments:
            filename = att.get("filename", "attachment.bin")
            mime = att.get("mime", "application/octet-stream")
            b64 = base64.b64encode(att.get("content", "").encode("utf-8")).decode("ascii")
            leaves.append(
                f'Content-Type: {mime}; name="{filename}"\r\n'
                f'Content-Disposition: attachment; filename="{filename}"\r\n'
                f'Content-Transfer-Encoding: base64\r\n\r\n{b64}'
            )
        mime_body = "".join(f"--{boundary}\r\n{leaf}\r\n" for leaf in leaves) + f"--{boundary}--"
        raw = "\r\n".join(f"{h['name']}: {h['value']}" for h in headers) + "\r\n\r\n" + mime_body
        msg["raw"] = _b64url(raw)
        return msg
    if fmt == "minimal":
        return msg
    if fmt == "metadata":
        msg["payload"] = {"partId": "", "mimeType": top_mime, "filename": "",
                          "headers": headers, "body": {"size": 0}}
        return msg

    # full: multipart with text/plain + text/html leaves, plus attachment leaves
    parts = [_leaf("text/plain", "0", row["content"]),
             _leaf("text/html", "1", html)]
    for i, att in enumerate(attachments):
        parts.append({
            "partId": str(i + 2), "mimeType": att.get("mime", "application/octet-stream"),
            "filename": att.get("filename", "attachment.bin"),
            "headers": [{"name": "Content-Disposition",
                         "value": f'attachment; filename="{att.get("filename", "attachment.bin")}"'}],
            "body": {"attachmentId": _att_id(row["doc_id"], i), "size": att.get("size", 1024)},
        })
    msg["payload"] = {"partId": "", "mimeType": top_mime, "filename": "",
                      "headers": headers, "body": {"size": 0}, "parts": parts}
    return msg


# ================================ Drive =========================================

_DRIVE_FULLTEXT_RE = re.compile(r"fullText\s+contains\s+'([^']+)'")


def _drive_q_match(row, q: str) -> bool:
    """Honor the common Drive `q` clauses connectors use (folder scoping, mimeType,
    name contains, modifiedTime, trashed). `fullText contains` is handled upstream via FTS
    (see drive_files_list), so it's stripped from `q` before this runs. Unrecognized clauses
    are ignored."""
    trashed = bool(row["trashed"])
    m = re.search(r"trashed\s*=\s*(true|false)", q)
    if m:
        if (m.group(1) == "true") != trashed:
            return False
    elif trashed:  # real API excludes trashed by default
        return False
    for fid in re.findall(r"'([^']+)'\s+in\s+parents", q):
        parents = store.jcol(row, "parents") or [synth.drive_folder_id(row["folder"])]
        if fid not in parents:
            return False
    m = re.search(r"mimeType\s*(=|!=)\s*'([^']+)'", q)
    if m:
        native = _NATIVE.get(row["subtype"] or "document")
        mime = native[0] if native else (row["mime_type"] or "application/octet-stream")
        if (m.group(1) == "=") != (mime == m.group(2)):
            return False
    m = re.search(r"name\s+contains\s+'([^']+)'", q)
    if m and m.group(1).lower() not in (row["title"] or "").lower():
        return False
    m = re.search(r"modifiedTime\s*>\s*'([^']+)'", q)
    if m:
        modified = row["updated_ts"] or (row["created_ts"] or synth.epoch(row["doc_id"])) + 3600
        if synth.rfc3339(modified) <= m.group(1):
            return False
    # `'<who>' in owners` — real Drive keys on the owner's email; the mock also accepts the
    # owner display name, since that's the only owner identifier some callers have.
    for who in re.findall(r"'([^']+)'\s+in\s+owners", q):
        w = who.strip().lower()
        if w not in ((row["author_email"] or "").lower(), (row["owner_display"] or "").lower()):
            return False
    return True


def _visible_drive_folders(conn, ids) -> list[str]:
    """Folder names the caller can see a file in — the containers to surface as folders."""
    folders = [r["name"] for r in store.list_containers(conn, "google_drive")]
    if ids is None:  # admin sees every folder
        return sorted(folders)
    return sorted(f for f in folders if store.drive_folder_has_visible(conn, f, ids))


def _drive_folder_obj(conn, name: str) -> dict:
    """A Drive file object for a folder container. Its id matches what files in it report as
    their parent (``synth.drive_folder_id``), and it hangs under ``root`` so a client that
    navigates from My Drive root (e.g. mirage) can discover and descend into it."""
    fid = synth.drive_folder_id(name)
    ts = synth.epoch("folder:" + name)
    return {
        "kind": "drive#file", "id": fid, "name": name,
        "mimeType": "application/vnd.google-apps.folder", "parents": ["root"],
        "createdTime": synth.rfc3339(ts), "modifiedTime": synth.rfc3339(ts),
        "trashed": False, "explicitlyTrashed": False, "starred": False,
        "shared": True, "ownedByMe": False, "viewedByMe": False,
        "version": "1", "spaces": ["drive"],
        "webViewLink": f"https://drive.google.com/drive/folders/{fid}",
        "iconLink": "https://drive.google.com/icons/folder.png",
        "capabilities": {"canDownload": False, "canListChildren": True, "canComment": False,
                         "canEdit": False, "canCopy": False, "canShare": True,
                         "canRename": False, "canTrash": False, "canDelete": False,
                         "canReadRevisions": False, "canAddChildren": False,
                         "canModifyContent": False},
    }


def _drive_folder_name_by_id(conn, file_id: str) -> str | None:
    """Reverse a synthesized folder id back to its container name. Uses the small folder table
    (no ACL/no per-row scan) — the caller's ACL is enforced when its files are then listed."""
    for row in store.list_containers(conn, "google_drive"):
        if synth.drive_folder_id(row["name"]) == file_id:
            return row["name"]
    return None


def _drive_file_field_keys(fields: str | None) -> set[str] | None:
    """Top-level file keys a client asked for via ``fields=…files(id,name,…)`` — so a list
    response carries only those, not the full ~30-field object. None = no projection (return
    everything). Nested masks (``capabilities/canEdit``) keep the whole parent key."""
    m = re.search(r"files\(([^)]*)\)", fields or "")
    if not m:
        return None
    keys = {tok.strip().split("/")[0].split("(")[0]
            for tok in m.group(1).split(",") if tok.strip()}
    return keys or None


def _drive_q_plain_folder(q: str) -> bool:
    """True when ``q`` is just a folder scope (``'<id>' in parents``, optionally ``trashed=false``)
    with no other clause — the shape a tree-walking client sends, servable straight from SQL."""
    residual = re.sub(r"'[^']+'\s+in\s+parents", " ", q)
    residual = re.sub(r"trashed\s*=\s*false", " ", residual)
    residual = re.sub(r"\band\b", " ", residual, flags=re.IGNORECASE)
    return residual.strip() == ""


def _drive_root_folders(conn, ids, q: str, offset: int, limit: int) -> dict:
    """Listing for ``'root' in parents``: the mock's files always live in a folder, so the
    root holds exactly the (visible) folder objects. Honors the ``name``/``mimeType`` clauses
    a client may add."""
    names = _visible_drive_folders(conn, ids)
    m = re.search(r"name\s+contains\s+'([^']+)'", q)
    if m:
        names = [n for n in names if m.group(1).lower() in n.lower()]
    m = re.search(r"mimeType\s*(=|!=)\s*'([^']+)'", q)
    if m:  # a non-folder mimeType filter excludes every folder
        is_folder = m.group(2) == "application/vnd.google-apps.folder"
        if (m.group(1) == "=") != is_folder:
            names = []
    folders = [_drive_folder_obj(conn, n) for n in names]
    page = folders[offset:offset + limit]
    body = {"kind": "drive#fileList", "incompleteSearch": False, "files": page}
    token = next_page_token(offset, len(page), len(folders))
    if token:
        body["nextPageToken"] = token
    return body


@router.get("/drive/v3/drives")
async def drive_shared_drives(request: Request):
    """Shared (Team) Drives — the mock's corpus lives entirely in My Drive, so this is empty.
    Present so shared-drive-aware clients don't 404 while enumerating."""
    _require(request)
    return {"kind": "drive#driveList", "drives": []}


@router.get("/drive/v3/files", response_model=DriveFileList,
            openapi_extra={"parameters": _P_DRIVE_LIST})
async def drive_files_list(request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    limit = _int(request, "pageSize", get_settings().default_page_size)
    offset = decode_cursor(request.query_params.get("pageToken"))
    q = request.query_params.get("q", "") or ""
    parent_ids = re.findall(r"'([^']+)'\s+in\s+parents", q)
    if "root" in parent_ids:
        return _drive_root_folders(conn, ids, q, offset, limit)
    # A folder-scoped parent resolves to one container name (for the SQL-scoped paths below).
    container = next((n for pid in parent_ids
                      if (n := _drive_folder_name_by_id(conn, pid))), None)
    if container is not None and _drive_q_plain_folder(q):
        # The common case (a client walking the tree): just this folder's files. SQL-scoped and
        # SQL-paginated — one page of rows per request, not a full scan re-run for every page.
        total = store.count_drive_folder(conn, container, ids)
        rows = store.list_drive_folder(conn, container, ids, limit=limit, offset=offset)
    elif q.strip():  # filter the visible set by the query, then paginate
        ft = _DRIVE_FULLTEXT_RE.search(q)
        if ft:  # fullText contains → FTS candidates (ranked), then the other q clauses
            # Honor real Drive semantics: a quoted value (`fullText contains '"X Y"'`) is an exact
            # phrase (tokens adjacent); unquoted is separate terms. A grep push-down sends the quoted
            # form for a literal pattern, so the exact doc surfaces instead of being buried under
            # coincidental docs that merely contain the words scattered.
            ft_raw = ft.group(1)
            phrase = len(ft_raw) >= 2 and ft_raw[0] == '"' and ft_raw[-1] == '"'
            ft_term = ft_raw[1:-1] if phrase else ft_raw
            q_rest = _DRIVE_FULLTEXT_RE.sub(" ", q)  # FTS owns fullText; strip it from the rest
            candidates = store.search_documents(conn, ft_term, "google_drive", ids,
                                                limit=10_000, phrase=phrase)
        else:
            q_rest = q
            nm = re.search(r"name\s+contains\s+'([^']+)'", q)
            if nm:  # a name lookup (mirage resolves every gdrive file this way) — SQL title LIKE
                # instead of materializing the whole corpus (~25k rows, ~1.6s) to substring-match in
                # Python. The remaining q clauses still filter the (small) name-matched set below.
                candidates = store.list_drive_by_name(conn, nm.group(1), container, ids, limit=100_000)
            else:  # scope to the folder (if any) to shrink the set before the Python filter
                candidates = store.list_documents(conn, "google_drive", container=container,
                                                  visible_ids=ids, limit=100_000)
        matched = [r for r in candidates if _drive_q_match(r, q_rest)]
        total = len(matched)
        rows = matched[offset:offset + limit]
    else:
        total = store.count_documents(conn, "google_drive", visible_ids=ids)
        rows = store.list_documents(conn, "google_drive", visible_ids=ids, limit=limit, offset=offset)
    shared = store.docs_with_grants(conn, [r["doc_id"] for r in rows])  # one query, not one per file
    keys = _drive_file_field_keys(request.query_params.get("fields"))  # honor fields → smaller payload
    files = [_drive_file(conn, r, shared=r["doc_id"] in shared) for r in rows]
    if keys:
        files = [{k: v for k, v in f.items() if k in keys} for f in files]
    body = {"kind": "drive#fileList", "incompleteSearch": False, "files": files}
    token = next_page_token(offset, len(rows), total)
    if token:
        body["nextPageToken"] = token
    return body


@router.get("/drive/v3/files/{file_id}", openapi_extra={"parameters": _P_DRIVE_ALT})
async def drive_files_get(file_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.get_document(conn, "google_drive", file_id, visible_ids=ids)
    if row is None:
        name = _drive_folder_name_by_id(conn, file_id)  # folders aren't stored as rows
        if name is not None:
            return _drive_folder_obj(conn, name)
        raise HTTPException(status_code=404, detail="File not found")
    if request.query_params.get("alt") == "media":
        # raw download — real API errors on native Docs-editors types (use export)
        if _native(row) is not None:
            raise HTTPException(status_code=403,
                                detail="Only files with binary content can be downloaded. Use Export with Docs Editors files.")
        mime = row["mime_type"] or "application/octet-stream"
        return Response(row["content"].encode("utf-8"), media_type=mime)
    return _drive_file(conn, row)


@router.get("/drive/v3/files/{file_id}/export", openapi_extra={"parameters": _P_DRIVE_EXPORT})
async def drive_files_export(file_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.get_document(conn, "google_drive", file_id, visible_ids=ids)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    native = _native(row)
    if native is None or native[2] is None:  # binary or folder — not exportable
        raise HTTPException(status_code=403, detail="Export only supports Docs Editors files.")
    requested = request.query_params.get("mimeType")
    if not requested:  # the real API requires an explicit target format
        raise HTTPException(status_code=400,
                            detail="The 'mimeType' parameter is required for files.export.")
    # honor the requested target format; CSV/TSV keep the raw content, others prefix the title
    plain = requested in ("text/csv", "text/tab-separated-values")
    body = row["content"] if plain else f"{row['title']}\n\n{row['content']}"
    return PlainTextResponse(body, media_type=requested)


@router.get("/drive/v3/files/{file_id}/permissions", response_model=DrivePermissionList)
async def drive_files_permissions(file_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.get_document(conn, "google_drive", file_id, visible_ids=ids)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"kind": "drive#permissionList", "permissions": _drive_permissions(conn, file_id)}


# --- Google Workspace editors read APIs (Docs / Sheets / Slides) ------------------
#
# Drive `files.export` renders a native doc to text, but editor-aware clients (e.g. mirage)
# read the *structured* document straight from the Docs/Sheets/Slides APIs instead. These
# endpoints serve the corpus content shaped into each API's read response, keyed on the same
# Drive file id (the doc_id), and enforce the same ACL as Drive.

def _editor_doc(request: Request, file_id: str):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.get_document(conn, "google_drive", file_id, visible_ids=ids)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return row


@router.get("/docs/v1/documents/{document_id}")
async def docs_get(document_id: str, request: Request):
    row = _editor_doc(request, document_id)
    # Docs body is an ordered list of structural elements; one paragraph per line.
    content = [{"sectionBreak": {"sectionStyle": {}}}]
    for line in (row["content"] or "").split("\n"):
        content.append({"paragraph": {"elements": [
            {"textRun": {"content": line + "\n", "textStyle": {}}}]}})
    return {"documentId": document_id, "title": row["title"],
            "revisionId": synth._digest(document_id)[:24], "suggestionsViewMode": "SUGGESTIONS_INLINE",
            "body": {"content": content}, "documentStyle": {}, "namedStyles": {"styles": []}}


@router.get("/sheets/v4/spreadsheets/{spreadsheet_id}")
async def sheets_get(spreadsheet_id: str, request: Request):
    row = _editor_doc(request, spreadsheet_id)
    rows = [line.split(",") for line in (row["content"] or "").split("\n")]
    ncols = max((len(r) for r in rows), default=0)
    row_data = [{"values": [{"formattedValue": c,
                             "effectiveValue": {"stringValue": c}} for c in r]} for r in rows]
    return {"spreadsheetId": spreadsheet_id, "properties": {"title": row["title"], "locale": "en_US"},
            "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1", "index": 0,
                                       "sheetType": "GRID",
                                       "gridProperties": {"rowCount": len(rows) or 1,
                                                          "columnCount": ncols or 1}},
                        "data": [{"startRow": 0, "startColumn": 0, "rowData": row_data}]}]}


@router.get("/slides/v1/presentations/{presentation_id}")
async def slides_get(presentation_id: str, request: Request):
    row = _editor_doc(request, presentation_id)
    chunks = [c for c in (row["content"] or "").split("\n\n") if c.strip()] or [row["content"] or ""]
    slides = []
    for i, chunk in enumerate(chunks):
        slides.append({"objectId": f"p{i}", "pageType": "SLIDE", "pageElements": [
            {"objectId": f"p{i}_t", "shape": {"shapeType": "TEXT_BOX", "text": {"textElements": [
                {"textRun": {"content": chunk + "\n", "style": {}}}]}}}]})
    return {"presentationId": presentation_id, "title": row["title"],
            "pageSize": {"width": {"magnitude": 9144000, "unit": "EMU"},
                         "height": {"magnitude": 6858000, "unit": "EMU"}},
            "slides": slides}


# Google Workspace native types: subtype -> (mimeType, webView path segment, export content-type)
_NATIVE = {
    "document": ("application/vnd.google-apps.document", "document", "text/plain"),
    "spreadsheet": ("application/vnd.google-apps.spreadsheet", "spreadsheets", "text/csv"),
    "presentation": ("application/vnd.google-apps.presentation", "presentation", "text/plain"),
    "folder": ("application/vnd.google-apps.folder", None, None),
}


def _native(row):
    """Return the _NATIVE tuple for this doc, or None if it's a binary (non-native) file."""
    return _NATIVE.get(row["subtype"] or "document")


def _drive_user(email: str) -> dict:
    return {"kind": "drive#user", "displayName": email.split("@")[0].replace(".", " ").title(),
            "emailAddress": email, "me": False,
            "permissionId": str(synth.github_user_id(email)),
            "photoLink": synth.github_avatar(synth.github_user_id(email))}


def _drive_file(conn, row, shared: bool | None = None) -> dict:
    created = row["created_ts"] or synth.epoch(row["doc_id"])
    modified = row["updated_ts"] or created + 3600
    author = row["author_email"]
    native = _native(row)
    if native is not None:
        mime, seg, _ = native
        view = (f"https://docs.google.com/{seg}/d/{row['doc_id']}/edit" if seg
                else f"https://drive.google.com/drive/folders/{row['doc_id']}")
    else:  # binary file (PDF, image, office doc)
        mime = row["mime_type"] or "application/octet-stream"
        view = f"https://drive.google.com/file/d/{row['doc_id']}/view"
    is_folder = row["subtype"] == "folder"
    # "shared" = visible to anyone besides the owner — true for org/group/multi-reader docs.
    # In a list the caller passes it in (batch-computed); for a single get, look it up here.
    if shared is None:
        shared = bool(store.doc_grants(conn, row["doc_id"]))
    ext = row["title"].rsplit(".", 1)[-1] if (native is None and "." in row["title"]) else None
    f = {
        "kind": "drive#file", "id": row["doc_id"], "name": row["title"], "mimeType": mime,
        "parents": store.jcol(row, "parents") or [synth.drive_folder_id(row["folder"])],
        "createdTime": synth.rfc3339(created), "modifiedTime": synth.rfc3339(modified),
        "owners": [_drive_user(author)], "lastModifyingUser": _drive_user(author),
        "trashed": bool(row["trashed"]), "explicitlyTrashed": bool(row["trashed"]),
        "starred": False, "shared": bool(shared), "ownedByMe": False, "viewedByMe": False,
        "version": str(2 if row["updated_ts"] else 1),
        "spaces": ["drive"], "webViewLink": view,
        "iconLink": f"https://drive.google.com/icons/{(row['subtype'] or 'document')}.png",
        "capabilities": {
            "canDownload": not is_folder, "canListChildren": is_folder,
            "canComment": not is_folder, "canEdit": False, "canCopy": not is_folder,
            "canShare": True, "canRename": False, "canTrash": False, "canDelete": False,
            "canReadRevisions": not is_folder, "canAddChildren": is_folder,
            "canModifyContent": False},
    }
    if native is None:  # binaries report bytes + checksums; native Workspace files don't
        f["size"] = str(len(row["content"]))
        f["md5Checksum"] = hashlib.md5(row["content"].encode()).hexdigest()
        f["quotaBytesUsed"] = str(len(row["content"]))
        f["webContentLink"] = f"https://drive.google.com/uc?id={row['doc_id']}&export=download"
        if ext:
            f["fileExtension"] = ext
            f["fullFileExtension"] = ext
    return f


def _drive_permissions(conn, doc_id: str) -> list[dict]:
    """Build from the doc's ACL grants (preserving user/group/org identity) + an owner."""
    grants = store.doc_grants(conn, doc_id)
    domain = get_settings().org_domain
    perms = []
    for g in grants:
        ptype, pid = g["principal_type"], g["principal_id"]
        if ptype == "org":  # anyone-in-org / anyone-with-link
            perms.append({"kind": "drive#permission", "id": "anyoneWithLink", "type": "anyone",
                          "role": "reader", "allowFileDiscovery": True})
        elif ptype == "group":
            perms.append({"kind": "drive#permission", "id": str(synth.github_user_id(pid)),
                          "type": "group", "role": "reader", "emailAddress": f"{pid}@{domain}",
                          "displayName": pid})
        else:  # user
            perms.append({"kind": "drive#permission", "id": str(synth.github_user_id(pid)),
                          "type": "user", "role": "reader", "emailAddress": pid,
                          "displayName": pid.split("@")[0].replace(".", " ").title()})
    # every file has an owner
    row = store.get_document(conn, "google_drive", doc_id)
    if row is not None:
        owner = row["author_email"]
        perms.insert(0, {"kind": "drive#permission", "id": str(synth.github_user_id(owner)),
                         "type": "user", "role": "owner", "emailAddress": owner,
                         "displayName": owner.split("@")[0].replace(".", " ").title()})
    return perms


def _int(request: Request, key: str, default: int) -> int:
    v = request.query_params.get(key)
    try:
        return min(int(v), get_settings().max_page_size) if v else default
    except ValueError:
        return default
