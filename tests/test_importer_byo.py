"""app.importer.byo: load an arbitrary BYO JSONL corpus -> DB, honoring per-doc ACL."""
import json

import pytest
import yaml

from app import store
from app.acl import Acl
from app.config import Settings
from app.routers.slack import _message
from app.importer import byo
from app.importer.byo import load


def _write(tmp_path, records):
    p = tmp_path / "corpus.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records))
    return p


def test_byo_load_and_acl(tmp_path):
    corpus = _write(tmp_path, [
        {"source_type": "confluence", "title": "Public", "content": "x", "visibility": "public"},
        {"source_type": "confluence", "title": "Secret", "content": "y", "space": "ppl",
         "group": "people", "author_email": "hana@a.com", "author_groups": ["people"],
         "visibility": "group"},
        {"source_type": "jira", "title": "Mine", "content": "z", "author_email": "bob@a.com",
         "visibility": "private"},
    ])
    settings = Settings(data_dir=tmp_path)
    res = load(corpus, settings)
    assert res["total"] == 3

    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    tokens = {u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]}

    def visible_titles(token, source):
        ids = acl.visible_ids(conn, acl.resolve(token))
        return sorted(r["title"] for r in store.list_documents(conn, source, visible_ids=ids, limit=50))

    # admin (None) sees everything
    assert sorted(r["title"] for r in store.list_documents(conn, "confluence", limit=50)) == ["Public", "Secret"]
    # hana is in 'people' -> sees the group-restricted page; a non-member does not
    assert visible_titles(tokens["hana@a.com"], "confluence") == ["Public", "Secret"]
    assert visible_titles(tokens["bob@a.com"], "confluence") == ["Public"]
    # private jira doc visible only to its author
    assert visible_titles(tokens["bob@a.com"], "jira") == ["Mine"]
    assert visible_titles(tokens["hana@a.com"], "jira") == []


def test_byo_readers_and_defaults(tmp_path):
    corpus = _write(tmp_path, [
        {"source_type": "gmail", "title": "Deck", "content": "c", "author_email": "ceo@a.com",
         "readers": ["ceo@a.com", "ava@a.com"]},
        {"source_type": "slack", "title": "hi", "content": "c"},  # no author, no visibility -> public + dsid_ id
    ])
    settings = Settings(data_dir=tmp_path)
    res = load(corpus, settings)
    # the org is derived from the corpus's dominant email domain (a.com), not the default
    assert res["org"] == "a" and res["org_domain"] == "a.com"
    conn = store.connect_ro(settings.db_path)
    acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    assert acl.org_name == "a"  # Acl.load picks up the derived org from tokens.yaml
    tokens = {u["email"]: u["token"] for u in yaml.safe_load(settings.tokens_path.read_text())["users"]}

    # explicit readers: ava can see the deck doc; a stranger cannot
    deck = conn.execute("SELECT doc_id FROM gmail_messages").fetchone()["doc_id"]
    ava_ids = acl.visible_ids(conn, acl.resolve(tokens["ava@a.com"]))
    assert store.get_document(conn, "gmail", deck, visible_ids=ava_ids) is not None
    assert store.get_document(conn, "gmail", deck, visible_ids={"nobody@a.com"}) is None
    # no-author doc got a generated dsid_ id and is org-public (any real caller's
    # visible_ids includes the org sentinel = the derived org)
    slack = conn.execute("SELECT doc_id FROM slack_messages").fetchone()["doc_id"]
    assert slack.startswith("dsid_")
    assert store.get_document(conn, "slack", slack, visible_ids={res["org"]}) is not None


def test_slack_title_optional(tmp_path):
    # slack needs no title; the other sources still require one
    load(_write(tmp_path, [{"source_type": "slack", "content": "deploy freeze Friday"}]),
         Settings(data_dir=tmp_path))
    conn = store.connect_ro((tmp_path / "mock.sqlite"))
    assert conn.execute("SELECT title FROM slack_messages").fetchone()["title"] == ""

    with pytest.raises(SystemExit):
        load(_write(tmp_path, [{"source_type": "confluence", "content": "no title here"}]),
             Settings(data_dir=tmp_path))


def _row(**kw):
    kw.setdefault("thread_id", None)
    kw.setdefault("thread_seq", 0)
    kw.setdefault("subtype", None)
    kw.setdefault("created_ts", None)
    kw.setdefault("meta", None)
    return kw


