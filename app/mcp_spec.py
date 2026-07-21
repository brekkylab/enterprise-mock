"""Derive an MCP-ready OpenAPI spec from the mock's own ``/openapi.json``.

The mock exposes some routes multiple ways for vendor fidelity — Slack methods accept GET **and**
POST on one route, Jira aliases ``/rest/api/2`` and ``/rest/api/3``, Drive's ``batch`` is
double-mounted — and FastAPI's auto operationId generation collapses each such pair to a single id,
so the raw ``/openapi.json`` carries ~14 *duplicate* operationIds. An OpenAPI→MCP bridge keys its
tools by operationId, so a raw spec would collide (one tool silently overwriting another).

This module slices the spec to one source's paths and collapses those fidelity aliases to one
callable operation each (prefer GET, then fewest path params, then the higher-versioned path). The
result is served at ``GET /mcp/openapi/{source}`` so a bridge can consume it directly — no
client-side spec surgery. S3 is intentionally absent: it is SigV4-signed, which a static bridge
auth header can't produce.
"""
from __future__ import annotations

# source -> the path prefix(es) whose operations that source's MCP server should expose.
SOURCE_PREFIXES: dict[str, list[str]] = {
    "github": ["/github"],
    "slack": ["/slack/api"],
    "gmail": ["/gmail"],
    "drive": ["/drive"],
    "notion": ["/notion/v1"],
    "atlassian": ["/atlassian", "/wiki"],
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

    Preference: GET before POST/…, then fewest path params, then lexicographically greatest path
    (so ``/rest/api/3`` beats ``/rest/api/2``; ``/batch`` beats ``/batch/{api}/{version}``)."""
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


def _duplicate_operation_ids(spec: dict) -> list[str]:
    seen: dict[str, int] = {}
    for item in spec.get("paths", {}).values():
        for method, op in item.items():
            if method in _METHODS and isinstance(op, dict) and "operationId" in op:
                seen[op["operationId"]] = seen.get(op["operationId"], 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def build_mcp_spec(full_spec: dict, source: str) -> dict:
    """The MCP-ready spec for ``source``: sliced to its paths, fidelity aliases collapsed.

    Raises ``KeyError`` for an unknown source and ``ValueError`` if any operationId collision
    survives (an invariant — dedupe should always resolve them)."""
    prefixes = SOURCE_PREFIXES[source]
    spec = dedupe_operations(slice_spec(full_spec, prefixes))
    dupes = _duplicate_operation_ids(spec)
    if dupes:
        raise ValueError(f"unresolved duplicate operationIds for {source!r}: {dupes}")
    return spec
