import json
import os
import shutil
import sqlite3
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import pytest
import yaml

from app import store
from app.config import get_settings
from app.importer import erb
from app.importer.erb import Principals, canonical, grants_for

C = erb


# ---------------------------------------------------------------------------
# from test_erb_source.py
# ---------------------------------------------------------------------------

def test_derive_title_content_scalar():
    raw = {"title_field_name": "title", "content_field_names": ["body", "body_addendum"],
           "title": "Doc A", "body": "hello", "body_addendum": "world"}
    title, content = erb.derive_title_content(raw)
    assert title == "Doc A"
    assert "hello" in content and "world" in content


def test_derive_title_content_list_field():
    raw = {"title_field_name": "channel", "content_field_names": ["messages"],
           "channel": "eng-infra", "messages": "Alex: hi\nMaria: yo"}
    title, content = erb.derive_title_content(raw)
    assert title == "eng-infra"
    assert "Alex: hi" in content


def test_supported_sources():
    assert erb.SUPPORTED == ("slack", "gmail", "google_drive", "github", "jira", "confluence")


# ---------------------------------------------------------------------------
# from test_principals.py
# ---------------------------------------------------------------------------

EMPLOYEES = [
    {"name": "Ava Chen", "email": "ava.chen@redwoodinference.com", "dept_slug": "engineering"},
]


def _p():
    return Principals(list(EMPLOYEES), "redwoodinference.com")


def test_canonical_strips_punctuation_and_case():
    assert canonical("Connor O'Brien") == canonical("Connor OBrien") == "connorobrien"
    assert canonical("Ava  Chen") == "avachen"


def test_canonical_drops_middle_initials():
    # 'Aisha K. Patel' and 'Aisha Patel' are the same person; 'Asha Patel' is not
    assert canonical("Aisha K. Patel") == canonical("Aisha Patel") == "aishapatel"
    assert canonical("Asha Patel") == "ashapatel" != "aishapatel"


def test_resolve_directory_match():
    p = _p()
    assert p.resolve("Ava Chen", role="author") == "ava.chen@redwoodinference.com"


def test_resolve_synthesizes_internal_user():
    p = _p()
    email = p.resolve("Maya Chen", role="owner", group_hint="research-applied-ml")
    assert email == "maya.chen@redwoodinference.com"
    assert p.users[email]["group"] == "research-applied-ml"
    assert p.users[email]["external"] is False


def test_resolve_external_parses_email_and_is_not_registered():
    # 'Name <email>' → the real email, deduped by email; never becomes an org principal/user
    p = _p()
    email = p.resolve("Alyssa Chen <alyssa.chen@cascadefg.com>", role="participant_external")
    assert email == "alyssa.chen@cascadefg.com"
    assert email not in p.users  # externals are recipients, not org users


def test_resolve_external_bare_name_offdomain_and_not_registered():
    p = _p()
    email = p.resolve("Dana Ext", role="participant_external")
    assert not email.endswith("@redwoodinference.com")
    assert email not in p.users


def test_resolve_slack_speaker_is_label_not_registered():
    # first-names/bots become display labels only — not org users
    p = _p()
    email = p.resolve("infra-bot", role="slack_participant")
    assert email == "infrabot@redwoodinference.com"  # _slug strips the hyphen
    assert email not in p.users
    assert "alex@redwoodinference.com" == p.resolve("Alex", role="slack_participant")
    assert "alex@redwoodinference.com" not in p.users


def test_resolve_rejects_non_person_junk():
    # a lone single-word token in a name field is not a person → not minted
    p = _p()
    assert p.resolve("Note", role="author") is None
    assert "note@redwoodinference.com" not in p.users


def test_harvest_gmail_email_wins_over_synthesis():
    p = _p()
    rec = ("gmail", "dsid_x", {"title_field_name": "subject", "content_field_names": ["messages"],
            "subject": "s", "messages": ["From: Maya Chen <maya_chen@redwoodinference.com>\nTo: x\n\nhi"]})
    p.harvest_gmail_emails([rec])
    assert p.resolve("Maya Chen", role="author") == "maya_chen@redwoodinference.com"


