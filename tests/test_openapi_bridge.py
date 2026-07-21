"""Unit tests for the generic OpenAPI→MCP bridge's pure helpers.

The bridge lives under examples/ (not an importable package, and tests must not depend
on examples being importable) — load it by file path just for these pure functions.
"""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

_BRIDGE = Path(__file__).resolve().parents[1] / "examples" / "using-mcp-with-agents" / "_bridge.py"
_spec = importlib.util.spec_from_file_location("_bridge_under_test", _BRIDGE)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def _spec_doc():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/github/repos/{owner}/{repo}/issues": {"get": {"operationId": "list_issues"}},
            "/github/search/issues": {"get": {"operationId": "search_issues"}},
            "/notion/v1/search": {"post": {"operationId": "notion_search"}},
        },
    }


def test_slice_keeps_only_matching_prefix():
    out = bridge.slice_spec(_spec_doc(), ["/github"])
    assert set(out["paths"]) == {
        "/github/repos/{owner}/{repo}/issues",
        "/github/search/issues",
    }
    assert "/notion/v1/search" in _spec_doc()["paths"]  # original untouched


def test_slice_empty_match_raises():
    with pytest.raises(ValueError, match="no paths matched"):
        bridge.slice_spec(_spec_doc(), ["/slack/api"])


def test_assert_unique_operation_ids_flags_dupes():
    dupe = {"paths": {"/a": {"get": {"operationId": "x"}, "post": {"operationId": "x"}}}}
    with pytest.raises(ValueError, match="x"):
        bridge.assert_unique_operation_ids(dupe)


def test_assert_unique_operation_ids_ok():
    bridge.assert_unique_operation_ids(_spec_doc())  # no raise


def test_dedupe_get_post_same_path_keeps_get():
    spec = {"paths": {
        "/slack/api/conversations.history": {
            "get": {"operationId": "conversations_history"},
            "post": {"operationId": "conversations_history"},
        },
    }}
    out = bridge.dedupe_operations(spec)
    assert set(out["paths"]["/slack/api/conversations.history"]) == {"get"}
    bridge.assert_unique_operation_ids(out)


def test_dedupe_v2_v3_aliases_keeps_v3():
    spec = {"paths": {
        "/rest/api/2/issue/{key}": {"get": {"operationId": "jira_get_issue"}},
        "/rest/api/3/issue/{key}": {"get": {"operationId": "jira_get_issue"}},
    }}
    out = bridge.dedupe_operations(spec)
    assert set(out["paths"]) == {"/rest/api/3/issue/{key}"}


def test_dedupe_prefers_fewer_path_params():
    spec = {"paths": {
        "/batch": {"post": {"operationId": "batch"}},
        "/batch/{api}/{version}": {"post": {"operationId": "batch"}},
    }}
    out = bridge.dedupe_operations(spec)
    assert set(out["paths"]) == {"/batch"}


def test_dedupe_keeps_distinct_ops():
    out = bridge.dedupe_operations(_spec_doc())
    assert len(out["paths"]) == 3  # nothing collapsed


def test_build_auth_headers_bearer():
    assert bridge.build_auth_headers("bearer", "tok", None) == {"Authorization": "Bearer tok"}


def test_build_auth_headers_basic():
    h = bridge.build_auth_headers("basic", "tok", "svc@example.com")
    assert h["Authorization"] == "Basic " + base64.b64encode(b"svc@example.com:tok").decode()