def test_byo_meta_comments_hierarchy(tmp_path):
    load(_write(tmp_path, [
        {"source_type": "confluence", "title": "Parent", "content": "p", "doc_id": "pg-root",
         "labels": ["engineering"]},
        {"source_type": "confluence", "title": "Child", "content": "c", "doc_id": "pg-child",
         "parent": "pg-root", "comments": [{"content": "looks good", "author_email": "rev@a.com"}]},
        {"source_type": "jira", "title": "Bug", "content": "b",
         "meta": {"issuelinks": [{"key": "X-1"}]},
         "comments": [{"content": "fixed in main", "author_email": "dev@a.com"}]},
    ]), Settings(data_dir=tmp_path))
    conn = store.connect_ro(tmp_path / "mock.sqlite")

    # meta blob on a doc
    assert store.jcol(store.get_document(conn, "confluence", "pg-root"), "labels") == ["engineering"]
    # parent/child hierarchy
    kids = store.children(conn, "confluence", "pg-root")
    assert [k["doc_id"] for k in kids] == ["pg-child"]
    # comments attached to a doc
    cs = store.doc_comments(conn, "confluence", "pg-child")
    assert len(cs) == 1 and cs[0]["body"] == "looks good"
    # jira meta + comments
    bug = conn.execute("SELECT * FROM jira_issues").fetchone()
    assert store.jcol(bug, "issuelinks")[0]["key"] == "X-1"
    assert len(store.doc_comments(conn, "jira", bug["doc_id"])) == 1


def test_slack_message_text_without_title():
    # empty title -> the message text is just the content (no bold lead line)
    assert _message(_row(doc_id="d1", title="", content="hi", author_email="a@x.com"))["text"] == "hi"
    assert _message(_row(doc_id="d2", title="T", content="hi", author_email="a@x.com"))["text"] == "*T*\nhi"
    # a standalone message has no thread_ts / reply_count
    assert "thread_ts" not in _message(_row(doc_id="d1", title="", content="hi", author_email="a@x.com"))


def test_byo_slack_threads(tmp_path):
    load(_write(tmp_path, [{
        "source_type": "slack", "content": "seeing 502s?", "channel": "incidents",
        "author_email": "bob@a.com",
        "replies": [
            {"content": "looking", "author_email": "ava@a.com"},
            {"content": "rolled back", "author_email": "bob@a.com"},
        ],
    }]), Settings(data_dir=tmp_path))
    conn = store.connect_ro(tmp_path / "mock.sqlite")

    # 3 docs total (root + 2 replies), but only the root is top-level
    assert conn.execute("SELECT COUNT(*) FROM slack_messages").fetchone()[0] == 3
    tops = store.list_slack_top_level(conn, "incidents", limit=50)
    assert len(tops) == 1
    root = tops[0]
    assert store.slack_reply_count(conn, root["doc_id"]) == 2

    thread = store.slack_thread(conn, root["doc_id"])
    assert [r["thread_seq"] for r in thread] == [0, 1, 2]
    # replies share the root's thread_ts and sort strictly after it
    from app.routers.slack import _msg_ts
    ts = [_msg_ts(r) for r in thread]
    assert ts == sorted(ts) and ts[0] < ts[1] < ts[2]


def _epoch(iso):
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def test_byo_created_updated_times(tmp_path):
    load(_write(tmp_path, [
        {"source_type": "jira", "title": "T", "content": "c", "doc_id": "j1",
         "created": "2026-03-01T09:00:00Z", "updated": 1740900000},
        {"source_type": "google_drive", "title": "D", "content": "c", "doc_id": "d1",
         "created": "2026-01-15T00:00:00Z"},
    ]), Settings(data_dir=tmp_path))
    conn = store.connect_ro(tmp_path / "mock.sqlite")

    # created accepts ISO, updated accepts epoch int — both land as epoch seconds
    j = conn.execute("SELECT created_ts, updated_ts FROM jira_issues WHERE doc_id='j1'").fetchone()
    assert j["created_ts"] == _epoch("2026-03-01T09:00:00Z")
    assert j["updated_ts"] == 1740900000

    # and reach the router response
    from starlette.requests import Request
    from app.routers.atlassian import _jira_issue
    req = Request({"type": "http", "headers": [], "query_string": b"",
                   "scheme": "http", "server": ("t", 80), "path": "/"})
    fields = _jira_issue(conn, req, store.get_document(conn, "jira", "j1"))["fields"]
    assert fields["created"].startswith("2026-03-01T09:00:00")
    assert fields["updated"].startswith("2025-03-02")  # 1740900000 -> 2025-03-02

    # updated defaults to created + 1h when omitted (drive)
    d = conn.execute("SELECT created_ts, updated_ts FROM gdrive_files WHERE doc_id='d1'").fetchone()
    assert d["created_ts"] == _epoch("2026-01-15T00:00:00Z") and d["updated_ts"] is None