def test_harvest_skips_alias_header_names():
    # a header alias like 'On-Call (SRE) <oncall@…>' is not a person → not harvested as a user
    p = _p()
    rec = ("gmail", "dsid_y", {"title_field_name": "subject", "content_field_names": ["messages"],
            "subject": "s", "messages": ["From: On-Call (SRE) <oncall@redwoodinference.com>\n\nhi"]})
    p.harvest_gmail_emails([rec])
    assert "oncall@redwoodinference.com" not in p.users


def _p_multi():
    employees = [
        {"name": "Ava Chen", "email": "ava.chen@redwoodinference.com", "dept_slug": "engineering"},
        {"name": "Maya Chen", "email": "maya.chen@redwoodinference.com", "dept_slug": "security-compliance"},
        {"name": "Priya Desai", "email": "priya.desai@redwoodinference.com", "dept_slug": "applied-ml-research"},
    ]
    return Principals(employees, "redwoodinference.com")


def test_canonical_group_reconciles_partial_team_label():
    p = _p_multi()
    assert p.canonical_group("security") == "security-compliance"


def test_canonical_group_exact_match():
    p = _p_multi()
    assert p.canonical_group("engineering") == "engineering"


def test_canonical_group_unknown_team_is_its_own_group():
    p = _p_multi()
    assert p.canonical_group("some-unknown-team") == "some-unknown-team"


def test_write_tokens_is_directory_only(tmp_path):
    import types, yaml as _yaml
    p = Principals([{"name": "Ava Chen", "email": "ava.chen@redwoodinference.com",
                     "dept_slug": "engineering"}], "redwoodinference.com")
    p.resolve("Maya Chen", role="owner", group_hint="engineering")   # synthesized, non-directory
    p.resolve("Wei Chen", role="reviewer")                            # synthesized, non-directory
    st = types.SimpleNamespace(tokens_path=tmp_path / "tokens.yaml", org_name="redwood",
                               org_domain="redwoodinference.com", admin_token="admin-service-token")
    p.write_tokens(st)
    d = _yaml.safe_load(st.tokens_path.read_text())
    emails = {u["email"] for u in d["users"]}
    assert emails == {"ava.chen@redwoodinference.com"}   # only the directory employee
    assert "maya.chen@redwoodinference.com" not in emails


def test_canonical_folds_accents():
    assert canonical("Tomáš Novák") == canonical("Tomas Novak") == "tomasnovak"


def test_mint_does_not_clobber_directory_user(tmp_path):
    # an accented/titled directory name whose doc-reference doesn't canonical-match must still
    # keep its directory flag (the colliding mint must not overwrite it) → stays tokened
    import types, yaml as _yaml
    p = Principals([{"name": "Tomáš Novák", "email": "tomas.novak@redwoodinference.com",
                     "dept_slug": "engineering"}], "redwoodinference.com")
    # a doc references the plain spelling; folded canonical now matches → resolves to the dir user
    assert p.resolve("Tomas Novak", role="owner") == "tomas.novak@redwoodinference.com"
    assert p.users["tomas.novak@redwoodinference.com"].get("directory") is True
    st = types.SimpleNamespace(tokens_path=tmp_path / "t.yaml", org_name="redwood",
                               org_domain="redwoodinference.com", admin_token="admin-service-token")
    p.write_tokens(st)
    assert "tomas.novak@redwoodinference.com" in {u["email"] for u in _yaml.safe_load(st.tokens_path.read_text())["users"]}


# ---------------------------------------------------------------------------
# from test_conversations.py
# ---------------------------------------------------------------------------

def test_parse_gmail_thread():
    msgs = ["From: Vivek K <vivek_k@redwoodinference.com>\n"
            "To: Connor O'Brien <connor_obrien@redwoodinference.com>\n"
            "Date: Wed, May 14, 2025 at 9:12 AM PT\nSubject: Beta plan\n\nBody one.",
            "From: Connor O'Brien <connor_obrien@redwoodinference.com>\n"
            "To: Vivek K <vivek_k@redwoodinference.com>\nDate: Wed, May 14, 2025 at 10:00 AM PT\n"
            "Subject: Re: Beta plan\n\nReply two."]
    out = C.parse_gmail_thread(msgs)
    assert len(out) == 2
    assert out[0]["from_email"] == "vivek_k@redwoodinference.com"
    assert out[0]["subject"] == "Beta plan"
    assert "Body one." in out[0]["body"]


