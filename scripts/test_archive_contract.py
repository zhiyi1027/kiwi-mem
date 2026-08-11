#!/usr/bin/env python3
"""Fast static guards for the private complete-transcript archive surface."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    database_source = (ROOT / "database.py").read_text(encoding="utf-8")
    archive_page = (ROOT / "admin-panel/js/pages/archive.js").read_text(encoding="utf-8")
    routes = (ROOT / "admin-panel/js/routes.js").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    config_source = (ROOT / "config.py").read_text(encoding="utf-8")
    access_source = (ROOT / "access_control.py").read_text(encoding="utf-8")
    identity_source = (ROOT / "memory_identity.py").read_text(encoding="utf-8")
    api_source = (ROOT / "admin-panel/js/api.js").read_text(encoding="utf-8")

    for endpoint in (
        "/memory/v1/events/ingest-batch",
        "/memory/v1/archive/conversations",
        "/memory/v1/archive/search",
    ):
        require(endpoint in main_source, f"missing complete archive endpoint: {endpoint}")
    require("async with conn.transaction()" in database_source, "batch replay lost its database transaction")
    require("event_id is already bound" in database_source, "event id collisions no longer fail closed")
    require("STRPOS(LOWER(content), LOWER($2))" in database_source, "literal transcript search became wildcard search")

    require("key: 'archive'" in routes, "admin navigation lost the transcript archive")
    require("sessionStorage" in archive_page, "archive credential is not tab-scoped")
    require("localStorage" not in archive_page, "archive credential persists beyond the browser tab")
    require("'知知/Lyra'" in archive_page and "'凛/Grey'" in archive_page,
            "archive UI lost the shared display names")
    require("source_client" not in archive_page, "archive UI anchors identity to a source room")
    require("_PUBLIC_ARCHIVE_EVENT_FIELDS" in main_source and "_public_archive_record" in main_source,
            "archive API lost its identity-neutral response projection")
    whoami_start = main_source.index('async def memory_whoami')
    whoami_end = main_source.index('@app.post("/memory/v1/events/ingest")', whoami_start)
    require('"client_id"' not in main_source[whoami_start:whoami_end],
            "whoami exposes which room authenticated")
    require('"source_client" in request.query_params' in main_source,
            "archive directory accepted source-room filtering")
    require("_reject_memory_identity_override(payload)" in main_source,
            "archive search accepted server-owned source identity fields")

    require("KIWI_ADMIN_TOKEN_SHA256" in access_source, "admin control plane lost digest authentication")
    require("is_control_plane_path" in main_source, "control-plane middleware is not installed")
    require("sessionStorage" in api_source and "kiwi-admin-session-token" in api_source,
            "admin credential is not tab-scoped")
    require("MEMORY_CLIENT_KEYS_JSON" not in identity_source,
            "server still accepts plaintext memory-client keys")
    require("MEMORY_CLIENT_KEY_DIGESTS_JSON" in identity_source,
            "memory-client digest configuration is missing")

    require("${KIWI_BIND_IP:-127.0.0.1}" in compose, "Docker no longer binds privately by default")
    require('"chat_archive_enabled"' in config_source and '"false"' in config_source, "verbatim archive is not explicit opt-in")
    require("_archive_gateway_user(" in main_source and "_archive_gateway_assistant(" in main_source,
            "gateway stopped capturing both visible sides")

    print("PASS: private complete-transcript archive contract")


if __name__ == "__main__":
    main()
