#!/usr/bin/env python3
"""Fast guards for digest-only credentials and control-plane route coverage."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from access_control import authenticate_admin_token, is_control_plane_path, sha256_digest


def require(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    admin_secret = "test-admin-secret-that-never-enters-server-config"
    memory_secrets = {
        "codex_vps2": "test-codex-memory-secret",
        "cc_vps1": "test-cc-one-memory-secret",
        "cc_vps2": "test-cc-two-memory-secret",
    }
    os.environ["KIWI_ADMIN_TOKEN_SHA256"] = sha256_digest(admin_secret)
    os.environ["MEMORY_CLIENT_KEY_DIGESTS_JSON"] = json.dumps(
        {client_id: sha256_digest(secret) for client_id, secret in memory_secrets.items()}
    )

    from memory_identity import authenticate_memory_client

    require(authenticate_admin_token(admin_secret), "valid admin secret failed digest authentication")
    require(not authenticate_admin_token("wrong"), "invalid admin secret authenticated")
    for client_id, secret in memory_secrets.items():
        context = authenticate_memory_client(secret)
        require(context is not None and context.client_id == client_id, f"memory door failed: {client_id}")
    require(authenticate_memory_client("wrong") is None, "invalid memory secret authenticated")

    for path in (
        "/admin/config",
        "/debug/memories",
        "/sync/export",
        "/dream/start",
        "/calendar/2026-08-11",
        "/comments",
        "/reminders",
    ):
        require(is_control_plane_path(path), f"sensitive route is public: {path}")
    for path in ("/admin", "/admin/", "/admin/js/app.js", "/memory/v1/recall", "/memory/mcp", "/calendar/mcp"):
        require(not is_control_plane_path(path), f"non-control route was accidentally gated: {path}")

    print("PASS: digest-only credentials and control-plane route guards")


if __name__ == "__main__":
    main()