def test_to_epoch_parses_bench_date_formats():
    # RFC 2822 email Date header (the bench's gmail format) — the big one: previously unparsed,
    # which left ~96% of gmail with NULL created_ts and a synthesized (fake) served date.
    assert C.to_epoch("Mon, 18 May 2026 09:02:00 -0700") == 1779120120   # 16:02Z
    assert C.to_epoch("Mon, 18 May 2026 10:17:00 -07:00") == 1779124620  # malformed colon offset
    # ISO 8601 with a numeric offset and with a trailing Z
    assert C.to_epoch("2026-05-18T09:02:00-07:00") == 1779120120
    assert C.to_epoch("2028-05-23T09:12:00Z") == 1842685920
    # timezone-ABBREVIATION formats (no numeric offset) — the bench's third gmail date shape
    assert C.to_epoch("2026-08-30 09:12 PDT") == 1788106320   # 16:12Z (PDT = -0700)
    assert C.to_epoch("2026-10-04 09:12 UTC") == 1791105120   # 09:12Z
    assert C.to_epoch("Wed, May 14, 2025 at 9:12 AM PT") == 1747242720  # 17:12Z (PT = -0800)
    # date-only, epoch string, and unparseable
    assert C.to_epoch("2025-11-05") == 1762300800
    assert C.to_epoch("1718326400") == 1718326400
    assert C.to_epoch("not a date") is None


def test_parse_jira_comments():
    out = C.parse_jira_comments(["2026-03-14 Jordan Kim: Filing request.",
                                 "2026-03-15 Priya Desai: On it."])
    assert out[0] == {"date": "2026-03-14", "name": "Jordan Kim", "body": "Filing request."}
    assert out[1]["name"] == "Priya Desai"


def test_parse_slack_transcript():
    out = C.parse_slack_transcript("Alex: hi there\ncontinued line\nMaria: yo\ninfra-bot: ping")
    assert out[0] == ("Alex", "hi there\ncontinued line")
    assert out[1] == ("Maria", "yo")
    assert out[2] == ("infra-bot", "ping")


def test_parse_slack_transcript_gates_on_participants():
    # a message-body line "A couple followups: ..." must NOT become a speaker (it's not a
    # participant) — it stays as body of the current turn, so no fake author is minted.
    out = C.parse_slack_transcript(
        "Alex: hey team\nA couple followups: can we warn on whitespace?\nMaria: sure",
        ["Alex", "Maria"])
    assert [s for s, _ in out] == ["Alex", "Maria"]
    assert "A couple followups: can we warn on whitespace?" in out[0][1]  # merged into Alex
    # participant match is tolerant of team labels / formatting, and the speaker is normalized to
    # the participant's canonical name: "Ben Jones" -> "ben.jones" (from "ben.jones (Acme)").
    out2 = C.parse_slack_transcript("Ben Jones: hi\nrandom note: x", ["ben.jones (Acme)"])
    assert [s for s, _ in out2] == ["ben.jones"] and "random note: x" in out2[0][1]
    # transcript variants collapse onto one participant identity (no variant-duplicate authors)
    out3 = C.parse_slack_transcript("Alex: a\nA lex: b\nMaria: c", ["alex", "maria"])
    assert [s for s, _ in out3] == ["alex", "alex", "maria"]


def test_parse_gmail_thread_handles_escaped_newlines():
    # some docs double-escape newlines (literal '\n'); body must still be extracted
    msg = "From: A <a@x.com>\\nTo: B <b@x.com>\\nDate: 2024-01-01\\nSubject: Hi\\n\\nThe body text."
    out = C.parse_gmail_thread([msg])
    assert len(out) == 1
    assert out[0]["from_email"] == "a@x.com" and out[0]["subject"] == "Hi"
    assert "The body text." in out[0]["body"] and out[0]["body"] != ""


def test_parse_slack_transcript_handles_escaped_newlines():
    out = C.parse_slack_transcript("alex: hi there\\nmaria: yo back")
    assert out == [("alex", "hi there"), ("maria", "yo back")]


