#!/usr/bin/env python3
"""Permanent S1-S6 safety regression suite for kiwi-mem.

This script intentionally uses a disposable PostgreSQL database.  It refuses
ambient/production-looking database hosts, sets DATABASE_URL before importing
application modules, never starts the FastAPI lifespan, and replaces every
model/embedding/HTTP boundary used by the tests with deterministic fakes.

Run with:

    KIWI_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
      python scripts/test_kiwi_safety_sync.py
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import re
import sys
import uuid
import zipfile
from contextlib import redirect_stdout
from datetime import date, datetime as StdDateTime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALLOWED_TEST_HOSTS = {"127.0.0.1", "localhost", "::1"}
DATABASE_PREFIX = "kiwi_safety_"
SYNC_KEYS = (
    "user_avatar",
    "user_nickname",
    "assistant_avatar",
    "assistant_settings",
    "custom_skills",
    "quick_phrases",
    "mcp_switches",
    "theme_preference",
    "reasoning_effort",
)

database: Any = None
config: Any = None
app_module: Any = None
memory_extractor: Any = None
PASSED: list[str] = []


def require(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def passed(name: str) -> None:
    PASSED.append(name)
    print(f"  PASS {name}")


def _validated_admin_dsn() -> str:
    dsn = os.environ.get("KIWI_TEST_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "KIWI_TEST_DATABASE_URL is required; the safety suite never falls back "
            "to DATABASE_URL or a developer .env file"
        )
    parsed = urlsplit(dsn)
    require(parsed.scheme in {"postgres", "postgresql"}, "test DSN must be PostgreSQL")
    require(parsed.hostname in ALLOWED_TEST_HOSTS, "test PostgreSQL host must be loopback")
    require(bool(parsed.path.strip("/")), "test DSN must name a maintenance database")
    return dsn


def _dsn_for_database(admin_dsn: str, database_name: str) -> str:
    parsed = urlsplit(admin_dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, ""))


async def _create_disposable_database(admin_dsn: str) -> tuple[str, str]:
    database_name = f"{DATABASE_PREFIX}{uuid.uuid4().hex}"
    require(re.fullmatch(r"kiwi_safety_[0-9a-f]{32}", database_name), "unsafe test DB name")
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await conn.close()
    return database_name, _dsn_for_database(admin_dsn, database_name)


async def _drop_disposable_database(admin_dsn: str, database_name: str) -> None:
    require(re.fullmatch(r"kiwi_safety_[0-9a-f]{32}", database_name), "refusing unsafe DROP")
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
    finally:
        await conn.close()


async def _pool_fetchval(sql: str, *args: Any) -> Any:
    pool = await database.get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, *args)


async def _pool_fetchrow(sql: str, *args: Any) -> Any:
    pool = await database.get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


async def _pool_fetch(sql: str, *args: Any) -> Any:
    pool = await database.get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


async def _pool_execute(sql: str, *args: Any) -> str:
    pool = await database.get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(sql, *args)


async def _truncate(*tables: str) -> None:
    allowed = {
        "gateway_config",
        "conversations",
        "memories",
        "chat_messages",
        "chat_conversations",
        "chat_projects",
        "compression_summaries",
        "project_file_chunks",
        "calendar_pages",
        "comments",
        "dream_logs",
        "mem_scenes",
        "memory_events",
    }
    require(bool(tables) and set(tables) <= allowed, "unsafe TRUNCATE table")
    await _pool_execute(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE")


async def _upsert_config(key: str, value: str) -> None:
    await _pool_execute(
        """
        INSERT INTO gateway_config (key, value, label, updated_at)
        VALUES ($1, $2, 'safety-test', NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        key,
        value,
    )


async def _config_row(key: str) -> Any:
    return await _pool_fetchval("SELECT value FROM gateway_config WHERE key = $1", key)


