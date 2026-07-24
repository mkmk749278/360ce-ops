"""Test-wide environment setup.

pytest imports conftest.py *before* collecting/importing any test module, so
this runs before ``app.main`` (and its module-level ``load_settings()``) is
first imported. That guarantees the file-backed stores (app-tokens, device
registry, audit log) point at a writable temp dir instead of the production
``/data`` volume — which isn't writable on CI runners. Without this, the store
paths are fixed at import time to ``/data`` and writes silently no-op.
"""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="ops-test-")

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")
os.environ.setdefault("OPS_APP_TOKENS_PATH", os.path.join(_tmp, "app_tokens.json"))
os.environ.setdefault("OPS_DEVICE_TOKENS_PATH", os.path.join(_tmp, "devices.json"))
os.environ.setdefault("OPS_AUDIT_LOG", os.path.join(_tmp, "audit.jsonl"))
os.environ.setdefault("EXIT_BACKTEST_STATE_DIR", os.path.join(_tmp, "exit_backtest"))