def test_parse_slack_transcript_speaker_with_parenthetical_team():
    # Some bench docs label speakers "Name (Team):" — each turn must still split per speaker,
    # the parenthetical dropped so the name resolves against the directory.
    out = C.parse_slack_transcript(
        "Elena (CFO): Following up.\nDiego (Eng): thanks\nAsha (FinanceOps): filed it")
    assert out == [("Elena", "Following up."), ("Diego", "thanks"),
                   ("Asha", "filed it")]


# ---------------------------------------------------------------------------
# from test_acl_faithful.py
# ---------------------------------------------------------------------------

def test_drive_grants_owner_collaborators_and_group():
    g = grants_for("google_drive", {"owner": "a@x.com", "people": ["b@x.com"],
                                    "group": "finance", "confidentiality": None, "org": "redwood"})
    assert ("user", "a@x.com") in g and ("user", "b@x.com") in g
    assert ("group", "finance") in g


def test_gmail_is_private_no_org_or_group():
    g = grants_for("gmail", {"owner": "a@x.com", "people": ["b@x.com", "ext@external.example"],
                             "group": "sales", "confidentiality": None, "org": "redwood"})
    assert ("user", "a@x.com") in g
    assert not any(t == "org" or t == "group" for t, _ in g)


def test_confluence_confidentiality_scope():
    pub = grants_for("confluence", {"owner": "a@x.com", "people": [], "group": "eng",
                                    "confidentiality": "public", "org": "redwood"})
    assert ("org", "redwood") in pub
    restr = grants_for("confluence", {"owner": "a@x.com", "people": [], "group": "eng",
                                      "confidentiality": "restricted", "org": "redwood"})
    assert ("group", "eng") in restr and ("org", "redwood") not in restr


# ---------------------------------------------------------------------------
# from test_erb_load.py
# ---------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    return c


def test_to_epoch_formats():
    assert erb.to_epoch("2025-09-18") is not None
    assert erb.to_epoch("Wed, May 14, 2025 at 9:12 AM PT") is not None
    assert erb.to_epoch(1710501234) == 1710501234
    assert erb.to_epoch("garbage") is None


def test_drive_owner_is_faithful():
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "title", "content_field_names": ["body"], "title": "Model",
           "body": "x", "owner": "Maya Chen", "collaborators": ["Ethan Park"],
           "team": "research-applied-ml", "created_at": "2025-09-18", "doc_type": "doc"}
    erb.load_drive(conn, "dsid_1", raw, P)
    row = conn.execute("SELECT author_email, owner_display, created_ts FROM gdrive_files WHERE doc_id='dsid_1'").fetchone()
    assert row["author_email"] == "maya.chen@redwoodinference.com"
    assert row["owner_display"] == "Maya Chen"
    assert row["created_ts"] is not None


def test_jira_assignee_reporter_and_duedate():
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "summary", "content_field_names": ["description"],
           "summary": "S", "description": "d", "reporter": "Jordan Kim", "assignee": "Priya Desai",
           "project": "INT", "status": "In Progress", "created_at": "2025-11-01"}
    erb.load_jira(conn, "dsid_2", raw, P)
    row = conn.execute("SELECT reporter_email, assignee_email, status FROM jira_issues WHERE doc_id='dsid_2'").fetchone()
    assert row["reporter_email"] == "jordan.kim@redwoodinference.com"
    assert row["assignee_email"] == "priya.desai@redwoodinference.com"
    assert row["status"] == "In Progress"


def test_confluence_restricted_grants_reconciled_directory_group():
    """A doc's team label ("security") must reconcile to the directory's actual dept_slug group
    ("security-compliance") for the ACL grant — not become its own empty group."""
    conn = _conn()
    employees = [
        {"name": "Priya Desai", "email": "priya.desai@redwoodinference.com",
         "dept_slug": "security-compliance"},
    ]
    P = Principals(employees, "redwoodinference.com")
    raw = {"title_field_name": "title", "content_field_names": ["body"], "title": "Sec Policy",
           "body": "x", "author": "Priya Desai", "owner_team": "security",
           "confidentiality": "restricted", "space": "SEC", "created_at": "2025-09-18"}
    bundle = erb.load_confluence(conn, "dsid_3", raw, P)
    assert bundle["group"] == "security-compliance"
    grants = grants_for("confluence", {**bundle, "org": "redwood"})
    assert ("group", "security-compliance") in grants
    assert ("group", "security") not in grants


