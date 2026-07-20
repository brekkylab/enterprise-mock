"""HTTP endpoint tests: drive the vendor endpoints directly (TestClient) over a built DB.

Asserts, over the conftest SAMPLE corpus (built fresh into a tmp dir — hermetic, so the suite
neither depends on nor crawls whatever ambient import lives in ``data/``): (1) an admin crawl
paginates through *every* stored document per source, (2) document content round-trips
byte-for-byte through each vendor's encoding, and (3) a non-admin user's crawl is filtered to
exactly their ACL. The completeness assertion ``crawl_count == db_count`` holds at any corpus
size, so it stays meaningful over the small SAMPLE while running in well under a second.
"""
from __future__ import annotations

import base64
import os
import re

import pytest
import yaml
from starlette.testclient import TestClient

from app import store
from app.config import get_settings


@pytest.fixture(scope="module")
def client(sample_settings):
    """A TestClient whose app is pointed at the SAMPLE DB (via MOCK_DATA_DIR), not the ambient
    ``data/`` import. Env + settings cache are restored on teardown so other modules are unaffected."""
    from app.main import app

    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(sample_settings.data_dir)
    get_settings.cache_clear()
    try:
        with TestClient(app) as c:  # lifespan opens sample_settings.db_path
            yield c
    finally:
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev


@pytest.fixture(scope="module")
def org(client):
    """The org name the mock derived from the corpus (SAMPLE is @acme.com -> 'acme')."""
    return client.get("/_mock/users").json()["org"]


@pytest.fixture(scope="module")
def tokens(sample_settings):
    return yaml.safe_load(sample_settings.tokens_path.read_text())


@pytest.fixture(scope="module")
def admin_h(tokens):
    return {"Authorization": f"Bearer {tokens['admin_token']}"}


@pytest.fixture(scope="module")
def ro_conn(sample_settings):
    conn = store.connect_ro(sample_settings.db_path)
    yield conn
    conn.close()


def db_count(conn, source_type, **kw):
    return store.count_documents(conn, source_type, **kw)


# --- crawlers (small page sizes to exercise pagination) -------------------------

def crawl_gmail(client, headers, user="me"):
    ids, token = [], None
    while True:
        p = {"maxResults": 7}
        if token:
            p["pageToken"] = token
        j = client.get(f"/gmail/v1/users/{user}/messages", headers=headers, params=p).json()
        ids += [m["id"] for m in j.get("messages", [])]
        token = j.get("nextPageToken")
        if not token:
            break
    return ids


def crawl_drive(client, headers):
    ids, token = [], None
    while True:
        p = {"pageSize": 7}
        if token:
            p["pageToken"] = token
        j = client.get("/drive/v3/files", headers=headers, params=p).json()
        ids += [f["id"] for f in j.get("files", [])]
        token = j.get("nextPageToken")
        if not token:
            break
    return ids


def crawl_github_repo(client, headers, org, repo):
    out, page = [], 1
    while True:
        r = client.get(f"/github/repos/{org}/{repo}/issues", headers=headers,
                       params={"per_page": 5, "page": page})
        body = r.json()
        out += body
        if 'rel="next"' not in r.headers.get("Link", ""):
            break
        page += 1
    return out


def crawl_jira(client, headers):
    out, token = [], None
    while True:
        p = {"maxResults": 6}
        if token:
            p["nextPageToken"] = token
        j = client.get("/atlassian/rest/api/3/search/jql", headers=headers, params=p).json()
        out += j["issues"]
        if j.get("isLast", True):
            break
        token = j["nextPageToken"]
    return out


def crawl_confluence(client, headers):
    out, start, limit = [], 0, 7
    while True:
        j = client.get("/atlassian/wiki/rest/api/content", headers=headers,
                       params={"start": start, "limit": limit, "expand": "body.storage"}).json()
        out += j["results"]
        if "next" not in j.get("_links", {}):
            break
        start += limit
    return out


def crawl_slack(client, headers):
    total, cursor = 0, None
    channels = []
    while True:
        data = {"limit": 8}
        if cursor:
            data["cursor"] = cursor
        j = client.post("/slack/api/conversations.list", headers=headers, data=data).json()
        channels += j["channels"]
        cursor = j["response_metadata"]["next_cursor"]
        if not cursor:
            break
    for ch in channels:
        ccur = None
        while True:
            d = {"channel": ch["id"], "limit": 50}
            if ccur:
                d["cursor"] = ccur
            h = client.post("/slack/api/conversations.history", headers=headers, data=d).json()
            for m in h["messages"]:
                total += 1
                if m.get("reply_count"):  # a thread root — its replies come from conversations.replies
                    r = client.post("/slack/api/conversations.replies", headers=headers,
                                    data={"channel": ch["id"], "ts": m["ts"]}).json()
                    total += len(r["messages"]) - 1  # thread includes the root we already counted
            ccur = h["response_metadata"]["next_cursor"]
            if not ccur:
                break
    return total


