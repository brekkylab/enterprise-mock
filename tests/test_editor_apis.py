"""New read surfaces added for filesystem-style clients (e.g. mirage):

- Drive root is navigable — ``'root' in parents`` returns folder objects whose ids match what
  files in them report as their parent, and shared-drives enumeration doesn't 404.
- The Workspace editor read APIs (Docs / Sheets / Slides) serve a native doc's content in each
  API's response shape, keyed on the Drive file id and ACL-enforced.
- A Slack channel's ``created`` never postdates its messages.

Driven over the shared SAMPLE corpus via the ``live_server`` subprocess.
"""
from __future__ import annotations

import httpx
import pytest


@pytest.fixture(scope="module")
def base(live_server):
    return live_server[0]


@pytest.fixture(scope="module")
def admin_h(live_server):
    return {"Authorization": f"Bearer {live_server[1].admin_token}"}


def _drive_by_mime(base, admin_h, mime):
    """A visible Drive file id + name for the given native mimeType."""
    r = httpx.get(f"{base}/drive/v3/files", headers=admin_h,
                  params={"q": "trashed=false", "pageSize": 1000}).json()
    for f in r["files"]:
        if f["mimeType"] == mime:
            return f["id"], f["name"]
    raise AssertionError(f"no {mime} in corpus")


# --- Drive navigability ---------------------------------------------------------

def test_shared_drives_empty(base, admin_h):
    r = httpx.get(f"{base}/drive/v3/drives", headers=admin_h, params={"fields": "drives(id,name)"})
    assert r.status_code == 200
    assert r.json()["drives"] == []


def test_root_lists_folders_with_matching_ids(base, admin_h):
    r = httpx.get(f"{base}/drive/v3/files", headers=admin_h,
                  params={"q": "'root' in parents and trashed=false", "pageSize": 1000}).json()
    folders = r["files"]
    assert folders, "root should expose folder objects"
    assert all(f["mimeType"] == "application/vnd.google-apps.folder" for f in folders)
    names = {f["name"] for f in folders}
    assert {"marketing", "finance"} <= names

    # a folder's id must equal what its children report as their parent, so a client can descend
    finance = next(f for f in folders if f["name"] == "finance")
    kids = httpx.get(f"{base}/drive/v3/files", headers=admin_h,
                     params={"q": f"'{finance['id']}' in parents and trashed=false"}).json()["files"]
    assert kids and all(finance["id"] in k["parents"] for k in kids)
    # and GET on the folder id resolves to the folder object
    got = httpx.get(f"{base}/drive/v3/files/{finance['id']}", headers=admin_h).json()
    assert got["mimeType"] == "application/vnd.google-apps.folder" and got["name"] == "finance"


# --- Workspace editor read APIs -------------------------------------------------

def test_docs_get_returns_paragraph_text(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    doc = httpx.get(f"{base}/docs/v1/documents/{fid}", headers=admin_h).json()
    assert doc["documentId"] == fid
    text = "".join(e["textRun"]["content"]
                   for el in doc["body"]["content"] if "paragraph" in el
                   for e in el["paragraph"]["elements"])
    assert "Logo usage" in text  # SAMPLE "Brand guidelines v3"


def test_sheets_get_returns_grid(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    sh = httpx.get(f"{base}/sheets/v4/spreadsheets/{fid}", headers=admin_h).json()
    assert sh["spreadsheetId"] == fid
    rows = sh["sheets"][0]["data"][0]["rowData"]
    cells = [[c.get("formattedValue") for c in row["values"]] for row in rows]
    assert ["month", "revenue"] in cells and ["Jan", "120000"] in cells


def test_slides_get_returns_slides(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.presentation")
    pr = httpx.get(f"{base}/slides/v1/presentations/{fid}", headers=admin_h).json()
    assert pr["presentationId"] == fid and len(pr["slides"]) >= 1
    text = "".join(t["textRun"]["content"]
                   for s in pr["slides"] for pe in s["pageElements"]
                   for t in pe["shape"]["text"]["textElements"])
    assert "Slide 1" in text


def test_editor_apis_enforce_acl(base, live_server):
    """The finance spreadsheet is group-restricted; a non-member gets 404, not the content."""
    import yaml
    tokens = {u["email"]: u["token"]
              for u in yaml.safe_load(live_server[1].tokens_path.read_text())["users"]}
    admin_h = {"Authorization": f"Bearer {live_server[1].admin_token}"}
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}  # marketing, not finance
    assert httpx.get(f"{base}/sheets/v4/spreadsheets/{fid}", headers=outsider).status_code == 404


# --- Slack timestamp consistency ------------------------------------------------

def test_channel_created_not_after_messages(base, admin_h):
    channels = httpx.get(f"{base}/slack/api/conversations.list", headers=admin_h).json()["channels"]
    assert channels
    for ch in channels:
        hist = httpx.get(f"{base}/slack/api/conversations.history", headers=admin_h,
                         params={"channel": ch["id"], "limit": 1}).json()
        msgs = hist.get("messages", [])
        if msgs:
            assert ch["created"] <= float(msgs[0]["ts"]), f"#{ch['name']} created after its message"


def test_history_honors_oldest_latest(base, admin_h):
    """A time-bounded fetch (as a filesystem client makes per day) is filtered by ts — a tight
    window keeps the message, a window entirely after it drops the message."""
    cid = httpx.get(f"{base}/slack/api/conversations.list", headers=admin_h).json()["channels"][0]["id"]
    ts = float(httpx.get(f"{base}/slack/api/conversations.history", headers=admin_h,
                         params={"channel": cid, "limit": 1}).json()["messages"][0]["ts"])

    tight = httpx.get(f"{base}/slack/api/conversations.history", headers=admin_h,
                      params={"channel": cid, "oldest": ts - 5, "latest": ts + 5,
                              "inclusive": "true", "limit": 1000}).json()["messages"]
    assert any(abs(float(m["ts"]) - ts) < 1e-6 for m in tight)

    after = httpx.get(f"{base}/slack/api/conversations.history", headers=admin_h,
                      params={"channel": cid, "oldest": ts + 1, "latest": ts + 100,
                              "limit": 1000}).json()["messages"]
    assert all(float(m["ts"]) > ts for m in after)  # the sampled message is excluded
