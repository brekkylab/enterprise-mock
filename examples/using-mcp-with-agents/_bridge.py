#!/usr/bin/env python3
"""Generic OpenAPI→MCP bridge — run as a stdio subprocess by the per-source launchers.

Fetches the mock's own ``/openapi.json``, slices it to one source's paths, and serves those
operations as MCP tools via ``FastMCP.from_openapi()`` over an ``httpx.AsyncClient`` whose base
URL is the mock and whose ``Authorization`` header is the caller's credential — so the mock's
per-token ACL is enforced on every tool call.

stdio only: FastMCP's streamable-HTTP mode has a known Authorization-forwarding bug.

    python _bridge.py --source github --base-url http://127.0.0.1:8000 --token <mock-token>
    python _bridge.py --source atlassian --base-url https://host --token <t> --username svc@example.com
"""
from __future__ import annotations

import argparse
import base64
import json
import urllib.request

# source -> path prefixes to expose + how its token authenticates.
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
    """Copy ``spec`` keeping only paths under one of ``prefixes``."""
    paths = {p: item for p, item in spec.get("paths", {}).items()
             if any(p == pre or p.startswith(pre + "/") for pre in prefixes)}
    if not paths:
        raise ValueError(f"no paths matched {prefixes} — is the router enriched/mounted?")
    return {**spec, "paths": paths}


def dedupe_operations(spec: dict) -> dict:
    """Keep one operation per operationId (the mock aliases the same op for fidelity).

    Preference: GET before POST/…, then fewest path params, then lexicographically greatest
    path (so /rest/api/3 beats /rest/api/2; /batch beats /batch/{api}/{version})."""
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
    """Raise if any operationId repeats — FastMCP keys tools by it, so dupes collide."""
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
    p = argparse.ArgumentParser(description="Generic OpenAPI→MCP bridge (stdio).")
    p.add_argument("--source", required=True, choices=sorted(SOURCES))
    p.add_argument("--base-url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--username", default=None, help="basic-auth username (atlassian)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = SOURCES[args.source]
    spec = dedupe_operations(slice_spec(_fetch_spec(args.base_url), cfg["prefixes"]))
    assert_unique_operation_ids(spec)  # safety net — should always hold post-dedupe

    import httpx
    from fastmcp import FastMCP

    client = httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=build_auth_headers(cfg["auth"], args.token, args.username),
        timeout=30,
    )
    # validate_output=False: the mock's responses are the source of truth; a passthrough
    # bridge must never reject a real mock response for not matching a loose schema.
    mcp = FastMCP.from_openapi(
        openapi_spec=spec, client=client, name=f"{args.source}-bridge", validate_output=False)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