def test_byo_gmail_created_and_to(tmp_path):
    load(_write(tmp_path, [
        {"source_type": "gmail", "title": "Hi", "content": "body", "doc_id": "m1", "mailbox": "ceo",
         "to": "board@acme.com", "created": "2026-04-01T12:00:00Z"},
    ]), Settings(data_dir=tmp_path))
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    from app.routers.google import _gmail_message
    msg = _gmail_message(store.get_document(conn, "gmail", "m1"), "metadata")
    assert msg["internalDate"] == str(_epoch("2026-04-01T12:00:00Z") * 1000)
    to = next(h["value"] for h in msg["payload"]["headers"] if h["name"] == "To")
    assert to == "board@acme.com"


def test_byo_slack_rich_replies(tmp_path):
    load(_write(tmp_path, [{
        "source_type": "slack", "content": "root", "channel": "incidents", "doc_id": "s-root",
        "author_email": "bob@a.com", "created": "2026-05-01T00:00:00Z",
        "replies": [
            {"content": "on it", "author_email": "ava@a.com",
             "reactions": [{"name": "eyes", "count": 1, "users": ["U1"]}], "subtype": "thread_broadcast"},
        ],
    }]), Settings(data_dir=tmp_path))
    conn = store.connect_ro(tmp_path / "mock.sqlite")
    from app.routers.slack import _message, _msg_ts

    thread = store.slack_thread(conn, "s-root")
    root, reply = thread[0], thread[1]
    # root ts reflects the caller-supplied created; reply follows one second later
    assert _msg_ts(root) == f"{_epoch('2026-05-01T00:00:00Z')}.{_msg_ts(root).split('.')[1]}"
    assert _msg_ts(reply) > _msg_ts(root)
    # reply carries the full message fields (reactions + subtype), not just content
    rm = _message(reply)
    assert rm["reactions"][0]["name"] == "eyes" and rm["subtype"] == "thread_broadcast"
    # reply shares the root's thread_ts
    assert rm["thread_ts"] == _message(root, reply_count=1)["thread_ts"] == _msg_ts(root)


def test_notion_byo_load(tmp_path):
    corpus = _write(tmp_path, [
        {"source_type": "notion", "teamspace": "eng", "title": "Runbook",
         "content": "# Heading\n\nBody line.", "doc_id": "n-page",
         "author_email": "ava@acme.com", "visibility": "public",
         "icon": "🚀",
         "comments": [{"content": "nit", "author_email": "bob@acme.com"}]},
        {"source_type": "notion", "teamspace": "eng", "subtype": "database",
         "title": "Tasks", "content": "Task tracker", "doc_id": "n-db",
         "author_email": "ava@acme.com", "visibility": "public",
         "properties": {"Status": {"type": "select"}}},
        {"source_type": "notion", "teamspace": "eng", "title": "Fix gateway",
         "content": "row body", "doc_id": "n-row", "parent": "n-db",
         "author_email": "ava@acme.com", "visibility": "public",
         "properties": {"Status": "In Progress"}},
    ])
    settings = Settings(data_dir=tmp_path)
    res = load(corpus, settings)
    assert res["counts"]["notion"] == 3

    conn = store.connect_ro(settings.db_path)
    row = store.get_document(conn, "notion", "n-row")
    assert row["parent_id"] == "n-db" and row["teamspace"] == "eng"
    assert '"Status"' in row["properties"]
    db = store.get_document(conn, "notion", "n-db")
    assert db["subtype"] == "database"
    page = store.get_document(conn, "notion", "n-page")
    assert page["icon"] == "🚀"
    assert len(store.doc_comments(conn, "notion", "n-page")) == 1
    assert store.get_container(conn, "notion", "eng") is not None
    conn.close()


def test_notion_byo_rejects_bad_subtype():
    from app.validation import record_errors
    errs = record_errors({"source_type": "notion", "title": "x", "content": "y",
                          "subtype": "wiki"})
    assert any("subtype" in e for e in errs)


