"""Read-only coverage: drive each official LlamaIndex reader against the mock.

Uses the `live_server` fixture (a real uvicorn on the conftest SAMPLE corpus) — readers make real
HTTP calls, so they need a listening port. One test per source; each self-skips if its reader
package is absent (installed via the `[llamaindex]` extra). Does not import from `examples/`
(repo rule) — the small point-at-the-mock setup is duplicated here.
"""
from __future__ import annotations

import pytest


def _base_token(live_server):
    base, settings = live_server
    return base, settings.admin_token