def test_slack_text_variant_not_empty():
    # slack docs whose transcript is in 'text' (title_field_name 'file_name') must still parse
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "file_name", "content_field_names": ["text"],
           "file_name": "1711-foo.json", "channel": "partnerships",
           "text": "andrea_p: Heads up on EU regions.\nmike_partner: On it, ETA next week.",
           "participants": ["andrea_p", "mike_partner"]}
    erb.load_slack(conn, "dsid_s1", raw, P)
    rows = conn.execute("SELECT title, content, thread_seq FROM slack_messages WHERE thread_id='dsid_s1' ORDER BY thread_seq").fetchall()
    assert len(rows) == 2
    assert rows[0]["title"] == "" and "Heads up" in rows[0]["content"]  # not '*file_name*'
    assert "On it" in rows[1]["content"]


def test_gmail_body_variant_not_empty():
    # gmail docs carrying a single email in 'body' (no 'messages' list) must still get content
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "subject", "content_field_names": ["body"],
           "subject": "Q2 plan", "mailbox_owner": "Ceo Person",
           "body": "Here is the Q2 plan draft, please review."}
    erb.load_gmail(conn, "dsid_g1", raw, P)
    r = conn.execute("SELECT title, content FROM gmail_messages WHERE doc_id='dsid_g1'").fetchone()
    assert r["title"] == "Q2 plan"
    assert "Q2 plan draft" in r["content"]


def test_gmail_thread_attachments_ingested():
    # the bench's thread-level `attachments` (filename strings) must land on the root message
    # so the Gmail API can render them as parts (this is qst_0012's missing data).
    import json as _json
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "subject", "content_field_names": ["messages"],
           "subject": "Epoch procurement", "mailbox_owner": "Irene Choi",
           "attachments": ["Epoch_MSAAttachment_v3.pdf", "redlines_epoch_orderform_20290715.docx"],
           "messages": ["From: A <a@x.com>\nTo: B <b@y.com>\nDate: 2029-07-15\nSubject: Epoch procurement\n\nbody"]}
    erb.load_gmail(conn, "dsid_att", raw, P)
    r = conn.execute("SELECT attachments FROM gmail_messages WHERE doc_id='dsid_att'").fetchone()
    atts = _json.loads(r["attachments"])
    assert [a["filename"] for a in atts] == ["Epoch_MSAAttachment_v3.pdf",
                                             "redlines_epoch_orderform_20290715.docx"]
    assert atts[0]["mime"] == "application/pdf"
    assert atts[1]["mime"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    # a doc with no attachments leaves the column NULL (not "[]")
    erb.load_gmail(conn, "dsid_noatt", {"content_field_names": ["body"], "body": "x"}, P)
    assert conn.execute("SELECT attachments FROM gmail_messages WHERE doc_id='dsid_noatt'").fetchone()[0] is None


def test_gmail_thread_title_is_doc_level_subject():
    # the doc-level `subject` (the bench's canonical thread subject) must win over the first
    # message's RFC822 "Re: ..." Subject header (qst_0026's dropped-subject bug).
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "subject", "content_field_names": ["messages"],
           "subject": "[P0] Acme Health — retry storm", "mailbox_owner": "Sean Gallagher",
           "messages": ["From: a@x.com\nSubject: Re: urgent — spikes in 5xx\n\nbody one",
                        "From: b@y.com\nSubject: Re: urgent — spikes in 5xx\n\nbody two"]}
    erb.load_gmail(conn, "dsid_subj", raw, P)
    title = conn.execute("SELECT title FROM gmail_messages WHERE doc_id='dsid_subj'").fetchone()[0]
    assert title == "[P0] Acme Health — retry storm"
    # fallback: no doc-level subject -> the message Subject header is used
    raw2 = {"title_field_name": "subject", "content_field_names": ["messages"], "subject": "",
            "mailbox_owner": "X", "messages": ["From: a@x.com\nSubject: Real subject\n\nbody"]}
    erb.load_gmail(conn, "dsid_subj2", raw2, P)
    assert conn.execute("SELECT title FROM gmail_messages WHERE doc_id='dsid_subj2'").fetchone()[0] == "Real subject"


