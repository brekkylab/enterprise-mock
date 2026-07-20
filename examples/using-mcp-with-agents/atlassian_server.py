"""Build the `docker run` args that point the community **official** Atlassian MCP
server (`ghcr.io/sooperset/mcp-atlassian`) at this project's mock.

Why the tricks:
- `mcp-atlassian` classifies a host as Jira/Confluence **Cloud** (the v3 + `/wiki`
  API shape our mock speaks) only when the hostname ends in `.atlassian.net`. So we
  always use a fake host `mock.atlassian.net`, mapped with Docker's `--add-host` — to
  the host machine (`host-gateway`) for a local mock, or to the deployment's resolved
  IP for a remote one.
- Its SSRF guard blocks literal `localhost` / private IPs but allows a domain listed
  in `MCP_ALLOWED_URL_DOMAINS` — hence `atlassian.net`.
- Auth is HTTP Basic `username:api_token`; our mock resolves the **api_token** to a
  user and enforces that user's ACL. Set `MOCK_MCP_TOKEN` to a per-user token from
  `data/tokens.yaml` to scope retrieval to that user (default: the admin token = sees all).
  The **username** is required by mcp-atlassian (with none it exposes ZERO tools) but the
  mock ignores it once the token resolves — so it's a required-but-throwaway placeholder.

Normally you don't call `configure()` yourself — the agent scripts call it with the mock's
base URL. `configure()` sets the MOCK_MCP_* env below from that URL (local vs remote).

Env: MOCK_MCP_HOST (default mock.atlassian.net), MOCK_MCP_PORT, MOCK_MCP_SCHEME (http/https),
MOCK_MCP_ADDHOST (host-gateway or an IP), MOCK_MCP_TOKEN, MOCK_MCP_USERNAME, MOCK_MCP_SSL_VERIFY.
"""
from __future__ import annotations

import os
import socket
import sys
from urllib.parse import urlparse


def _arg(name: str, argv: list[str] | None = None) -> str | None:
    argv = sys.argv[1:] if argv is None else argv
    flag = f"--{name}"
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def configure(base_url: str, token: str) -> None:
    """Set the MOCK_MCP_* env so :func:`docker_args` points mcp-atlassian at ``base_url``.

    - **Local** loopback mock → reach it via ``--add-host=mock.atlassian.net:host-gateway`` (HTTP),
      using the local mock's known admin token (override with ``--token`` / ``MOCK_MCP_TOKEN``).
    - **Remote** deployment → mcp-atlassian needs a ``*.atlassian.net`` hostname for Cloud
      detection, but the deployment's TLS cert won't match that name, so we alias
      ``mock.atlassian.net`` to the deployment's resolved IP and skip cert verification. For a
      remote target both ``--token`` and ``--username`` are **required**: the token authenticates
      and scopes ACL (don't silently reuse the built-in admin token against someone else's
      server), and mcp-atlassian additionally needs a Basic-auth username."""
    u = urlparse(base_url)
    host = u.hostname or "127.0.0.1"
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):  # local mock on the Docker host
        os.environ["MOCK_MCP_TOKEN"] = _arg("token") or os.environ.get("MOCK_MCP_TOKEN") or token
        os.environ["MOCK_MCP_SCHEME"] = "http"
        os.environ["MOCK_MCP_PORT"] = str(u.port or 80)
        os.environ["MOCK_MCP_ADDHOST"] = "host-gateway"
        return
    # remote deployment — require explicit credentials rather than defaulting to the admin token
    user = _arg("username") or os.environ.get("MOCK_MCP_USERNAME")
    tok = _arg("token") or os.environ.get("MOCK_MCP_TOKEN")
    missing = [n for n, v in (("--token", tok), ("--username", user)) if not v]
    if missing:
        sys.exit(f"--url points at a remote deployment ({host}); also pass {' and '.join(missing)} "
                 "— the token authenticates and scopes ACL (get one from GET /_mock/users), and "
                 "mcp-atlassian requires a Basic-auth username for Cloud API detection.")
    os.environ["MOCK_MCP_TOKEN"] = tok
    os.environ["MOCK_MCP_USERNAME"] = user
    os.environ["MOCK_MCP_SCHEME"] = u.scheme
    os.environ["MOCK_MCP_PORT"] = str(u.port or (443 if u.scheme == "https" else 80))
    os.environ["MOCK_MCP_ADDHOST"] = socket.gethostbyname(host)  # alias mock.atlassian.net -> here
    os.environ["MOCK_MCP_SSL_VERIFY"] = "false"  # cert is for the real host, not mock.atlassian.net


def docker_args() -> list[str]:
    # Read env at call time so a caller (configure(), or the shell) can set these per invocation.
    host = os.environ.get("MOCK_MCP_HOST", "mock.atlassian.net")  # must end in .atlassian.net
    scheme = os.environ.get("MOCK_MCP_SCHEME", "http")
    port = os.environ.get("MOCK_MCP_PORT", "8000")
    addhost = os.environ.get("MOCK_MCP_ADDHOST", "host-gateway")  # host-gateway (local) or an IP
    token = os.environ.get("MOCK_MCP_TOKEN", "admin-service-token")
    user = os.environ.get("MOCK_MCP_USERNAME", "svc@example.com")
    image = os.environ.get("MOCK_MCP_IMAGE", "ghcr.io/sooperset/mcp-atlassian:latest")
    default_port = (scheme == "https" and port == "443") or (scheme == "http" and port == "80")
    netloc = host if default_port else f"{host}:{port}"
    base = f"{scheme}://{netloc}"
    args = [
        "run", "-i", "--rm",
        f"--add-host={host}:{addhost}",
        "-e", f"JIRA_URL={base}/atlassian",
        "-e", f"JIRA_USERNAME={user}",
        "-e", f"JIRA_API_TOKEN={token}",
        "-e", f"CONFLUENCE_URL={base}/atlassian/wiki",
        "-e", f"CONFLUENCE_USERNAME={user}",
        "-e", f"CONFLUENCE_API_TOKEN={token}",
        "-e", "MCP_ALLOWED_URL_DOMAINS=atlassian.net",
        "-e", "READ_ONLY_MODE=true",
    ]
    if os.environ.get("MOCK_MCP_SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        args += ["-e", "JIRA_SSL_VERIFY=false", "-e", "CONFLUENCE_SSL_VERIFY=false"]
    args += [image, "--transport", "stdio"]
    return args
