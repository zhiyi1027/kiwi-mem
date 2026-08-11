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
    require("'知知'" in archive_page and "'我'" in archive_page, "archive UI lost first-person labels")
    require("source_client" not in archive_page, "archive UI anchors identity to a source room")

    require("${KIWI_BIND_IP:-127.0.0.1}" in compose, "Docker no longer binds privately by default")
    require('"chat_archive_enabled"' in config_source and '"false"' in config_source, "verbatim archive is not explicit opt-in")
    require("_archive_gateway_user(" in main_source and "_archive_gateway_assistant(" in main_source,
            "gateway stopped capturing both visible sides")

    print("PASS: private complete-transcript archive contract")


if __name__ == "__main__":
    main()
