from app import store


def _conn(tmp_path):
    conn = store.connect_rw(tmp_path / "s3.sqlite")
    conn.execute(
        "INSERT INTO s3_objects (doc_id, bucket, author_email, key, title, content, "
        "subtype, content_type, size, created_ts, updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("o1", "eng-artifacts", "ava@acme.com", "runbooks/oncall.md", "oncall",
         "body text", "STANDARD", "text/markdown", 9, 1_700_000_000, 1_700_000_001),
    )
    conn.execute("INSERT INTO s3_buckets (bucket, group_id) VALUES (?,?)",
                 ("eng-artifacts", "engineering"))
    conn.commit()
    return conn


def test_s3_registry_wiring():
    assert store.table("s3") == "s3_objects"
    assert store.grouping_col("s3") == "bucket"
    assert store.comment_table("s3") is None  # S3 has no comments


def test_s3_store_reads(tmp_path):
    conn = _conn(tmp_path)
    rows = store.list_documents(conn, "s3", container="eng-artifacts")
    assert [r["key"] for r in rows] == ["runbooks/oncall.md"]
    got = store.get_document(conn, "s3", "o1")
    assert got["content"] == "body text" and got["content_type"] == "text/markdown"
    assert store.get_container(conn, "s3", "eng-artifacts")["group_id"] == "engineering"