def test_s3_byo_load(tmp_path):
    unicode_body = "résumé ☕ dashboards"  # multibyte: size is the UTF-8 byte length, not char count
    records = [
        {"source_type": "s3", "bucket": "eng-artifacts", "key": "runbooks/oncall.md",
         "title": "On-call Runbook", "content": "check dashboards, roll back, page on-call",
         "content_type": "text/markdown", "author_email": "ava@acme.com",
         "author_groups": ["engineering"], "visibility": "public"},
        {"source_type": "s3", "bucket": "eng-artifacts", "key": "secret/comp.txt",
         "title": "Comp", "content": "confidential", "author_email": "hana@acme.com",
         "author_groups": ["people"], "visibility": "group", "group": "people"},
        {"source_type": "s3", "bucket": "eng-artifacts", "key": "notes/unicode.md",
         "title": "Unicode", "content": unicode_body, "content_type": "text/markdown",
         "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public"},
    ]
    corpus = tmp_path / "s3.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in records))
    settings = Settings(data_dir=tmp_path)
    res = load(corpus, settings)
    assert res["counts"]["s3"] == 3
    conn = store.connect_ro(settings.db_path)
    rows = {r["key"]: r for r in store.list_documents(conn, "s3", container="eng-artifacts")}
    assert rows["runbooks/oncall.md"]["content_type"] == "text/markdown"
    assert rows["runbooks/oncall.md"]["size"] == len("check dashboards, roll back, page on-call")
    # size is the UTF-8 byte length, which is strictly greater than the character count here
    assert rows["notes/unicode.md"]["size"] == len(unicode_body.encode("utf-8"))
    assert rows["notes/unicode.md"]["size"] != len(unicode_body)
    assert store.get_container(conn, "s3", "eng-artifacts") is not None
    conn.close()


def test_s3_byo_rejects_missing_key(tmp_path):
    corpus = tmp_path / "bad.jsonl"
    corpus.write_text(json.dumps(
        {"source_type": "s3", "bucket": "b", "title": "t", "content": "c"}))  # no key
    with pytest.raises(SystemExit):
        load(corpus, Settings(data_dir=tmp_path))


def _corpus(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(x) for x in lines))
    return p


def test_append_preserves_prior_roster_and_org(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_DATA_DIR", str(tmp_path))
    from app.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    a = _corpus(tmp_path, "a.jsonl", [
        {"source_type": "confluence", "title": "A", "content": "alpha",
         "space": "ENG", "author_email": "ann@acme.com", "visibility": "group", "group": "eng"}])
    byo.load(a, s, reset=True)
    prev_users = {u["email"] for u in yaml.safe_load(s.tokens_path.read_text())["users"]}
    prev_org = yaml.safe_load(s.tokens_path.read_text())["org"]
    b = _corpus(tmp_path, "b.jsonl", [
        {"source_type": "notion", "title": "B", "content": "beta rotate",
         "teamspace": "ops", "author_email": "bob@redwoodinference.com",
         "visibility": "group", "group": "ops"}])
    byo.load(b, s, reset=False)
    tok = yaml.safe_load(s.tokens_path.read_text())
    now_users = {u["email"] for u in tok["users"]}
    assert "ann@acme.com" in now_users and "bob@redwoodinference.com" in now_users  # union
    assert prev_users <= now_users
    assert tok["org"] == prev_org                                                    # org unchanged


def test_append_incremental_fts_finds_new_and_keeps_old(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_DATA_DIR", str(tmp_path))
    from app.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    byo.load(_corpus(tmp_path, "a.jsonl", [
        {"source_type": "confluence", "title": "A", "content": "alpha unique",
         "space": "ENG", "author_email": "ann@acme.com", "visibility": "group", "group": "eng"}]),
        s, reset=True)
    byo.load(_corpus(tmp_path, "b.jsonl", [
        {"source_type": "notion", "title": "B", "content": "beta unique",
         "teamspace": "ops", "author_email": "bob@acme.com", "visibility": "group", "group": "ops"}]),
        s, reset=False)
    conn = store.connect_ro(s.db_path)
    assert len(store.search_documents(conn, "beta", "notion")) == 1       # new doc indexed
    assert len(store.search_documents(conn, "alpha", "confluence")) == 1  # old doc still indexed
    conn.close()