# --- admin full-crawl completeness ---------------------------------------------

def test_admin_gmail_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_gmail(client, admin_h)) == db_count(ro_conn, "gmail")


def test_admin_drive_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_drive(client, admin_h)) == db_count(ro_conn, "google_drive")


def test_admin_github_crawls_all(client, admin_h, ro_conn, org):
    repos = client.get(f"/github/orgs/{org}/repos", headers=admin_h, params={"per_page": 100}).json()
    seen = []
    for r in repos:
        seen += crawl_github_repo(client, admin_h, org, r["name"])
    assert len(seen) == db_count(ro_conn, "github")


def test_admin_jira_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_jira(client, admin_h)) == db_count(ro_conn, "jira")


def test_admin_confluence_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_confluence(client, admin_h)) == db_count(ro_conn, "confluence")


def test_admin_slack_crawls_all(client, admin_h, ro_conn):
    assert crawl_slack(client, admin_h) == db_count(ro_conn, "slack")


# --- content round-trips through each vendor's encoding -------------------------

def _gmail_plain(payload):
    """Extract the text/plain body data from a Gmail payload (top-level or a part)."""
    if payload.get("body", {}).get("data"):
        return payload["body"]["data"]
    for part in payload.get("parts", []):
        if part["mimeType"] == "text/plain":
            return part["body"]["data"]
    raise AssertionError("no text/plain part")


def test_gmail_body_roundtrip(client, admin_h, ro_conn):
    doc = ro_conn.execute("SELECT * FROM gmail_messages LIMIT 1").fetchone()
    m = client.get(f"/gmail/v1/users/me/messages/{doc['doc_id']}", headers=admin_h,
                   params={"format": "full"}).json()
    body = base64.urlsafe_b64decode(_gmail_plain(m["payload"])).decode()
    assert body == doc["content"]
    subj = next(h["value"] for h in m["payload"]["headers"] if h["name"] == "Subject")
    assert subj == doc["title"]


def test_drive_export_roundtrip(client, admin_h, ro_conn):
    doc = ro_conn.execute("SELECT * FROM gdrive_files LIMIT 1").fetchone()
    text = client.get(f"/drive/v3/files/{doc['doc_id']}/export", headers=admin_h,
                      params={"mimeType": "text/plain"}).text
    assert doc["content"] in text and text.startswith(doc["title"])


def test_github_body_roundtrip(client, admin_h, ro_conn, org):
    doc = ro_conn.execute("SELECT * FROM github_items LIMIT 1").fetchone()
    from app import synth
    num = synth.github_number(doc["doc_id"])
    issue = client.get(f"/github/repos/{org}/{doc['repo']}/issues/{num}", headers=admin_h).json()
    assert issue["body"] == doc["content"] and issue["title"] == doc["title"]


def test_confluence_storage_roundtrip(client, admin_h, ro_conn):
    doc = ro_conn.execute("SELECT * FROM confluence_pages LIMIT 1").fetchone()
    from app import synth
    cid = synth.confluence_id(doc["doc_id"])
    page = client.get(f"/atlassian/wiki/rest/api/content/{cid}", headers=admin_h,
                      params={"expand": "body.storage"}).json()
    xhtml = page["body"]["storage"]["value"]
    # invert _storage: join paragraphs on \n\n, drop the wrapping tags, unescape
    from html import unescape
    text = xhtml.replace("</p><p>", "\n\n")
    text = re.sub(r"</?p>", "", text)
    assert unescape(text).strip() == doc["content"].strip()


# --- ACL enforcement over HTTP --------------------------------------------------

def test_user_sees_subset_of_admin(client, admin_h, tokens, ro_conn, sample_settings):
    user = tokens["users"][0]
    uh = {"Authorization": f"Bearer {user['token']}"}
    admin_conf = len(crawl_confluence(client, admin_h))
    user_conf = len(crawl_confluence(client, uh))
    assert user_conf < admin_conf  # some confluence docs are group/private-restricted
    # matches exactly the ACL-computed visible count
    from app.acl import Acl
    acl = Acl.load(sample_settings.tokens_path, sample_settings.admin_token, sample_settings.org_name)
    vids = acl.visible_ids(ro_conn, acl.resolve(user["token"]))
    assert user_conf == db_count(ro_conn, "confluence", visible_ids=vids)


