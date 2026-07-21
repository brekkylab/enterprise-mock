#!/usr/bin/env python3
"""Self-contained OpenAPI→MCP bridge used ONLY by tests/test_mcp.py.

This is a deliberate DUPLICATE of examples/using-mcp-with-agents/_openapi_bridge.py. Tests must
never reach into examples/ (not even to launch a file there by path) — that would couple the test
suite to showcase code. So the small bridge is embedded here and run as its own stdio subprocess,
exactly the way the vendor MCP servers (docker / npx / uvx) are launched in test_mcp.py. If the
shipped bridge changes, keep this copy in sync; the end-to-end ACL tests exercise this copy.

Not a test module (no test_ prefix), so pytest does not collect it.
"""
from __future__ import annotations

import argparse
import base64
import json
import urllib.request

SOURCES: dict[str, dict] = {
    "github":    {"prefixes": ["/github"],             "auth": "bearer"},
    "slack":     {"prefixes": ["/slack/api"],          "auth": "bearer"},
    "gmail":     {"prefixes": ["/gmail"],              "auth": "bearer"},
    "drive":     {"prefixes": ["/drive"],              "auth": "bearer"},
    "notion":    {"prefixes": ["/notion/v1"],          "auth": "bearer"},
    "atlassian": {"prefixes": ["/atlassian", "/wiki"], "auth": "basic"},
}

_METHODS = ("get", "post", "put", "delete", "patch")
_METHOD_RANK = {m: i for i, m in enumerate(_METHODS)}


def slice_spec(spec: dict, prefixes: list[str]) -> dict:
    paths = {p: item for p, item in spec.get("paths", {}).items()
             if any(p == pre or p.startswith(pre + "/") for pre in prefixes)}
    if not paths:
        raise ValueError(f"no paths matched {prefixes} — is the router enriched/mounted?")
    return {**spec, "paths": paths}


def dedupe_operations(spec: dict) -> dict:
    cand: dict[str, list[tuple[str, str]]] = {}
    for path, item in spec.get("paths", {}).items():
        for method, op in item.items():
            if method in _METHODS and isinstance(op, dict) and "operationId" in op:
                cand.setdefault(op["operationId"], []).append((path, method))
    keep: set[tuple[str, str]] = set()
    for entries in cand.values():
        best_rank = min(_METHOD_RANK[m] for _, m in entries)
        finalists = [(p, m) for p, m in entries if _METHOD_RANK[m] == best_rank]
        fewest = min(p.count("{") for p, _ in finalists)
        finalists = [(p, m) for p, m in finalists if p.count("{") == fewest]
        keep.add(max(finalists, key=lambda pm: pm[0]))
    new_paths: dict[str, dict] = {}
    for path, item in spec.get("paths", {}).items():
        kept = {k: v for k, v in item.items() if k not in _METHODS or (path, k) in keep}
        if any(k in _METHODS for k in kept):
            new_paths[path] = kept
    return {**spec, "paths": new_paths}


def assert_unique_operation_ids(spec: dict) -> None:
    seen: dict[str, int] = {}
    for item in spec.get("paths", {}).values():
        for method, op in item.items():
            if method in _METHODS and isinstance(op, dict) and "operationId" in op:
                seen[op["operationId"]] = seen.get(op["operationId"], 0) + 1
    dupes = sorted(k for k, n in seen.items() if n > 1)
    if dupes:
        raise ValueError(f"duplicate operationIds (would collide as MCP tools): {dupes}")


def build_auth_headers(auth: str, token: str, username: str | None) -> dict[str, str]:
    if auth == "bearer":
        return {"Authorization": f"Bearer {token}"}
    if auth == "basic":
        raw = f"{username or 'svc@example.com'}:{token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    raise ValueError(f"unknown auth style {auth!r}")


def _fetch_spec(base_url: str) -> dict:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/openapi.json", timeout=10) as r:
        return json.load(r)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OpenAPI→MCP bridge probe (stdio).")
    p.add_argument("--source", required=True, choices=sorted(SOURCES))
    p.add_argument("--base-url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--username", default=None)
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = SOURCES[args.source]
    spec = dedupe_operations(slice_spec(_fetch_spec(args.base_url), cfg["prefixes"]))
    assert_unique_operation_ids(spec)

    import httpx
    from fastmcp import FastMCP

    client = httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=build_auth_headers(cfg["auth"], args.token, args.username),
        timeout=30,
    )
    mcp = FastMCP.from_openapi(
        openapi_spec=spec, client=client, name=f"{args.source}-bridge", validate_output=False)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
