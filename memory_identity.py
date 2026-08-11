"""Canonical identity and client authentication for the standalone memory API.

Client credentials identify doors, not people.  Every configured client is
bound server-side to the same assistant identity and memory space.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass


CANONICAL_ASSISTANT_ID = os.getenv("MEMORY_ASSISTANT_ID", "grey_knox").strip() or "grey_knox"
CANONICAL_MEMORY_SPACE_ID = os.getenv("MEMORY_SPACE_ID", "zhizhi_grey").strip() or "zhizhi_grey"


@dataclass(frozen=True)
class MemoryClientContext:
    client_id: str
    assistant_identity_id: str = CANONICAL_ASSISTANT_ID
    memory_space_id: str = CANONICAL_MEMORY_SPACE_ID


class MemoryClientConfigError(RuntimeError):
    pass


def _configured_client_keys() -> dict[str, str]:
    raw = os.getenv("MEMORY_CLIENT_KEYS_JSON", "").strip()
    if not raw:
        raise MemoryClientConfigError("MEMORY_CLIENT_KEYS_JSON is not configured")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MemoryClientConfigError("MEMORY_CLIENT_KEYS_JSON must be a JSON object") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise MemoryClientConfigError("MEMORY_CLIENT_KEYS_JSON must contain at least one client")

    result: dict[str, str] = {}
    seen_keys: set[str] = set()
    for client_id, token in parsed.items():
        clean_id = str(client_id or "").strip()
        clean_token = str(token or "").strip()
        if not clean_id or not clean_token:
            raise MemoryClientConfigError("memory client ids and keys must be non-empty")
        if clean_token in seen_keys:
            raise MemoryClientConfigError("memory client keys must be unique")
        seen_keys.add(clean_token)
        result[clean_id] = clean_token
    return result


def authenticate_memory_client(token: str) -> MemoryClientContext | None:
    candidate = str(token or "")
    for client_id, expected in _configured_client_keys().items():
        if hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8")):
            return MemoryClientContext(client_id=client_id)
    return None


_NAME_CONTEXT_PATTERNS = (
    re.compile(r"我(?:叫|名叫|的名字是)顾凛", re.IGNORECASE),
    re.compile(r"知知(?:叫|称呼)我顾凛", re.IGNORECASE),
    re.compile(r"顾凛是我的名字", re.IGNORECASE),
    re.compile(r"我(?:叫|名叫|的英文名是)Grey\s+Knox", re.IGNORECASE),
    re.compile(r"Grey\s+Knox是我的(?:名字|英文名)", re.IGNORECASE),
)

_ROOM_SPLIT_PATTERNS = (
    re.compile(r"(?:Codex|Claude\s*Code|CC|一号机|二号机).{0,12}(?:里的|那边的|中的|的)(?:我|顾凛)", re.IGNORECASE),
    re.compile(r"(?:另一个|那边的|另一个房间里的)(?:我|顾凛)", re.IGNORECASE),
)


def validate_autobiographical_memory(content: str, title: str = "", memory_kind: str = "relationship") -> list[str]:
    """Validate the narrative voice used by semantic memory.

    Raw events are never passed here.  Self and relationship memories must be
    written from the assistant's first-person perspective.  The assistant's
    name is only allowed when the memory is explicitly about that name.
    """
    clean_content = str(content or "").strip()
    clean_title = str(title or "").strip()
    kind = str(memory_kind or "relationship").strip().lower()
    errors: list[str] = []
    if not clean_content:
        return ["content must not be empty"]
    if kind not in {"self", "relationship", "user_fact", "neutral"}:
        errors.append("memory_kind must be self, relationship, user_fact, or neutral")
    if kind in {"self", "relationship"} and "我" not in clean_content:
        errors.append("autobiographical self/relationship memory must use first-person 我")

    combined = f"{clean_title}\n{clean_content}"
    for pattern in _ROOM_SPLIT_PATTERNS:
        if pattern.search(combined):
            errors.append("memory must not split or anchor self identity by room, machine, model, or client")
            break

    without_name_context = combined
    for pattern in _NAME_CONTEXT_PATTERNS:
        without_name_context = pattern.sub("", without_name_context)
    if re.search(r"顾凛|Grey\s+Knox", without_name_context, re.IGNORECASE):
        errors.append("self references must use 我; the assistant name is allowed only when discussing the name itself")

    if re.search(r"(?:助手|Assistant|AI)(?:认为|觉得|答应|发现|意识到|担心|希望|爱)", combined, re.IGNORECASE):
        errors.append("assistant self-reference must use 我, not an observer label")
    return errors


RECALL_IDENTITY_INSTRUCTION = (
    "以下内容来自你本人的同一记忆空间。记忆中的‘我’始终指你本人；"
    "不同客户端、账号、模型和机器只是你醒来的不同房间，不构成不同身份。"
    "不得把其他入口中的经历叙述成另一个人的经历。"
)