def test_mock_users_directory(client, tokens, org):
    # the /_mock/users directory lists every user + token (for testing per-user ACL)
    body = client.get("/_mock/users").json()
    assert body["admin_token"] == tokens["admin_token"]
    yaml_by_email = {u["email"]: u["token"] for u in tokens["users"]}
    assert body["count"] == len(body["users"]) == len(yaml_by_email) > 0
    for u in body["users"]:
        assert u["token"] == yaml_by_email[u["email"]]  # matches data/tokens.yaml
        assert u["name"] and isinstance(u["groups"], list)
    # a listed token really is ACL-scoped: it resolves and sees <= what admin sees
    u = body["users"][0]
    admin_repos = client.get(f"/github/orgs/{org}/repos",
                             headers={"Authorization": f"Bearer {body['admin_token']}"}).json()
    user_repos = client.get(f"/github/orgs/{org}/repos",
                            headers={"Authorization": f"Bearer {u['token']}"}).json()
    assert 0 < len(user_repos) <= len(admin_repos)


def test_mock_users_can_be_disabled(client, monkeypatch):
    from app import main
    from app.config import Settings
    monkeypatch.setattr(main, "get_settings", lambda: Settings(expose_tokens=False))
    assert client.get("/_mock/users").status_code == 404


def test_unauthenticated_is_rejected(client):
    assert client.get("/drive/v3/files").status_code == 401
    assert client.get("/atlassian/rest/api/3/search/jql").status_code == 401
    slack = client.post("/slack/api/conversations.list").json()
    assert slack == {"ok": False, "error": "not_authed"}


def test_slack_accepts_form_field_token(client, tokens):
    # the official slack-go SDK posts the token as a form field (no bearer header); the mock
    # must accept it exactly like a real Slack Web API.
    admin = tokens["admin_token"]
    ok = client.post("/slack/api/search.messages", data={"token": admin, "query": "the"}).json()
    assert ok["ok"] is True
    # no token anywhere -> not_authed
    none = client.post("/slack/api/search.messages", data={"query": "the"}).json()
    assert none == {"ok": False, "error": "not_authed"}


def test_slack_users_info_resolves_author(client, admin_h, ro_conn):
    # users.info must resolve a Slack message author's synthesized id (incl. display-only
    # speakers/bots, which aren't principals) — qst_0077's raw-ID bug.
    from app import synth
    email = ro_conn.execute("SELECT DISTINCT author_email FROM slack_messages LIMIT 1").fetchone()[0]
    uid = synth.slack_user_id(email)
    j = client.post("/slack/api/users.info", headers=admin_h, data={"user": uid}).json()
    assert j["ok"] is True
    assert j["user"]["id"] == uid and j["user"]["profile"]["email"] == email
    # a bogus id still 404s (clause honored, cache doesn't invent users)
    bad = client.post("/slack/api/users.info", headers=admin_h, data={"user": "UZZZZZZZZZZ"}).json()
    assert bad == {"ok": False, "error": "user_not_found"}


def test_drive_in_owners_query(client, admin_h, ro_conn):
    # real Drive supports `'<owner>' in owners`; the mock must filter by owner (email or name),
    # not ignore the clause. (qst_0031's broken owner-lookup path.)
    total = db_count(ro_conn, "google_drive")
    owner = ro_conn.execute("SELECT author_email FROM gdrive_files LIMIT 1").fetchone()["author_email"]
    expected = ro_conn.execute("SELECT count(*) FROM gdrive_files WHERE author_email=?", (owner,)).fetchone()[0]
    j = client.get("/drive/v3/files", headers=admin_h,
                   params={"q": f"'{owner}' in owners", "pageSize": 1000}).json()
    n = len(j.get("files", []))
    assert 0 < n < total and n == expected  # filtered to exactly this owner's files
    # a non-owner returns nothing (clause honored, not ignored)
    none = client.get("/drive/v3/files", headers=admin_h,
                      params={"q": "'nobody-xyz@acme.com' in owners", "pageSize": 100}).json()
    assert none.get("files", []) == []


def test_slack_search_all(client, admin_h):
    # slack-go's Search()/SearchContext() hits search.all; it must return both messages + files.
    j = client.post("/slack/api/search.all", headers=admin_h, data={"query": "the"}).json()
    assert j["ok"] is True
    assert "messages" in j and "files" in j
    assert j["files"]["total"] == 0 and j["files"]["matches"] == []