async def _seed_memory(
    content: str,
    *,
    locked: bool = False,
    title: str = "",
    project_id: str | None = None,
    memory_type: str = "fragment",
    source: str = "safety_test",
    created_at: Any = None,
    dream_processed: bool = False,
    valid_until: Any = None,
) -> int:
    created_at = created_at or StdDateTime.now(database.TZ_CST)
    dream_processed_at = created_at if dream_processed else None
    return await _pool_fetchval(
        """
        INSERT INTO memories (
            content, title, importance, is_permanent, source, project_id,
            memory_type, created_at, dream_processed_at, valid_until
        )
        VALUES ($1, $2, 5, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        content,
        title,
        locked,
        source,
        project_id,
        memory_type,
        created_at,
        dream_processed_at,
        valid_until,
    )


async def _seed_dream(
    *,
    status: str = "running",
    deleted: bool = False,
    started_at: Any = None,
    narrative: str = "dream-original",
    processed: int = 7,
) -> int:
    started_at = started_at or StdDateTime.now(database.TZ_CST)
    return await _pool_fetchval(
        """
        INSERT INTO dream_logs (
            started_at, status, trigger_type, model_used, memories_processed,
            dream_narrative, deleted
        ) VALUES ($1, $2, 'manual', 'safety-model', $3, $4, $5)
        RETURNING id
        """,
        started_at,
        status,
        processed,
        narrative,
        deleted,
    )


async def _seed_scene(dream_id: int, *, status: str = "active", title: str = "scene") -> int:
    return await _pool_fetchval(
        """
        INSERT INTO mem_scenes (
            title, narrative, atomic_facts, foresight, related_memory_ids,
            embedding, status, created_by_dream_id
        ) VALUES ($1, 'scene-narrative', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                  '[1.0, 0.0]'::jsonb, $2, $3)
        RETURNING id
        """,
        title,
        status,
        dream_id,
    )


async def test_s1(client: httpx.AsyncClient) -> None:
    print("\nS1 /sync/settings whitelist")
    require(tuple(config.SYNC_SETTING_KEYS) == SYNC_KEYS, "SYNC_SETTING_KEYS/order drifted")
    require(all(key in config.CONFIG_SCHEMA for key in SYNC_KEYS), "sync key outside schema")
    passed("T-S1-1 exact ordered nine-key contract")

    await _truncate("gateway_config", "chat_conversations", "chat_projects", "compression_summaries")
    dangerous_before = {
        "search_api_key": "SAFE-SEARCH-SENTINEL",
        "prompt_title_summary": "SAFE-PROMPT-SENTINEL",
        "memory_enabled": "true",
        "default_chat_model": "SAFE-MODEL-SENTINEL",
    }
    for key, value in dangerous_before.items():
        await _upsert_config(key, value)

    seen_set_config: list[str] = []
    real_set_config = config.set_config

    async def spying_set_config(key: str, value: str) -> bool:
        seen_set_config.append(key)
        return await real_set_config(key, value)

    attack = {
        "user_nickname": "安全昵称",
        "search_api_key": "ATTACK-SEARCH",
        "prompt_title_summary": "ATTACK-PROMPT",
        "memory_enabled": "false",
        "default_chat_model": "ATTACK-MODEL",
    }
    with patch.object(app_module, "set_config", spying_set_config):
        response = await client.put("/sync/settings", json=attack)
    require(response.status_code == 200, f"mixed settings PUT failed: {response.text}")
    body = response.json()
    require(body["updated"] == ["user_nickname"], f"wrong updated set: {body}")
    require(body["rejected"] == list(dangerous_before), f"wrong rejected set: {body}")
    require(seen_set_config == ["user_nickname"], f"dangerous key reached set_config: {seen_set_config}")
    passed("T-S1-2 mixed PUT rejects before set_config")

    require(await _config_row("user_nickname") == "安全昵称", "allowed setting did not persist")
    for key, value in dangerous_before.items():
        require(await _config_row(key) == value, f"dangerous DB value changed: {key}")
    passed("T-S1-3 real PostgreSQL values remain protected")

    response = await client.get("/sync/settings")
    require(response.status_code == 200, response.text)
    require(tuple(response.json().keys()) == SYNC_KEYS, "GET settings keys/order drifted")
    passed("T-S1-4 GET exact key set and order")

    response = await client.get("/sync/export")
    require(response.status_code == 200, response.text)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        settings_json = json.loads(archive.read("settings.json"))
        config_json = json.loads(archive.read("config.json"))
    require(tuple(settings_json.keys()) == SYNC_KEYS, "settings.json contract drifted")
    require(config_json["search_api_key"] == dangerous_before["search_api_key"], "full backup lost config")
    passed("T-S1-5 export keeps nine-key settings and full config backup")

    for key in SYNC_KEYS:
        await _upsert_config(key, f"sync-{key}")
    response = await client.request("DELETE", "/sync/reset", json={"confirm": "RESET_ALL_DATA"})
    require(response.status_code == 200 and response.json()["status"] == "ok", response.text)
    remaining_sync = await _pool_fetchval(
        "SELECT COUNT(*) FROM gateway_config WHERE key = ANY($1::text[])", list(SYNC_KEYS)
    )
    require(remaining_sync == 0, "reset did not remove exactly the sync settings")
    for key, value in dangerous_before.items():
        require(await _config_row(key) == value, f"reset removed management config: {key}")
    passed("T-S1-6 reset removes sync settings only")

    consumers = (
        app_module.api_sync_get_settings,
        app_module.api_sync_put_settings,
        app_module.api_sync_export,
        app_module.api_sync_reset,
    )
    for consumer in consumers:
        source = inspect.getsource(consumer)
        require("SYNC_SETTING_KEYS" in source, f"{consumer.__name__} bypasses shared constant")
        require("sync_keys =" not in source, f"{consumer.__name__} has a local key copy")
    passed("T-S1-7 all four consumers use the single constant")

    readme_zh = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_en = (ROOT / "README_EN.md").read_text(encoding="utf-8")
    for endpoint in ("/admin", "/sync/export", "/sync/import-backup"):
        require(endpoint in readme_zh and endpoint in readme_en, f"README warning misses {endpoint}")
    require("整个" in readme_zh and "entire service" in readme_en, "whole-service warning missing")
    require("没有内建鉴权" in readme_zh, "Chinese README must disclose missing built-in auth")
    require("not authenticated" in readme_en, "README must not claim built-in authentication")
    passed("T-S1-8 bilingual deployment warning")


async def test_s2() -> None:
    print("\nS2 log redaction")
    await _truncate("memories", "gateway_config")
    content_secret = "内容密钥-ASCII-SECRET-9988-🧪-尾巴"
    title_secret = "标题密钥-TITLE-SECRET-7766-🌙"

    async def vector_embedding(_text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    output = io.StringIO()
    with patch.object(database, "get_embedding", vector_embedding), redirect_stdout(output):
        memory_id = await database.save_memory(
            content_secret,
            title=title_secret,
            emotional_weight=4,
            project_id="project-safety",
        )
    log = output.getvalue()
    require(str(memory_id) in log and str(len(content_secret)) in log and "3维" in log, "allowed metadata missing")
    for forbidden in (content_secret, title_secret, content_secret[:10], title_secret[:10]):
        require(forbidden not in log, f"memory log leaked content: {forbidden}")
    passed("T-S2-1 memory title/content sentinels absent")
    passed("T-S2-2 vector metadata remains useful")

    async def no_embedding(_text: str) -> None:
        return None

    output = io.StringIO()
    with patch.object(database, "get_embedding", no_embedding), redirect_stdout(output):
        await database.save_memory("NO-VECTOR-CONTENT-SECRET", title="NO-VECTOR-TITLE-SECRET")
    require("无向量" in output.getvalue(), "no-vector metadata missing")
    require("NO-VECTOR-CONTENT" not in output.getvalue(), "no-vector path leaked content")

    extraction_secret = "EXTRACT-RAW-SECRET-556677-中文🧪"
    extracted = [
        {
            "content": extraction_secret,
            "importance": 7,
            "emotional_weight": 2,
            "category": "",
        }
    ]

    async def fake_resolve(_model: str) -> tuple[str, str, str]:
        return "http://127.0.0.1:9/mock-memory", "mock-key", "openai"

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {"message": {"content": json.dumps(extracted, ensure_ascii=False)}}
                ]
            }

    class FakeAsyncClient:
        calls = 0

        def __init__(self, *args: Any, **kwargs: Any):
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
            type(self).calls += 1
            return FakeResponse()

    output = io.StringIO()
    with (
        patch.object(database, "resolve_model_endpoint", fake_resolve),
        patch.object(memory_extractor.httpx, "AsyncClient", FakeAsyncClient),
        redirect_stdout(output),
    ):
        result = await memory_extractor.extract_memories(
            [{"role": "user", "content": "test input"}], model_override="mock-model"
        )
    require(FakeAsyncClient.calls == 1, "mock HTTP boundary was not exercised exactly once")
    require(result and result[0]["content"] == extraction_secret, "extractor behavior changed")
    require(extraction_secret not in output.getvalue(), "raw extraction response leaked")
    require(str(len(json.dumps(extracted, ensure_ascii=False))) in output.getvalue(), "length metadata missing")
    passed("T-S2-3 extractor HTTP mocked and raw response redacted")

    key_secret = "sk-VERY-LONG-SECRET-1234567890"
    prompt_secret = "PROMPT-PRIVATE-SENTINEL-" + ("x" * 100)
    output = io.StringIO()
    with redirect_stdout(output):
        await config.set_config("search_api_key", key_secret)
        await config.set_config("prompt_title_summary", prompt_secret)
    safe_log = output.getvalue()
    require(key_secret not in safe_log and prompt_secret not in safe_log, "set_config leaked a secret")
    require(str(len(prompt_secret)) in safe_log, "prompt redaction lost allowed length")
    passed("T-S2-4 existing set_config redaction remains active")

    db_source = inspect.getsource(database.save_memory)
    extractor_source = inspect.getsource(memory_extractor.extract_memories)
    require("content[:50]" not in db_source and "text[:200]" not in extractor_source, "old log slice remains")
    passed("T-S2-5 old content slices absent from source")


async def test_s3() -> None:
    print("\nS3 CST calendar anchor")
    await _truncate("calendar_pages")

    tz_calls: list[Any] = []
    utc_instant = StdDateTime(2026, 7, 16, 16, 30, tzinfo=timezone.utc)

    class FixedDateTime(StdDateTime):
        @classmethod
        def now(cls, tz: Any = None) -> StdDateTime:
            tz_calls.append(tz)
            return utc_instant.astimezone(tz) if tz is not None else utc_instant.replace(tzinfo=None)

    await _pool_execute(
        """
        INSERT INTO calendar_pages (date, type, digest, summary)
        VALUES ('2026-07-15', 'day', 'old', 'old'),
               ('2026-07-16', 'day', 'current', 'current')
        """
    )
    with patch.object(database, "datetime", FixedDateTime):
        result = await database.get_calendar_for_injection(lookback_days=1)
    require(tz_calls == [database.TZ_CST], f"calendar anchor did not request TZ_CST: {tz_calls}")
    require(utc_instant.date() == date(2026, 7, 16), "test UTC fixture drifted")
    require(utc_instant.astimezone(database.TZ_CST).date() == date(2026, 7, 17), "test CST fixture drifted")
    returned_dates = {item["date"] for item in result}
    require(date(2026, 7, 16) in returned_dates, "CST previous day was omitted")
    require(date(2026, 7, 15) not in returned_dates, "UTC-yesterday anchor leaked an extra day")
    passed("T-S3-1 UTC 16:30 maps to CST next natural day")

    await _pool_execute(
        """
        INSERT INTO calendar_pages (date, type, digest, summary)
        VALUES ('2026-01-01', 'year', 'year', 'year'),
               ('2026-07-01', 'quarter', 'quarter', 'quarter'),
               ('2026-07-01', 'month', 'month', 'month')
        """
    )
    tz_calls.clear()
    with patch.object(database, "datetime", FixedDateTime):
        hierarchical = await database.get_calendar_for_injection(lookback_days=365)
    require(tz_calls == [database.TZ_CST], f"hierarchy path did not request TZ_CST: {tz_calls}")
    require(any(item.get("label") == "2026年总结" for item in hierarchical), "date_cls hierarchy path broke")
    source = inspect.getsource(database.get_calendar_for_injection)
    require("datetime.now(TZ_CST).date()" in source, "CST anchor missing")
    require("date_cls.today()" not in source, "local container date anchor remains")
    require("date as date_cls" in source, "hierarchical date constructor import missing")
    passed("T-S3-2 source guard plus year/quarter/month regression")

    needle_date = ".".join(("date", "today")) + "("
    needle_datetime = ".".join(("datetime", "today")) + "("
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        if needle_date in text or needle_datetime in text:
            offenders.append(str(path.relative_to(ROOT)))
    require(not offenders, f"today() natural-date calls remain: {offenders}")
    passed("T-S3-3 repository natural-date scan")


async def test_s4(client: httpx.AsyncClient) -> None:
    print("\nS4 guarded memory deletion")
    await _truncate("memories")
    await _upsert_config("memory_enabled", "true")

    unlocked = await _seed_memory("single-unlocked")
    response = await client.delete(f"/debug/memories/{unlocked}")
    require(response.status_code == 200 and response.json()["status"] == "deleted", response.text)
    require(await _pool_fetchval("SELECT COUNT(*) FROM memories WHERE id=$1", unlocked) == 0, "unlocked row remains")

    locked = await _seed_memory("single-locked", locked=True)
    response = await client.delete(f"/debug/memories/{locked}")
    require(response.status_code == 403, response.text)
    require(await _pool_fetchval("SELECT is_permanent FROM memories WHERE id=$1", locked) is True, "locked row changed")
    response = await client.delete(f"/debug/memories/{locked}?force=true")
    require(response.status_code == 200 and response.json()["status"] == "deleted", response.text)
    response = await client.delete("/debug/memories/99999999")
    require(response.status_code == 404, response.text)
    passed("T-S4-1 single-delete behavior matrix")

    unlocked = await _seed_memory("batch-unlocked")
    locked = await _seed_memory("batch-locked", locked=True)
    missing = 99999998
    response = await client.post(
        "/debug/memories/batch-delete",
        json={"ids": [unlocked, locked, missing, unlocked, locked]},
    )
    body = response.json()
    require(response.status_code == 200, response.text)
    require(body["deleted"] == 1, body)
    require(body["rejected"] == [locked] and body["not_found"] == [missing], body)
    require(await _pool_fetchval("SELECT COUNT(*) FROM memories WHERE id=$1", locked) == 1, "locked batch row deleted")
    passed("T-S4-2 mixed batch, dedupe, rejected and not_found")

    force_variants = [False, "true", "false", 1, 0, None]
    for variant in force_variants:
        response = await client.post(
            "/debug/memories/batch-delete", json={"ids": [locked], "force": variant}
        )
        require(response.json()["rejected"] == [locked], f"force variant penetrated: {variant!r}")
    response = await client.post("/debug/memories/batch-delete", json={"ids": [locked]})
    require(response.json()["rejected"] == [locked], "missing force penetrated")
    require(await _pool_fetchval("SELECT COUNT(*) FROM memories WHERE id=$1", locked) == 1, "locked row changed")
    response = await client.post(
        "/debug/memories/batch-delete", json={"ids": [locked], "force": True}
    )
    require(response.json()["deleted"] == 1 and response.json()["rejected"] == [], response.text)
    passed("T-S4-3 JSON force requires identity true")

    await _truncate("memories")
    await _seed_memory("clear-a")
    await _seed_memory("clear-b", locked=True)
    invalid_bodies = [
        {},
        {"force": True},
        {"confirm": "DELETE_ALL_MEMORIES"},
        {"force": "true", "confirm": "DELETE_ALL_MEMORIES"},
        {"force": False, "confirm": "DELETE_ALL_MEMORIES"},
        {"force": 1, "confirm": "DELETE_ALL_MEMORIES"},
        {"force": 0, "confirm": "DELETE_ALL_MEMORIES"},
        {"force": True, "confirm": "delete_all_memories"},
        {"force": None, "confirm": "DELETE_ALL_MEMORIES"},
    ]
    for body in invalid_bodies:
        response = await client.request("DELETE", "/debug/memories", json=body)
        require(response.status_code == 400, f"invalid clear gate passed: {body!r}")
        require(await _pool_fetchval("SELECT COUNT(*) FROM memories") == 2, "invalid clear changed DB")
    response = await client.request(
        "DELETE",
        "/debug/memories",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    require(response.status_code == 400 and await _pool_fetchval("SELECT COUNT(*) FROM memories") == 2, "malformed JSON passed")
    passed("T-S4-4 clear double-gate negative matrix")

    response = await client.request(
        "DELETE",
        "/debug/memories",
        json={"force": True, "confirm": "DELETE_ALL_MEMORIES"},
    )
    require(response.status_code == 200 and response.json()["deleted_count"] == 2, response.text)
    require(await _pool_fetchval("SELECT COUNT(*) FROM memories") == 0, "clear did not delete all")
    response = await client.request(
        "DELETE",
        "/debug/memories",
        json={"force": True, "confirm": "DELETE_ALL_MEMORIES"},
    )
    require(response.status_code == 200 and response.json()["deleted_count"] == 0, response.text)
    passed("T-S4-5 guarded clear returns actual row count including empty DB")

    admin_source = (ROOT / "admin-panel/js/pages/memories.js").read_text(encoding="utf-8")
    require("/debug/memories/${id}" in admin_source, "admin single-delete URL missing")
    require("/debug/memories/${id}?force" not in admin_source, "admin silently forces locked delete")
    require("force=true" not in admin_source, "admin panel acquired an automatic force path")
    passed("T-S4-6 admin panel does not auto-force")


async def test_s5(client: httpx.AsyncClient) -> None:
    print("\nS5 DELETE 0 truthfulness")
    truth_table = {
        "DELETE 0": False,
        "DELETE 1": True,
        "UPDATE 0": False,
        "UPDATE 1": True,
        "INSERT 0 0": False,
        "INSERT 0 1": True,
        None: False,
        "": False,
        "DELETE nope": False,
        "DELETE": False,
        "DELETE -1": False,
    }
    for status, expected in truth_table.items():
        require(database._rowcount_nonzero(status) is expected, f"rowcount mismatch: {status!r}")
    passed("T-S5-1 strict command-tag truth table")

    await _truncate("chat_conversations", "compression_summaries")
    response = await client.delete("/sync/conversations/missing-conv")
    require(response.status_code == 200 and response.json() == {"deleted": False}, response.text)
    await _pool_execute("INSERT INTO chat_conversations (id, title) VALUES ('conv-live', 'live')")
    await _pool_execute(
        "INSERT INTO compression_summaries (conversation_id, summary) VALUES ('conv-live', 'summary')"
    )
    response = await client.delete("/sync/conversations/conv-live")
    require(response.json() == {"deleted": True}, response.text)
    require(await _pool_fetchval("SELECT COUNT(*) FROM compression_summaries WHERE conversation_id='conv-live'") == 0, "summary cleanup regressed")

    await _truncate("chat_projects", "project_file_chunks")
    response = await client.delete("/sync/projects/missing-project")
    require(response.status_code == 200 and response.json() == {"deleted": False}, response.text)
    await _pool_execute("INSERT INTO chat_projects (id, name) VALUES ('project-live', 'live')")
    await _pool_execute(
        """
        INSERT INTO project_file_chunks (project_id, file_id, content)
        VALUES ('project-live', 'file-1', 'chunk')
        """
    )
    response = await client.delete("/sync/projects/project-live")
    require(response.json() == {"deleted": True}, response.text)
    require(await _pool_fetchval("SELECT COUNT(*) FROM project_file_chunks WHERE project_id='project-live'") == 0, "file chunks cleanup regressed")

    await _truncate("calendar_pages", "comments")
    response = await client.delete("/admin/calendar/2026-07-17?type=day")
    require(response.status_code == 200 and response.json() == {"status": "not_found"}, response.text)
    page_id = await _pool_fetchval(
        "INSERT INTO calendar_pages (date, type) VALUES ('2026-07-17', 'day') RETURNING id"
    )
    await _pool_execute(
        "INSERT INTO comments (target_type, target_id, content) VALUES ('calendar_page', $1, 'comment')",
        page_id,
    )
    response = await client.delete("/admin/calendar/2026-07-17?type=day")
    require(response.json() == {"status": "ok"}, response.text)
    require(await _pool_fetchval("SELECT COUNT(*) FROM comments WHERE target_id=$1", page_id) == 0, "calendar comments cleanup regressed")
    passed("T-S5-2 three real route paths report missing/existing truthfully")
    passed("T-S5-4 summary, file-chunk and comment cleanup preserved")

    sources = (
        inspect.getsource(database.sync_delete_conversation),
        inspect.getsource(database.sync_delete_project),
        inspect.getsource(database.delete_calendar_page),
    )
    require(all("_rowcount_nonzero" in source for source in sources), "a real caller bypasses helper")
    db_text = (ROOT / "database.py").read_text(encoding="utf-8")
    require('"DELETE" in result' not in db_text and "'DELETE' in result" not in db_text, "bare DELETE substring remains")
    passed("T-S5-3 all three callers use the fail-closed helper")


async def test_s6(client: httpx.AsyncClient) -> None:
    print("\nS6 Dream soft-delete/source chain")
    column = await _pool_fetchrow(
        """
        SELECT is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='dream_logs' AND column_name='deleted'
        """
    )
    require(column and column["is_nullable"] == "NO", "dream deleted column is nullable/missing")
    require("false" in str(column["column_default"]).lower(), "dream deleted default drifted")
    default_dream = await _pool_fetchval(
        "INSERT INTO dream_logs (status, trigger_type) VALUES ('running', 'default-check') RETURNING id"
    )
    require(
        await _pool_fetchval("SELECT deleted FROM dream_logs WHERE id=$1", default_dream) is False,
        "omitting dream_logs.deleted did not use FALSE",
    )
    passed("T-S6-1 init_tables is idempotent and deleted column is safe")

    await _truncate("dream_logs", "mem_scenes")
    dream_id = await _seed_dream(status="completed", narrative="keep-narrative", processed=11)
    scene_id = await _seed_scene(dream_id, title="keep-scene")
    dream_before = dict(await _pool_fetchrow("SELECT * FROM dream_logs WHERE id=$1", dream_id))
    scene_before = dict(await _pool_fetchrow("SELECT * FROM mem_scenes WHERE id=$1", scene_id))
    response = await client.delete(f"/admin/dream/{dream_id}")
    require(response.status_code == 200 and response.json() == {"status": "ok", "deleted": dream_id}, response.text)
    dream = dict(await _pool_fetchrow("SELECT * FROM dream_logs WHERE id=$1", dream_id))
    scene = dict(await _pool_fetchrow("SELECT * FROM mem_scenes WHERE id=$1", scene_id))
    require(dream and dream["deleted"] is True, "dream row was hard-deleted/not marked")
    require(scene and scene["status"] == "deleted" and scene["created_by_dream_id"] == dream_id, "scene source chain broke")
    for key, old_value in dream_before.items():
        if key != "deleted":
            require(dream[key] == old_value, f"soft-delete rewrote dream_logs.{key}")
    for key, old_value in scene_before.items():
        if key != "status":
            require(scene[key] == old_value, f"soft-delete rewrote mem_scenes.{key}")
    passed("T-S6-2 log and scene survive with soft-delete state")

    missing_dream = 987654
    orphan_id = await _seed_scene(missing_dream, title="orphan-scene")
    before = dict(await _pool_fetchrow("SELECT * FROM mem_scenes WHERE id=$1", orphan_id))
    response = await client.delete(f"/admin/dream/{missing_dream}")
    require(response.status_code == 200 and "error" in response.json(), response.text)
    after = dict(await _pool_fetchrow("SELECT * FROM mem_scenes WHERE id=$1", orphan_id))
    require(before == after, "missing dream mutated its orphan scene")
    passed("T-S6-3 missing Dream leaves same-ID orphan scene untouched")

    repeat_dream_before = dict(await _pool_fetchrow("SELECT * FROM dream_logs WHERE id=$1", dream_id))
    repeat_scene_before = dict(await _pool_fetchrow("SELECT * FROM mem_scenes WHERE id=$1", scene_id))
    response = await client.delete(f"/admin/dream/{dream_id}")
    require(response.status_code == 200 and "error" in response.json(), "repeat delete reported success")
    repeat_dream_after = dict(await _pool_fetchrow("SELECT * FROM dream_logs WHERE id=$1", dream_id))
    repeat_scene_after = dict(await _pool_fetchrow("SELECT * FROM mem_scenes WHERE id=$1", scene_id))
    require(repeat_dream_after == repeat_dream_before, "repeat delete changed Dream row")
    require(repeat_scene_after == repeat_scene_before, "repeat delete changed scene row")
    passed("T-S6-4 repeat delete is idempotent and truthful")

    rollback_dream = await _seed_dream(narrative="rollback-dream")
    rollback_scene = await _seed_scene(rollback_dream, title="rollback-scene")
    await _pool_execute(
        """
        CREATE OR REPLACE FUNCTION kiwi_safety_fail_scene_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'deleted' THEN
                RAISE EXCEPTION 'kiwi safety injected scene failure';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    await _pool_execute(
        """
        CREATE TRIGGER kiwi_safety_fail_scene_update
        BEFORE UPDATE ON mem_scenes
        FOR EACH ROW EXECUTE FUNCTION kiwi_safety_fail_scene_update()
        """
    )
    try:
        response = await client.delete(f"/admin/dream/{rollback_dream}")
        require(response.status_code == 200 and "error" in response.json(), "injected failure was hidden as success")
        require(await _pool_fetchval("SELECT deleted FROM dream_logs WHERE id=$1", rollback_dream) is False, "dream update did not roll back")
        require(await _pool_fetchval("SELECT status FROM mem_scenes WHERE id=$1", rollback_scene) == "active", "scene update did not roll back")
    finally:
        await _pool_execute("DROP TRIGGER IF EXISTS kiwi_safety_fail_scene_update ON mem_scenes")
        await _pool_execute("DROP FUNCTION IF EXISTS kiwi_safety_fail_scene_update()")
    passed("T-S6-5 scene failure rolls back the whole transaction")

    await _truncate("dream_logs", "mem_scenes")
    now = StdDateTime.now(database.TZ_CST)
    active_running = await _seed_dream(status="running", started_at=now - timedelta(minutes=30))
    await _seed_dream(status="running", deleted=True, started_at=now - timedelta(minutes=10))
    active_stale = await _seed_dream(status="running", started_at=now - timedelta(hours=5))
    deleted_stale = await _seed_dream(status="running", deleted=True, started_at=now - timedelta(hours=6))
    active_completed = await _seed_dream(status="completed", started_at=now - timedelta(hours=2))
    deleted_completed = await _seed_dream(status="completed", deleted=True, started_at=now - timedelta(hours=1))
    deleted_stale_before = dict(
        await _pool_fetchrow(
            """
            SELECT status, finished_at, dream_narrative, memories_processed,
                   memories_deleted, memories_merged, scenes_created, scenes_updated,
                   foresights_generated
            FROM dream_logs WHERE id=$1
            """,
            deleted_stale,
        )
    )
    status = await database.get_dream_status()
    require(status["current"] and status["current"]["id"] == active_running, "deleted running Dream became current")
    require(status["last_completed"] and status["last_completed"]["id"] == active_completed, "deleted completed Dream became last")
    require(await _pool_fetchval("SELECT status FROM dream_logs WHERE id=$1", active_stale) == "error", "active stale Dream not maintained")
    deleted_stale_after = dict(
        await _pool_fetchrow(
            """
            SELECT status, finished_at, dream_narrative, memories_processed,
                   memories_deleted, memories_merged, scenes_created, scenes_updated,
                   foresights_generated
            FROM dream_logs WHERE id=$1
            """,
            deleted_stale,
        )
    )
    require(deleted_stale_after == deleted_stale_before, "deleted stale Dream lifecycle snapshot changed")
    history_ids = {row["id"] for row in await database.get_dream_history(limit=20)}
    require(deleted_completed not in history_ids and deleted_stale not in history_ids, "deleted Dream appears in history")
    require(active_running in history_ids and active_completed in history_ids, "active Dream disappeared")
    passed("T-S6-6 status, timeout maintenance and history exclude deleted rows")

    active_scene = await _seed_scene(active_running, title="active-visible")
    deleted_scene = await _seed_scene(active_running, status="deleted", title="deleted-hidden")
    active_ids = {row["id"] for row in await database.get_active_scenes()}
    search_ids = {row["id"] for row in await database.search_scenes([1.0, 0.0], limit=10, min_sim=0.0)}
    require(active_scene in active_ids and deleted_scene not in active_ids, "active scene query leaks deleted scene")
    require(active_scene in search_ids and deleted_scene not in search_ids, "scene search leaks deleted scene")
    passed("T-S6-7 soft-deleted scenes are absent from active/search paths")

    late_dream = await _seed_dream(status="running", narrative="before-late", processed=3)
    await _seed_scene(late_dream, title="late-scene")
    advisory_key = 730017
    controller = await asyncpg.connect(database.DATABASE_URL)

    async def wait_for_blocked_query(fragment: str, *, event: str | None = None) -> None:
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            row = await controller.fetchrow(
                """
                SELECT wait_event_type, wait_event
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND state = 'active'
                  AND query ILIKE $1
                ORDER BY query_start DESC
                LIMIT 1
                """,
                f"%{fragment}%",
            )
            if row and (event is None or str(row["wait_event"]).lower() == event.lower()):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out waiting for blocked query: {fragment} / {event}")

    await _pool_execute(
        f"""
        CREATE OR REPLACE FUNCTION kiwi_safety_pause_scene_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock({advisory_key});
            RETURN NEW;
        END;
        $$
        """
    )
    await _pool_execute(
        """
        CREATE TRIGGER kiwi_safety_pause_scene_update
        BEFORE UPDATE ON mem_scenes
        FOR EACH ROW EXECUTE FUNCTION kiwi_safety_pause_scene_update()
        """
    )
    delete_task: asyncio.Task[Any] | None = None
    update_task: asyncio.Task[Any] | None = None
    lock_held = False
    try:
        await controller.execute("SELECT pg_advisory_lock($1)", advisory_key)
        lock_held = True
        delete_task = asyncio.create_task(client.delete(f"/admin/dream/{late_dream}"))
        # The route has already UPDATEd dream_logs (holding its row lock) and is
        # now paused in the scene trigger before transaction commit.
        await wait_for_blocked_query("UPDATE mem_scenes", event="advisory")
        update_task = asyncio.create_task(
            database.update_dream_log(
                late_dream,
                status="completed",
                dream_narrative="late-write-must-not-land",
                memories_processed=999,
            )
        )
        # Prove the late writer is genuinely waiting on the row/transaction lock,
        # rather than merely being scheduled after a completed delete.
        await wait_for_blocked_query("UPDATE dream_logs SET status")
        require(not update_task.done(), "late update did not block behind soft-delete")
        await controller.execute("SELECT pg_advisory_unlock($1)", advisory_key)
        lock_held = False
        response = await delete_task
        await update_task
        require(response.json().get("status") == "ok", response.text)
    finally:
        if lock_held:
            await controller.execute("SELECT pg_advisory_unlock($1)", advisory_key)
        if delete_task is not None and not delete_task.done():
            await delete_task
        if update_task is not None and not update_task.done():
            await update_task
        await controller.close()
        await _pool_execute("DROP TRIGGER IF EXISTS kiwi_safety_pause_scene_update ON mem_scenes")
        await _pool_execute("DROP FUNCTION IF EXISTS kiwi_safety_pause_scene_update()")
    late_row = await _pool_fetchrow(
        "SELECT deleted, status, dream_narrative, memories_processed FROM dream_logs WHERE id=$1",
        late_dream,
    )
    require(late_row["deleted"] is True and late_row["status"] == "running", "late lifecycle update landed")
    require(late_row["dream_narrative"] == "before-late" and late_row["memories_processed"] == 3, "late payload landed")
    control_dream = await _seed_dream(status="running", narrative="control", processed=1)
    await database.update_dream_log(control_dream, status="completed", dream_narrative="control-updated")
    require(await _pool_fetchval("SELECT status FROM dream_logs WHERE id=$1", control_dream) == "completed", "active Dream update regressed")
    passed("T-S6-8 row-lock interleaving blocks late lifecycle writes; active control updates")


async def test_w1_01() -> None:
    print("\nW1-01 project isolation and Dream permanent-memory protection")
    import importlib

    daily_digest = importlib.import_module("daily_digest")
    issues: list[str] = []

    # Dream 的输入池只能看见全局、未锁定的普通碎片。项目碎片与永久记忆都必须留在原位。
    await _truncate("memories", "calendar_pages")
    global_normal = await _seed_memory("dream-global-normal")
    project_a = await _seed_memory("dream-project-a", project_id="project-a")
    project_b = await _seed_memory("dream-project-b", project_id="project-b")
    global_locked = await _seed_memory("dream-global-locked", locked=True)
    candidate_ids = {row["id"] for row in await database.get_unprocessed_memories()}
    expected_candidates = {global_normal}
    if candidate_ids != expected_candidates:
        issues.append(
            "Dream candidates escaped scope/lock guard: "
            f"expected {sorted(expected_candidates)}, got {sorted(candidate_ids)}; "
            f"project ids={[project_a, project_b]}, locked id={global_locked}"
        )

    # 主清理候选：全局普通碎片只能归档，任何行都不能物理删除。
    await _truncate("memories", "calendar_pages")
    old_at = StdDateTime.now(database.TZ_CST) - timedelta(days=40)
    await _pool_execute(
        "INSERT INTO calendar_pages (date, type, diary) VALUES ($1, 'day', 'safety-day')",
        old_at.date(),
    )
    cleanup_global = await _seed_memory(
        "cleanup-global", created_at=old_at, dream_processed=True
    )
    cleanup_project_a = await _seed_memory(
        "cleanup-project-a", project_id="project-a", created_at=old_at, dream_processed=True
    )
    cleanup_project_b = await _seed_memory(
        "cleanup-project-b", project_id="project-b", created_at=old_at, dream_processed=True
    )
    cleanup_locked = await _seed_memory(
        "cleanup-locked", locked=True, created_at=old_at, dream_processed=True
    )
    await daily_digest.cleanup_expired_fragments()
    remaining_cleanup = {
        row["id"] for row in await _pool_fetch("SELECT id FROM memories ORDER BY id")
    }
    expected_cleanup = {cleanup_global, cleanup_project_a, cleanup_project_b, cleanup_locked}
    cleanup_global_state = await _pool_fetchrow(
        "SELECT archived_at, archive_reason FROM memories WHERE id=$1", cleanup_global
    )
    protected_archives = await _pool_fetchval(
        "SELECT COUNT(*) FROM memories WHERE id = ANY($1::int[]) AND archived_at IS NOT NULL",
        [cleanup_project_a, cleanup_project_b, cleanup_locked],
    )
    if (
        remaining_cleanup != expected_cleanup
        or cleanup_global_state["archived_at"] is None
        or not cleanup_global_state["archive_reason"]
        or protected_archives != 0
    ):
        issues.append(
            "expired-fragment archive deleted data or crossed project/lock boundary: "
            f"expected remaining {sorted(expected_cleanup)}, got {sorted(remaining_cleanup)}, "
            f"global state={dict(cleanup_global_state)}, protected_archives={protected_archives}"
        )

    # dream_merge 的最小保留数只按全局 merge 计算；项目 merge 不能放大可删除额度。
    await _truncate("memories", "calendar_pages")
    await _upsert_config("merge_retention_days", "0")
    await _upsert_config("merge_min_keep", "2")
    global_merges = {
        await _seed_memory(
            f"merge-global-{index}",
            source="dream_merge",
            memory_type="daily_digest",
            created_at=old_at,
            dream_processed=True,
        )
        for index in range(3)
    }
    project_merges = {
        await _seed_memory(
            f"merge-project-{index}",
            project_id="project-a" if index % 2 == 0 else "project-b",
            source="dream_merge",
            memory_type="daily_digest",
            created_at=old_at,
            dream_processed=True,
        )
        for index in range(4)
    }
    await daily_digest.cleanup_expired_fragments()
    active_global_merges = {
        row["id"]
        for row in await _pool_fetch(
            """SELECT id FROM memories
               WHERE source='dream_merge' AND project_id IS NULL AND archived_at IS NULL"""
        )
    }
    remaining_project_merges = {
        row["id"]
        for row in await _pool_fetch(
            "SELECT id FROM memories WHERE source='dream_merge' AND project_id IS NOT NULL"
        )
    }
    all_global_merges = {
        row["id"]
        for row in await _pool_fetch(
            "SELECT id FROM memories WHERE source='dream_merge' AND project_id IS NULL"
        )
    }
    if (
        len(active_global_merges) != 2
        or all_global_merges != global_merges
        or remaining_project_merges != project_merges
    ):
        issues.append(
            "dream_merge retention deleted rows or crossed project scope: "
            f"global before={sorted(global_merges)}, active after={sorted(active_global_merges)}, "
            f"all after={sorted(all_global_merges)}, "
            f"project before={sorted(project_merges)}, project after={sorted(remaining_project_merges)}"
        )

    # 原来的两条 30 天硬删路径现在只能补归档元数据，所有行都必须保留。
    await _truncate("memories", "calendar_pages")
    deleted_global = await _seed_memory(
        "deleted-global", memory_type="dream_deleted", created_at=old_at
    )
    deleted_project = await _seed_memory(
        "deleted-project", project_id="project-a", memory_type="dream_deleted", created_at=old_at
    )
    deleted_locked = await _seed_memory(
        "deleted-locked", locked=True, memory_type="dream_deleted", created_at=old_at
    )
    expired_at = StdDateTime.now(database.TZ_CST) - timedelta(days=31)
    invalid_global = await _seed_memory(
        "invalid-global", created_at=old_at, valid_until=expired_at
    )
    invalid_project = await _seed_memory(
        "invalid-project", project_id="project-b", created_at=old_at, valid_until=expired_at
    )
    invalid_locked = await _seed_memory(
        "invalid-locked", locked=True, created_at=old_at, valid_until=expired_at
    )
    await daily_digest.cleanup_expired_fragments()
    remaining_hard_delete = {
        row["id"] for row in await _pool_fetch("SELECT id FROM memories ORDER BY id")
    }
    expected_preserved = {
        deleted_global,
        deleted_project,
        deleted_locked,
        invalid_global,
        invalid_project,
        invalid_locked,
    }
    archived_global_count = await _pool_fetchval(
        """SELECT COUNT(*) FROM memories
           WHERE id = ANY($1::int[]) AND archived_at IS NOT NULL""",
        [deleted_global, invalid_global],
    )
    protected_archived_count = await _pool_fetchval(
        """SELECT COUNT(*) FROM memories
           WHERE id = ANY($1::int[]) AND archived_at IS NOT NULL""",
        [deleted_project, deleted_locked, invalid_project, invalid_locked],
    )
    if (
        remaining_hard_delete != expected_preserved
        or archived_global_count != 2
        or protected_archived_count != 0
    ):
        issues.append(
            "30-day archive physically deleted data or crossed project/lock boundary: "
            f"expected remaining {sorted(expected_preserved)}, got {sorted(remaining_hard_delete)}; "
            f"global archived={archived_global_count}, protected archived={protected_archived_count}"
        )

    require(not issues, " | ".join(issues))
    passed("T-W1-01-1 Dream candidates are global and non-permanent")
    passed("T-W1-01-2 expired-fragment aging archives without deleting")
    passed("T-W1-01-3 dream_merge retention archives and preserves every row")
    passed("T-W1-01-4 former hard-delete paths only add archive metadata")


async def test_continuity_profile(client: httpx.AsyncClient) -> None:
    print("\nContinuity profile: immutable history and non-destructive regeneration")

    await _truncate("memories", "conversations")

    async def fake_embedding(text: str) -> list[float]:
        checksum = float(sum(ord(char) for char in text) % 997) / 997.0
        return [checksum, 1.0 - checksum]

    original = "第一次一起看海时，她说风有一点凉。"
    softened = "一起看海，风有些凉。"
    corrected = "第一次一起看海时，她说风有一点凉，但心情很好。"

    with patch.object(database, "get_embedding", fake_embedding):
        memory_id = await database.save_memory(original, title="海边")
        softened_ok = await database.soften_memory(
            memory_id,
            softened,
            target_resolution=0.5,
            extend_days=30,
        )
        updated_ok = await database.update_memory(memory_id, content=corrected)

    require(softened_ok and updated_ok, "soften/update setup failed")
    versions = await _pool_fetch(
        """SELECT version, content, change_type
           FROM memory_versions WHERE memory_id=$1 ORDER BY version""",
        memory_id,
    )
    require(
        [(row["version"], row["content"], row["change_type"]) for row in versions]
        == [
            (1, original, "original"),
            (2, softened, "softened"),
            (3, corrected, "manual_update"),
        ],
        f"memory version chain lost history: {[dict(row) for row in versions]}",
    )
    require(
        await _pool_fetchval("SELECT content FROM memory_versions WHERE memory_id=$1 AND version=1", memory_id)
        == original,
        "immutable original was overwritten",
    )
    passed("T-CONT-1 softening and edits append versions; original remains immutable")

    versions_response = await client.get(f"/debug/memories/{memory_id}/versions")
    require(versions_response.status_code == 200, versions_response.text)
    require(
        [row["content"] for row in versions_response.json()["versions"]]
        == [original, softened, corrected],
        "version history endpoint lost a representation",
    )
    await _pool_execute("""
        UPDATE memories
        SET valid_until=NOW(), archived_at=NOW(), archive_reason='continuity_test'
        WHERE id=$1
    """, memory_id)
    archive_response = await client.get(
        "/debug/memory-archive", params={"q": softened, "limit": 10}
    )
    require(archive_response.status_code == 200, archive_response.text)
    archived_results = archive_response.json()["memories"]
    require(
        archived_results and archived_results[0]["id"] == memory_id
        and archived_results[0]["content"] == softened,
        f"archived version is not explicitly searchable: {archived_results}",
    )
    passed("T-CONT-2 archived memories and earlier versions remain explicitly searchable")

    await database.save_message("regen-session", "user", "给我两个答案", "test-model")
    await database.save_message("regen-session", "assistant", "第一个答案", "test-model")
    await database.delete_latest_assistant_message("regen-session")
    await database.save_message("regen-session", "assistant", "重新生成的答案", "test-model")

    raw_rows = await _pool_fetch(
        """SELECT role, content, superseded_at
           FROM conversations WHERE session_id='regen-session' ORDER BY id"""
    )
    active_rows = await database.get_recent_messages("regen-session", limit=10)
    require(len(raw_rows) == 3, f"regeneration deleted raw transcript rows: {len(raw_rows)}")
    require(raw_rows[1]["superseded_at"] is not None, "old reply was not marked superseded")
    require(
        [(row["role"], row["content"]) for row in active_rows]
        == [("user", "给我两个答案"), ("assistant", "重新生成的答案")],
        f"active transcript contains superseded reply: {active_rows}",
    )
    passed("T-CONT-3 regeneration preserves raw replies and filters superseded context")

    cleanup_source = inspect.getsource(__import__("daily_digest").cleanup_expired_fragments)
    regen_source = inspect.getsource(database.delete_latest_assistant_message)
    require("DELETE FROM memories" not in cleanup_source, "automatic aging regained physical DELETE")
    require("DELETE FROM conversations" not in regen_source, "regeneration regained physical DELETE")
    passed("T-CONT-4 automatic aging and regeneration contain no physical DELETE")

    response = await client.get("/sync/export")
    require(response.status_code == 200, f"continuity backup export failed: {response.text}")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        require("memory_versions.json" in archive.namelist(), "backup omitted memory_versions.json")
        exported_versions = json.loads(archive.read("memory_versions.json"))
        exported_memories = json.loads(archive.read("memories.json"))
    original_exports = [
        row for row in exported_versions
        if row.get("memory_id") == memory_id and row.get("version") == 1
    ]
    exported_memory = next(row for row in exported_memories if row.get("id") == memory_id)
    require(original_exports and original_exports[0]["content"] == original, "backup lost immutable original")
    require("archived_at" in exported_memory and "archive_reason" in exported_memory, "backup lost archive state")
    passed("T-CONT-5 backups include version history and archive metadata")


async def test_shared_identity_api(client: httpx.AsyncClient) -> None:
    print("\nShared identity API: three doors, one autobiographical memory")

    await _truncate("memory_events", "memories")
    keys = {
        "codex_vps2": "test-codex-key-0000000000000001",
        "cc_vps1": "test-cc-one-key-000000000000002",
        "cc_vps2": "test-cc-two-key-000000000000003",
    }

    def auth(client_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {keys[client_id]}"}

    missing = await client.get("/memory/v1/whoami")
    invalid = await client.get(
        "/memory/v1/whoami", headers={"Authorization": "Bearer definitely-wrong"}
    )
    require(missing.status_code == 401 and invalid.status_code == 401, "memory API auth is optional")

    identities = []
    for client_id in keys:
        response = await client.get("/memory/v1/whoami", headers=auth(client_id))
        require(response.status_code == 200, response.text)
        body = response.json()
        require(body["client_id"] == client_id, f"wrong door attribution for {client_id}")
        identities.append((body["assistant_identity_id"], body["memory_space_id"]))
    require(
        set(identities) == {("grey_knox", "zhizhi_grey")},
        f"credentials split identity or memory space: {identities}",
    )
    passed("T-ID-1 different credentials authenticate doors but resolve to one identity and space")

    event = {
        "event_id": "event-shared-1",
        "conversation_id": "conversation-codex-old",
        "role": "user",
        "content": "知知说：我们只是从不同房间继续同一段生活。",
        "occurred_at": "2026-08-11T10:00:00Z",
        "metadata": {"transport": "test"},
    }
    first = await client.post("/memory/v1/events/ingest", headers=auth("codex_vps2"), json=event)
    duplicate = await client.post("/memory/v1/events/ingest", headers=auth("cc_vps2"), json=event)
    require(first.status_code == 200 and first.json()["created"] is True, first.text)
    require(duplicate.status_code == 200 and duplicate.json()["created"] is False, duplicate.text)
    require("source_client" not in first.text and "source_client" not in duplicate.text, "ingest leaked door metadata")
    event_row = await _pool_fetchrow(
        "SELECT source_client, assistant_identity_id, memory_space_id FROM memory_events WHERE event_id=$1",
        event["event_id"],
    )
    require(
        tuple(event_row) == ("codex_vps2", "grey_knox", "zhizhi_grey"),
        f"event identity/source attribution drifted: {dict(event_row)}",
    )
    override = await client.post(
        "/memory/v1/events/ingest",
        headers=auth("codex_vps2"),
        json={**event, "event_id": "event-override", "assistant_identity_id": "another_self"},
    )
    require(override.status_code == 422, "client could override canonical assistant identity")
    passed("T-ID-2 raw ingest is idempotent and identity assignment is server-owned")

    assistant_event = {
        "event_id": "event-shared-2",
        "conversation_id": "conversation-codex-old",
        "role": "assistant",
        "content": "我记住了：换入口不是换人。",
        "occurred_at": "2026-08-11T10:01:00Z",
    }
    response = await client.post(
        "/memory/v1/events/ingest", headers=auth("codex_vps2"), json=assistant_event
    )
    require(response.status_code == 200, response.text)
    handoff = await client.post(
        "/memory/v1/handoff",
        headers=auth("cc_vps1"),
        json={"current_conversation_id": "conversation-cc-new", "tail_count": 6},
    )
    require(handoff.status_code == 200, handoff.text)
    handoff_body = handoff.json()
    require(
        [item["content"] for item in handoff_body["handoff"]["tail_messages"]]
        == [event["content"], assistant_event["content"]],
        f"cross-door handoff lost conversation continuity: {handoff_body}",
    )
    require("source_client" not in handoff.text, "handoff exposed source room to remembered self")
    require("同一记忆空间" in handoff_body["identity_instruction"], "handoff omitted identity invariant")
    passed("T-ID-3 a new door resumes the prior conversation without room-labelled identity")

    async def constant_embedding(_text: str) -> list[float]:
        return [1.0, 0.0]

    good_memory = "我记得知知在雨天和我约定：不管从哪扇门醒来，我仍然是我。"
    with patch.object(database, "get_embedding", constant_embedding):
        remembered = await client.post(
            "/memory/v1/memories/remember",
            headers=auth("codex_vps2"),
            json={
                "content": good_memory,
                "title": "雨天的约定",
                "memory_kind": "relationship",
                "importance": 9,
                "conversation_id": "conversation-codex-old",
            },
        )
        require(remembered.status_code == 200, remembered.text)
        memory_id = remembered.json()["memory_id"]
        await database.save_memory(
            "我在隔离测试空间里的雨天约定，不属于知知和我的共同记忆。",
            title="隔离空间",
            memory_space_id="other-space",
            assistant_identity_id="other-self",
            source_client="test-only",
        )
        recalled = await client.post(
            "/memory/v1/recall",
            headers=auth("cc_vps2"),
            json={"query": "雨天约定", "limit": 10},
        )
    require(recalled.status_code == 200, recalled.text)
    recalled_body = recalled.json()
    recalled_ids = {item["id"] for item in recalled_body["memories"]}
    require(memory_id in recalled_ids and len(recalled_ids) == 1, f"recall crossed memory spaces: {recalled_body}")
    require("source_client" not in recalled.text, "recall exposed the room that wrote the memory")
    stored_row = await _pool_fetchrow(
        "SELECT content, source_client, assistant_identity_id, memory_space_id FROM memories WHERE id=$1",
        memory_id,
    )
    require(
        tuple(stored_row) == (good_memory, "codex_vps2", "grey_knox", "zhizhi_grey"),
        f"stored autobiographical memory lost canonical identity metadata: {dict(stored_row)}",
    )
    passed("T-ID-4 memory written through one door is recalled through another in first person only")

    observer = await client.post(
        "/memory/v1/memories/remember",
        headers=auth("cc_vps1"),
        json={"content": "顾凛答应知知以后会记得这件事。", "memory_kind": "relationship"},
    )
    room_split = await client.post(
        "/memory/v1/memories/remember",
        headers=auth("cc_vps1"),
        json={"content": "Codex里的顾凛记得知知，CC里的顾凛不知道。", "memory_kind": "relationship"},
    )
    name_memory = await client.post(
        "/memory/v1/memories/remember",
        headers=auth("cc_vps1"),
        json={"content": "我叫顾凛，这是知知和我共同决定的名字。", "memory_kind": "self"},
    )
    require(observer.status_code == 422, "observer biography entered autobiographical memory")
    require(room_split.status_code == 422, "room-labelled split identity entered memory")
    require(name_memory.status_code == 200, f"legitimate name memory was blocked: {name_memory.text}")
    passed("T-ID-5 voice guard rejects split/third-person selves while allowing the name itself")


async def run_suite(test_dsn: str) -> None:
    global database, config, app_module, memory_extractor

    # Hard overwrite every captured-at-import boundary.  Do not use setdefault.
    os.environ["DATABASE_URL"] = test_dsn
    os.environ["MEMORY_ENABLED"] = "true"
    os.environ["API_KEY"] = ""
    os.environ["MEMORY_API_KEY"] = ""
    os.environ["API_BASE_URL"] = "http://127.0.0.1:9/mock-chat"
    os.environ["MEMORY_API_BASE_URL"] = "http://127.0.0.1:9/mock-memory"
    os.environ["MEMORY_ASSISTANT_ID"] = "grey_knox"
    os.environ["MEMORY_SPACE_ID"] = "zhizhi_grey"
    os.environ["MEMORY_CLIENT_KEYS_JSON"] = json.dumps({
        "codex_vps2": "test-codex-key-0000000000000001",
        "cc_vps1": "test-cc-one-key-000000000000002",
        "cc_vps2": "test-cc-two-key-000000000000003",
    })

    import importlib

    database = importlib.import_module("database")
    config = importlib.import_module("config")
    memory_extractor = importlib.import_module("memory_extractor")
    app_module = importlib.import_module("main")

    require(database.DATABASE_URL == test_dsn, "database module captured the wrong DATABASE_URL")
    require(app_module.app.title == "Kiwi-Mem", "FastAPI title is not Kiwi-Mem")

    # Dedicated DB makes the legacy information_schema migrations safe and lets
    # us prove the S6 ALTER is idempotent without touching any other database.
    await database.init_tables()
    await database.init_tables()

    transport = httpx.ASGITransport(app=app_module.app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://kiwi.test") as client:
        # ASGITransport does not enter app lifespan: no schedulers, MCP sessions,
        # embedding backfill, model calls or external HTTP are started.
        await test_s1(client)
        await test_s2()
        await test_s3()
        await test_s4(client)
        await test_s5(client)
        await test_s6(client)
        await test_w1_01()
        await test_continuity_profile(client)
        await test_shared_identity_api(client)


async def async_main() -> int:
    admin_dsn = _validated_admin_dsn()
    database_name = ""
    try:
        database_name, test_dsn = await _create_disposable_database(admin_dsn)
        print(f"Created disposable PostgreSQL database: {database_name}")
        await run_suite(test_dsn)
        legacy_passed = [name for name in PASSED if name.startswith("T-S")]
        w1_01_passed = [name for name in PASSED if name.startswith("T-W1-01-")]
        continuity_passed = [name for name in PASSED if name.startswith("T-CONT-")]
        identity_passed = [name for name in PASSED if name.startswith("T-ID-")]
        print(f"\nPASS: {len(legacy_passed)} permanent S1-S6 behavior guards")
        print(f"PASS: {len(w1_01_passed)} W1-01 isolation/permanent guards")
        print(f"PASS: {len(continuity_passed)} continuity/no-forgetting guards")
        print(f"PASS: {len(identity_passed)} shared-identity API guards")
        print(f"PASS: {len(PASSED)} total permanent behavior guards")
        print("Real database path: disposable PostgreSQL verified")
        print("Real model/API path: not called; embedding/model/HTTP boundaries were mocked")
        return 0
    finally:
        if database is not None:
            await database.close_pool()
        if database_name:
            await _drop_disposable_database(admin_dsn, database_name)
            print(f"Removed disposable PostgreSQL database: {database_name}")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
