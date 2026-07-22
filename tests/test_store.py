"""Tests for the read-only SQLite store layer (`app.store`).

The store is shared by every router, search, and the importers, so it gets its own file rather
than being verified incidentally through a load/route test. Registry wiring is checked across
every source; generic reads run against the shared SAMPLE corpus (the `db` fixture); connection
tuning uses hand-built / SAMPLE DBs.

ACL-filtered reads live in test_acl.py (the ACL is the subject there) and FTS search in
test_search.py (search is its own sub-domain); this file covers the plain store surface.
"""
import sqlite3

import pytest

from app import store

ALL_SOURCES = ["slack", "gmail", "google_drive", "github", "jira", "confluence", "notion", "s3"]


# --- registry wiring ------------------------------------------------------------

def test_registry_covers_every_source():
    assert set(store.SOURCE_TABLE) == set(ALL_SOURCES)
    for src in ALL_SOURCES:
        assert store.table(src)            # source -> table resolves
        assert store.grouping_table(src)   # source -> grouping table resolves
        assert store.grouping_col(src)     # source -> grouping column resolves


def test_unknown_source_type_raises():
    with pytest.raises(ValueError):
        store.table("nope")


def test_grouping_cols_per_source():
    assert store.grouping_col("slack") == "channel"
    assert store.grouping_col("gmail") == "mailbox"
    assert store.grouping_col("google_drive") == "folder"
    assert store.grouping_col("github") == "repo"
    assert store.grouping_col("jira") == "project"
    assert store.grouping_col("confluence") == "space"
    assert store.grouping_col("notion") == "teamspace"
    assert store.grouping_col("s3") == "bucket"


def test_comment_tables_only_where_supported():
    # jira/confluence/github/notion expose comments; slack/gmail/drive/s3 do not
    assert store.comment_table("jira") == "jira_comments"
    assert store.comment_table("confluence") == "confluence_comments"
    assert store.comment_table("github") == "github_comments"
    assert store.comment_table("notion") == "notion_comments"
    for src in ("slack", "gmail", "google_drive", "s3"):
        assert store.comment_table(src) is None


# --- generic reads over the SAMPLE corpus ---------------------------------------

def test_get_document(db):
    doc = store.get_document(db, "confluence", "cf-handbook")
    assert doc["title"] == "Engineering Handbook"
    assert store.get_document(db, "confluence", "no-such-doc") is None


def test_list_documents_container_scope(db):
    keys = {r["doc_id"] for r in store.list_documents(db, "jira", container="payments", limit=100)}
    assert {"jira-sev2", "jira-sub1", "jira-private"} <= keys
    assert store.list_documents(db, "jira", container="no-such-project", limit=100) == []


def test_list_documents_author_scope(db):
    rows = store.list_documents(db, "confluence", author_email="ava@acme.com", limit=100)
    assert rows and all(r["author_email"] == "ava@acme.com" for r in rows)


def test_count_documents(db):
    assert store.count_documents(db, "jira", container="payments") >= 3
    assert store.count_documents(db, "confluence") >= 3
    assert store.count_documents(db, "jira", container="no-such-project") == 0


def test_list_documents_state_filter(db):
    # gateway repo: gh-issue-1 is open (state NULL/"open"), gh-pr-1 is closed
    open_ids = {r["doc_id"] for r in store.list_documents(db, "github", container="gateway",
                                                          limit=100, state="open")}
    closed_ids = {r["doc_id"] for r in store.list_documents(db, "github", container="gateway",
                                                            limit=100, state="closed")}
    assert "gh-issue-1" in open_ids and "gh-pr-1" not in open_ids
    assert "gh-pr-1" in closed_ids and "gh-issue-1" not in closed_ids
    # state=None (default) applies no filter -> both present
    all_ids = {r["doc_id"] for r in store.list_documents(db, "github", container="gateway", limit=100)}
    assert {"gh-issue-1", "gh-pr-1"} <= all_ids


def test_count_documents_state_filter(db):
    assert store.count_documents(db, "github", container="gateway", state="open") == 1
    assert store.count_documents(db, "github", container="gateway", state="closed") == 1
    assert store.count_documents(db, "github", container="gateway") >= 2