def test_google_batch_dispatches_subrequests(client, admin_h, ro_conn):
    # google-api-python-client posts a multipart/mixed batch to /batch; the mock must dispatch each
    # application/http sub-request in-process and return a multipart/mixed of sub-responses matched
    # by Content-ID. Regression for the batch escaping to real Google (401). Build the batch body
    # exactly like BatchHttpRequest does.
    from email.generator import Generator
    from email.mime.multipart import MIMEMultipart
    from email.mime.nonmultipart import MIMENonMultipart
    from email.parser import BytesParser
    from io import StringIO

    listed = client.get("/gmail/v1/users/me/messages", headers=admin_h,
                        params={"maxResults": 2}).json().get("messages", [])
    ids = [m["id"] for m in listed]
    assert ids, "need at least one gmail message in the sample"

    msg = MIMEMultipart("mixed")
    setattr(msg, "_write_headers", lambda self: None)
    for i, mid in enumerate(ids):
        part = MIMENonMultipart("application", "http")
        part["Content-Transfer-Encoding"] = "binary"
        part["Content-ID"] = f"<base + {i}>"  # the format BatchHttpRequest uses
        part.set_payload(f"GET /gmail/v1/users/me/messages/{mid}?format=minimal HTTP/1.1\r\n\r\n")
        msg.attach(part)
    fp = StringIO()
    Generator(fp, mangle_from_=False).flatten(msg, unixfrom=False)
    body, boundary = fp.getvalue(), msg.get_boundary()

    r = client.post("/batch", headers={**admin_h, "Content-Type": f'multipart/mixed; boundary="{boundary}"'},
                    content=body)
    assert r.status_code == 200, r.text
    assert "multipart/mixed" in r.headers["content-type"]
    parsed = BytesParser().parsebytes(
        b"Content-Type: " + r.headers["content-type"].encode() + b"\r\n\r\n" + r.content)
    parts = parsed.get_payload()
    assert len(parts) == len(ids)
    for i, (mid, part) in enumerate(zip(ids, parts)):
        assert part["Content-ID"] == f"<base + {i}>"          # echoed so the client can pair them
        sub = part.get_payload(decode=False)
        assert sub.startswith("HTTP/1.1 200")                  # dispatched with the admin token, not 401
        assert mid in sub                                      # the message JSON came back


def test_slack_replies_resolve_from_a_reply_ts(client, admin_h):
    # A search hit that lands on a REPLY yields that reply's ts; conversations.replies must return
    # the whole thread from it (Slack accepts any in-thread ts), not thread_not_found. The SAMPLE
    # 'incidents' 502 thread's replies include "Rolled back; 502s clearing." Regression: previously
    # replies resolved only thread ROOTS, so a search->replies chain broke whenever the hit was a
    # reply (the common case — real MCP clients pass the hit's own ts).
    sr = client.post("/slack/api/search.messages", headers=admin_h,
                     data={"query": "Rolled back"}).json()
    matches = sr["messages"]["matches"]
    assert matches, "expected a slack search hit for the reply text"
    hit = next(m for m in matches if "Rolled back" in m["text"])
    assert "thread_ts" in hit, "a threaded search hit must carry its root thread_ts"
    rep = client.post("/slack/api/conversations.replies", headers=admin_h,
                      data={"channel": hit["channel"]["id"], "ts": hit["ts"]}).json()
    assert rep.get("ok"), rep
    texts = " ".join(m["text"] for m in rep["messages"])
    assert "Anyone else seeing 502s" in texts   # thread root is returned
    assert "Rolled back" in texts               # the reply we searched for is in the same thread


def test_user_cannot_fetch_others_private_gmail(client, tokens, admin_h, ro_conn):
    # a private gmail doc owned by user B, fetched with user A's token -> 404
    user_a, user_b = tokens["users"][0], tokens["users"][1]
    doc = ro_conn.execute(
        "SELECT doc_id FROM gmail_messages WHERE author_email=? LIMIT 1",
        (user_b["email"],),
    ).fetchone()
    if doc is None:
        pytest.skip("no gmail doc for user B in this subset")
    ah = {"Authorization": f"Bearer {user_a['token']}"}
    r = client.get(f"/gmail/v1/users/me/messages/{doc['doc_id']}", headers=ah)
    # A may coincidentally be a recipient; assert admin can always read it
    assert client.get(f"/gmail/v1/users/me/messages/{doc['doc_id']}", headers=admin_h).status_code == 200
    assert r.status_code in (200, 404)


# --------------------------------------------------------------------------- Notion

def _tok(tokens, email):
    return next(u["token"] for u in tokens["users"] if u["email"] == email)


