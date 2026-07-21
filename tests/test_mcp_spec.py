"""Unit tests for app.mcp_spec — the MCP-ready OpenAPI derivation served at /_mock/openapi/{source}.

This is app code (not examples), so it's imported and tested directly: the slice/dedupe logic that
lets an OpenAPI→MCP bridge consume the mock's spec without operationId collisions.
"""
from __future__ import annotations

import warnings

import pytest

from app import mcp_spec


def _doc():
    return {"paths": {
        "/github/repos/{owner}/{repo}/issues": {"get": {"operationId": "list_issues"}},
        "/github/search/issues": {"get": {"operationId": "search_issues"}},
        "/notion/v1/search": {"post": {"operationId": "notion_search"}},
    }}


def test_slice_keeps_only_prefix():
    out = mcp_spec.slice_spec(_doc(), ["/github"])
    assert set(out["paths"]) == {"/github/repos/{owner}/{repo}/issues", "/github/search/issues"}
    assert "/notion/v1/search" in _doc()["paths"]  # original untouched


def test_slice_empty_raises():
    with pytest.raises(ValueError, match="no paths matched"):
        mcp_spec.slice_spec(_doc(), ["/slack/api"])


def test_dedupe_get_post_same_path_keeps_get():
    spec = {"paths": {"/slack/api/conversations.history": {
        "get": {"operationId": "x"}, "post": {"operationId": "x"}}}}
    out = mcp_spec.dedupe_operations(spec)
    assert set(out["paths"]["/slack/api/conversations.history"]) == {"get"}


def test_dedupe_same_id_across_paths_prefers_greater_path():
    # tie-break when one id spans two paths of equal method/params: keep the greater path
    spec = {"paths": {
        "/rest/api/2/issue/{key}": {"get": {"operationId": "j"}},
        "/rest/api/3/issue/{key}": {"get": {"operationId": "j"}}}}
    assert set(mcp_spec.dedupe_operations(spec)["paths"]) == {"/rest/api/3/issue/{key}"}


def test_dedupe_prefers_fewer_path_params():
    spec = {"paths": {
        "/batch": {"post": {"operationId": "b"}},
        "/batch/{api}/{version}": {"post": {"operationId": "b"}}}}
    assert set(mcp_spec.dedupe_operations(spec)["paths"]) == {"/batch"}


def test_build_mcp_spec_rejects_unknown_source():
    with pytest.raises(KeyError):
        mcp_spec.build_mcp_spec(_doc(), "s3")  # SigV4 — intentionally no bridge


def test_build_mcp_spec_resolves_all_real_collisions():
    """Against the real app spec, every bridged source's built spec has unique operationIds
    (the raw /openapi.json carries ~14 duplicates from GET/POST and v2/v3 fidelity aliases)."""
    warnings.filterwarnings("ignore")
    from app.main import app

    full = app.openapi()
    for source in mcp_spec.SOURCE_PREFIXES:
        spec = mcp_spec.build_mcp_spec(full, source)  # raises ValueError if a collision survives
        assert spec["paths"], f"{source} sliced to empty"