def test_children(db):
    # jira-sub1 is a subtask of jira-sev2; cf-oncall is a child page of cf-handbook
    assert "jira-sub1" in {r["doc_id"] for r in store.children(db, "jira", "jira-sev2")}
    assert "cf-oncall" in {r["doc_id"] for r in store.children(db, "confluence", "cf-handbook")}


def test_doc_comments(db):
    cmts = store.doc_comments(db, "confluence", "cf-oncall")
    assert len(cmts) == 1 and "rate-limiter" in cmts[0]["body"]
    # a source with no comment table returns [] rather than erroring
    assert store.doc_comments(db, "slack", "whatever") == []


def test_containers(db):
    names = {c["name"] for c in store.list_containers(db, "confluence")}
    assert {"handbook", "people-ops"} <= names
    assert store.get_container(db, "confluence", "handbook")["group_id"] == "engineering"
    assert store.get_container(db, "confluence", "no-such") is None
    # S3 buckets are the grouping unit; both eng-artifacts objects share the engineering group
    assert store.get_container(db, "s3", "eng-artifacts")["group_id"] == "engineering"


def test_users(db):
    emails = set(store.all_user_emails(db))
    assert "ava@acme.com" in emails
    assert store.get_user(db, "ava@acme.com") is not None
    assert store.get_user(db, "nobody@acme.com") is None


def test_jcol_parses_json_columns(db):
    issue = store.get_document(db, "github", "gh-issue-1")
    assert store.jcol(issue, "labels") == ["bug", "gateway"]      # JSON-valued TEXT column
    assert store.jcol(issue, "no_such_col", default=["x"]) == ["x"]


# --- S3: SQL-pushed prefix / keyset pagination / ACL scoping --------------------

def _s3_mini_db(tmp_path):
    """A hand-built DB with two buckets and a handful of objects — enough to exercise
    prefix/keyset/ACL without going through the full BYO importer."""
    conn = store.connect_rw(tmp_path / "s3.sqlite")
    rows = [
        # (doc_id, bucket, key)
        ("d1", "b", "logs/2026/01/a.json"),
        ("d2", "b", "logs/2026/01/b.json"),
        ("d3", "b", "logs/2026/02/a.json"),
        ("d4", "b", "notes/readme.md"),
        ("d5", "other", "logs/2026/01/a.json"),   # same key, different bucket
    ]
    for doc_id, bucket, key in rows:
        conn.execute(
            "INSERT INTO s3_objects(doc_id, bucket, author_email, title, content, key, "
            "created_ts) VALUES (?,?,?,?,?,?,1)",
            (doc_id, bucket, "a@x.com", key, "body", key))
    # d2 is ACL-restricted to group 'eng'; everything else is unrestricted (no doc_acl row ->
    # _acl_clause's EXISTS check only bites rows it has an entry for).
    conn.execute("INSERT INTO doc_acl VALUES ('d2','group','eng')")
    conn.commit()
    return conn


def test_list_s3_objects_prefix_and_order(tmp_path):
    conn = _s3_mini_db(tmp_path)
    rows = store.list_s3_objects(conn, "b", prefix="logs/2026/01/")
    assert [r["key"] for r in rows] == ["logs/2026/01/a.json", "logs/2026/01/b.json"]
    # a bucket-only listing stays sorted and scoped to that bucket (d5 in "other" excluded)
    rows = store.list_s3_objects(conn, "b")
    assert [r["key"] for r in rows] == ["logs/2026/01/a.json", "logs/2026/01/b.json",
                                        "logs/2026/02/a.json", "notes/readme.md"]


def test_list_s3_objects_prefix_escapes_like_wildcards(tmp_path):
    conn = _s3_mini_db(tmp_path)
    # a literal '_' or '%' in the prefix must not act as a SQL LIKE wildcard
    assert store.list_s3_objects(conn, "b", prefix="logs_2026") == []
    assert store.list_s3_objects(conn, "b", prefix="logs%") == []