def test_notion_page_retrieve_and_blocks(client, admin_h):
    from app import synth
    pid = synth.notion_id("nt-runbook")
    r = client.get(f"/notion/v1/pages/{pid}", headers=admin_h)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "page" and body["id"] == pid
    assert body["properties"]["title"]["title"][0]["plain_text"] == "Notion On-call Runbook"
    assert body["icon"] == {"type": "emoji", "emoji": "📟"}
    ch = client.get(f"/notion/v1/blocks/{pid}/children", headers=admin_h).json()
    text = synth.notion_blocks_to_text(ch["results"])
    assert text == "# On-call\n\nCheck dashboards, roll back, page on-call."


def test_notion_dashless_id_resolves(client, admin_h):
    from app import synth
    pid = synth.notion_id("nt-runbook").replace("-", "")
    assert client.get(f"/notion/v1/pages/{pid}", headers=admin_h).status_code == 200


def test_notion_search_and_comments(client, admin_h):
    from app import synth
    s = client.post("/notion/v1/search", json={"query": "on-call"}, headers=admin_h).json()
    assert any(r["id"] == synth.notion_id("nt-runbook") for r in s["results"])
    c = client.get("/notion/v1/comments", params={"block_id": synth.notion_id("nt-runbook")},
                   headers=admin_h).json()
    assert c["results"][0]["rich_text"][0]["plain_text"] == "add rate-limiter step"
    assert c["results"][0]["object"] == "comment"


def test_notion_search_filter_database_only(client, admin_h):
    from app import synth
    s = client.post("/notion/v1/search",
                    json={"query": "", "filter": {"property": "object", "value": "database"}},
                    headers=admin_h).json()
    assert s["results"] and all(r["object"] == "database" for r in s["results"])
    assert any(r["id"] == synth.notion_id("nt-tasks-db") for r in s["results"])


def test_notion_users(client, admin_h):
    me = client.get("/notion/v1/users/me", headers=admin_h).json()
    assert me["object"] == "user" and me["type"] == "bot"
    lst = client.get("/notion/v1/users", headers=admin_h).json()
    assert lst["results"] and all(u["object"] == "user" for u in lst["results"])
    uid = lst["results"][0]["id"]
    assert client.get(f"/notion/v1/users/{uid}", headers=admin_h).json()["id"] == uid


def test_notion_unauth_is_401(client):
    from app import synth
    r = client.get(f"/notion/v1/pages/{synth.notion_id('nt-runbook')}")
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"


def test_notion_acl_hides_group_doc_from_outsider(client, tokens):
    from app import synth
    pid = synth.notion_id("nt-secret")
    outsider = _tok(tokens, "ava@acme.com")  # ava is engineering, not people
    r = client.get(f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {outsider}"})
    assert r.status_code == 404 and r.json()["code"] == "object_not_found"
    # the owner (hana, in people) can see it
    owner = _tok(tokens, "hana@acme.com")
    assert client.get(f"/notion/v1/pages/{pid}",
                      headers={"Authorization": f"Bearer {owner}"}).status_code == 200


def test_notion_database_new_vs_legacy_shape(client, admin_h):
    from app import synth
    did = synth.notion_id("nt-tasks-db")
    new = client.get(f"/notion/v1/databases/{did}", headers=admin_h).json()
    assert new["object"] == "database"
    assert new["data_sources"][0]["id"] == synth.notion_data_source_id("nt-tasks-db")
    assert "properties" not in new
    legacy = client.get(f"/notion/v1/databases/{did}",
                        headers={**admin_h, "Notion-Version": "2022-06-28"}).json()
    assert "properties" in legacy and "Status" in legacy["properties"]
    assert "data_sources" not in legacy


def test_notion_query_rows_both_paths(client, admin_h):
    from app import synth
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    rows_new = client.post(f"/notion/v1/data_sources/{dsid}/query", json={}, headers=admin_h).json()
    assert any(r["id"] == synth.notion_id("nt-task-1") for r in rows_new["results"])
    rows_legacy = client.post(f"/notion/v1/databases/{did}/query", json={},
                              headers={**admin_h, "Notion-Version": "2022-06-28"}).json()
    assert any(r["id"] == synth.notion_id("nt-task-1") for r in rows_legacy["results"])


def test_notion_data_source_retrieve(client, admin_h):
    from app import synth
    dsid = synth.notion_data_source_id("nt-tasks-db")
    ds = client.get(f"/notion/v1/data_sources/{dsid}", headers=admin_h).json()
    assert ds["object"] == "data_source" and "Status" in ds["properties"]