# ---------------------------------------------------------------------------
# from test_erb_orchestration.py
# ---------------------------------------------------------------------------

def test_acl_bundle_to_grants_drive():
    # a private-ish drive doc: owner + collaborator become user grants + team group
    bundle = {"_source": "google_drive", "owner": "maya.chen@redwoodinference.com",
              "people": ["ethan.park@redwoodinference.com"], "group": "research-applied-ml",
              "confidentiality": None}
    g = grants_for(bundle["_source"], {**bundle, "org": "redwood"})
    assert ("user", "maya.chen@redwoodinference.com") in g
    assert ("group", "research-applied-ml") in g


def test_flat_path_removed():
    # the untrusted flat importer symbols must be gone
    for gone in ("_parse_txt", "_ENTRY_RE", "fetch_slices", "generate_acl", "augment"):
        assert not hasattr(erb, gone), f"{gone} should be removed"


def test_synthesized_users_installed_after_load(tmp_path, monkeypatch):
    """Regression: users synthesized DURING load (owner/collaborator not in the directory) must
    land in principals AND their team group_members — i.e. P.install() runs after load_structured,
    not before (else they'd get tokens but no principal/group, breaking group-scoped ACL)."""
    data = tmp_path / "data"; data.mkdir()
    gen = tmp_path / "gen"; (gen / "sources" / "google_drive").mkdir(parents=True)
    (gen / "employee_directory.yaml").write_text(yaml.safe_dump({"departments": {"Engineering": [
        {"name": "Real Dev", "email": "real.dev@redwoodinference.com", "title": "Eng"}]}}))
    (gen / "sources" / "google_drive" / "d.json").write_text(json.dumps({
        "title_field_name": "title", "content_field_names": ["body"],
        "dataset_doc_uuid": "dsid_test1", "title": "Doc", "body": "x",
        "owner": "Zoe Newperson", "collaborators": ["Ravi Other"], "team": "engineering",
        "created_at": "2025-01-01", "confidentiality": "restricted"}))
    monkeypatch.setenv("MOCK_DATA_DIR", str(data))
    get_settings.cache_clear()
    settings = get_settings()
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    erb.import_structured(settings, gen)

    c = sqlite3.connect(settings.db_path)
    zoe = "zoe.newperson@redwoodinference.com"
    assert c.execute("SELECT 1 FROM principals WHERE email=?", (zoe,)).fetchone(), \
        "synthesized owner missing from principals"
    assert c.execute("SELECT 1 FROM group_members WHERE group_id='engineering' AND user_id=?",
                     (zoe,)).fetchone(), "synthesized owner missing from its team group_members"
    c.close()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# from test_faithful_e2e.py
# ---------------------------------------------------------------------------

def _extra_questions(tmp):
    p = Path(tmp) / "extra_questions.jsonl"
    urllib.request.urlretrieve(
        "https://raw.githubusercontent.com/onyx-dot-app/EnterpriseRAG-Bench/main/extra_questions.jsonl", p)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@pytest.mark.skipif(os.environ.get("ERB_E2E") != "1",
                    reason="set ERB_E2E=1 to run the network-backed faithful-import e2e")
def test_qst_0001_owner_is_maya_chen(tmp_path):
    data_dir = tmp_path / "data"
    qfile = Path(tmp_path) / "extra_questions.jsonl"
    _extra_questions(tmp_path)
    env = {**os.environ, "MOCK_DATA_DIR": str(data_dir)}
    subprocess.run([sys.executable, "-m", "app.importer.erb", "--slice-questions", str(qfile)],
                   check=True, env=env)
    # dsid_fc36... is qst_0001's expected doc; owner must now be Maya Chen, not a hash pick
    from starlette.testclient import TestClient
    os.environ["MOCK_DATA_DIR"] = str(data_dir)
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/drive/v3/files/dsid_fc36d1d60e7e4b4abc7db84629563b7a",
                  params={"fields": "owners(displayName)"},
                  headers={"Authorization": "Bearer admin-service-token"}).json()
        assert r["owners"][0]["displayName"] == "Maya Chen"