def test_list_s3_objects_keyset_pagination(tmp_path):
    conn = _s3_mini_db(tmp_path)
    page1 = store.list_s3_objects(conn, "b", limit=2)
    assert [r["key"] for r in page1] == ["logs/2026/01/a.json", "logs/2026/01/b.json"]
    page2 = store.list_s3_objects(conn, "b", start_after=page1[-1]["key"], limit=2)
    assert [r["key"] for r in page2] == ["logs/2026/02/a.json", "notes/readme.md"]
    # keyset pagination never re-returns the boundary key, and pages don't overlap
    assert not {r["key"] for r in page1} & {r["key"] for r in page2}


def test_list_s3_objects_acl_scoped(tmp_path):
    conn = _s3_mini_db(tmp_path)
    all_keys = {r["key"] for r in store.list_s3_objects(conn, "b", prefix="logs/2026/01/")}
    assert all_keys == {"logs/2026/01/a.json", "logs/2026/01/b.json"}
    scoped_keys = {r["key"] for r in
                  store.list_s3_objects(conn, "b", prefix="logs/2026/01/", visible_ids={"eng"})}
    assert scoped_keys == {"logs/2026/01/b.json"}          # only d2, granted to group 'eng'
    none_visible = store.list_s3_objects(conn, "b", prefix="logs/2026/01/", visible_ids={"nobody"})
    assert none_visible == []


# --- connection tuning ----------------------------------------------------------

def test_connect_rw_busy_timeout(tmp_path):
    c = store.connect_rw(tmp_path / "rw.sqlite", busy_ms=12345)
    try:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 12345
    finally:
        c.close()


def test_connect_ro_tuning(sample_settings):
    # tuned connection applies the pragmas; a plain one keeps sqlite defaults (tests unaffected)
    c = store.connect_ro(sample_settings.db_path, mmap_mb=64, cache_mb=16, temp_memory=True)
    try:
        assert c.execute("PRAGMA cache_size").fetchone()[0] == -16 * 1024
        assert c.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
        assert c.execute("PRAGMA mmap_size").fetchone()[0] > 0
    finally:
        c.close()
    d = store.connect_ro(sample_settings.db_path)
    try:
        assert d.execute("PRAGMA mmap_size").fetchone()[0] == 0
    finally:
        d.close()


# --- incremental FTS indexing --------------------------------------------------

def _mini_db(tmp_path):
    conn = store.connect_rw(tmp_path / "m.sqlite")
    conn.execute("INSERT INTO notion_pages(doc_id,teamspace,author_email,title,content,created_ts) "
                 "VALUES('n1','eng','a@x.com','Alpha runbook','deploy alpha service',1)")
    conn.commit()
    store.build_fts(conn)
    return conn


def test_fts_add_docs_indexes_new_without_dropping_old(tmp_path):
    conn = _mini_db(tmp_path)
    # a new page inserted AFTER the initial build is not searchable until indexed
    conn.execute("INSERT INTO notion_pages(doc_id,teamspace,author_email,title,content,created_ts) "
                 "VALUES('n2','eng','a@x.com','Beta guide','rotate beta credentials',2)")
    conn.commit()
    assert store.search_documents(conn, "beta", "notion") == []          # not yet indexed
    n = store.fts_add_docs(conn, "notion", ["n2"])
    assert n == 1
    got = {r["doc_id"] for r in store.search_documents(conn, "beta", "notion")}
    assert "n2" in got
    # the original doc is still searchable (index not clobbered)
    assert {r["doc_id"] for r in store.search_documents(conn, "alpha", "notion")} == {"n1"}


def test_fts_add_docs_is_idempotent(tmp_path):
    conn = _mini_db(tmp_path)
    store.fts_add_docs(conn, "notion", ["n1"])          # re-index existing doc
    assert len(store.search_documents(conn, "alpha", "notion")) == 1     # no duplicate row


def test_fts_add_docs_noop_without_index(tmp_path):
    conn = store.connect_rw(tmp_path / "n.sqlite")       # no build_fts called
    assert store.fts_add_docs(conn, "notion", ["x"]) == 0
