"""ACL resolution + visibility, asserted against the SAMPLE corpus's generated ACL."""
from app import store


def _visible(db, acl, token, source):
    ids = acl.visible_ids(db, acl.resolve(token))
    return {r["doc_id"] for r in store.list_documents(db, source, visible_ids=ids, limit=100)}


def test_admin_sees_all_confluence(db, acl):
    assert acl.resolve("admin-service-token").is_admin
    assert _visible(db, acl, "admin-service-token", "confluence") == {"cf-handbook", "cf-oncall", "cf-comp"}


def test_public_visible_to_everyone(db, acl, tokens):
    # a public page is visible to any user, regardless of group
    assert "cf-handbook" in _visible(db, acl, tokens["ava@acme.com"], "confluence")
    assert "cf-handbook" in _visible(db, acl, tokens["mia@acme.com"], "confluence")


def test_group_restricted_hidden_from_nonmember(db, acl, tokens):
    # ava is in engineering, not 'people' -> cannot see the people-only comp page
    assert _visible(db, acl, tokens["ava@acme.com"], "confluence") == {"cf-handbook", "cf-oncall"}


def test_group_restricted_visible_to_member(db, acl, tokens):
    # hana is in 'people' -> sees the comp page
    assert "cf-comp" in _visible(db, acl, tokens["hana@acme.com"], "confluence")


def test_private_doc_only_its_author(db, acl, tokens):
    assert "jira-private" in _visible(db, acl, tokens["bob@acme.com"], "jira")
    assert "jira-private" not in _visible(db, acl, tokens["ava@acme.com"], "jira")


def test_unknown_token_resolves_to_none(acl):
    assert acl.resolve("nope") is None
    assert acl.resolve(None) is None


def test_forbidden_direct_fetch_is_hidden(db, acl, tokens):
    ids = acl.visible_ids(db, acl.resolve(tokens["ava@acme.com"]))
    assert store.get_document(db, "jira", "jira-private", visible_ids=ids) is None      # hidden
    assert store.get_document(db, "confluence", "cf-handbook", visible_ids=ids) is not None  # public
    assert store.get_document(db, "jira", "jira-private", visible_ids=None) is not None  # admin bypass


def test_admin_visible_ids_is_none(db, acl):
    assert acl.visible_ids(db, acl.resolve("admin-service-token")) is None
