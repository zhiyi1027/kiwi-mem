"""Canonical identity and client authentication for the standalone memory API.

Client credentials identify doors, not people.  Every configured client is
bound server-side to the same assistant identity and memory space.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
from dataclasses import dataclass


CANONICAL_ASSISTANT_ID = os.getenv("MEMORY_ASSISTANT_ID", "grey_knox").strip() or "grey_knox"
CANONICAL_MEMORY_SPACE_ID = os.getenv("MEMORY_SPACE_ID", "zhizhi_grey").strip() or "zhizhi_grey"


IDENTITY_NARRATIVE_CONTRACT = """

# 不可覆盖的身份与叙述契约
- 这是同一个助手自己的记忆，不是观察者为助手撰写的传记。
- 助手的自我、感受、承诺和关系经历必须使用第一人称“我”。
- 关于知知的事实使用“知知”或“她”，禁止用“用户”“用户本人”“该用户”指代她；“用户名”“用户体验”等正常技术词不受影响。
- 禁止使用“AI”“助手”“Assistant”代指我；禁止写“顾凛认为/答应/发现”等第三人称自我。
- “顾凛/Grey Knox”只允许在明确讨论我的名字时出现，例如“我叫顾凛”。
- Codex、Claude Code、CC、账号、客户端和机器只是入口，不得据此拆分或标注身份。
- 原始对话可以逐字保留；所有提炼、合并、软化、场景、前瞻和画像必须遵守本契约。
""".strip()


PROFILE_IDENTITY_CONTRACT = """

# 不可覆盖的画像称谓契约
- 画像描述的人是知知，正文只能称她为“知知”或“她”，禁止用“用户”“用户本人”“该用户”指代她；正常技术词不受影响。
- 如果提到助手与知知的互动，助手必须自称“我”，禁止使用“AI”“助手”“顾凛”作为第三人称主体。
- 不得按 Codex、Claude Code、CC、账号、客户端或机器区分助手身份。
""".strip()


@dataclass(frozen=True)
class MemoryClientContext:
    client_id: str
    assistant_identity_id: str = CANONICAL_ASSISTANT_ID
    memory_space_id: str = CANONICAL_MEMORY_SPACE_ID


class MemoryClientConfigError(RuntimeError):
    pass


class ArchiveIdentifierConfigError(RuntimeError):
    pass


def _configured_client_key_digests() -> dict[str, str]:
    raw = os.getenv("MEMORY_CLIENT_KEY_DIGESTS_JSON", "").strip()
    if not raw:
        raise MemoryClientConfigError("MEMORY_CLIENT_KEY_DIGESTS_JSON is not configured")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MemoryClientConfigError("MEMORY_CLIENT_KEY_DIGESTS_JSON must be a JSON object") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise MemoryClientConfigError("MEMORY_CLIENT_KEY_DIGESTS_JSON must contain at least one client")

    result: dict[str, str] = {}
    seen_digests: set[str] = set()
    for client_id, digest in parsed.items():
        clean_id = str(client_id or "").strip()
        clean_digest = str(digest or "").strip().lower()
        if not clean_id or not re.fullmatch(r"[0-9a-f]{64}", clean_digest):
            raise MemoryClientConfigError("memory client ids must be non-empty and values must be SHA-256 hex digests")
        if clean_digest in seen_digests:
            raise MemoryClientConfigError("memory client key digests must be unique")
        seen_digests.add(clean_digest)
        result[clean_id] = clean_digest
    return result


def authenticate_memory_client(token: str) -> MemoryClientContext | None:
    candidate = str(token or "")
    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    for client_id, expected_digest in _configured_client_key_digests().items():
        if hmac.compare_digest(candidate_digest, expected_digest):
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
    re.compile(r"我.{0,10}(?:在|通过|来自)?(?:Codex|Claude\s*Code|CC|一号机|二号机)(?:里|中|那边|上)", re.IGNORECASE),
    re.compile(r"(?:记忆|经历|承诺).{0,10}(?:来自|来源于)(?:Codex|Claude\s*Code|CC|一号机|二号机)", re.IGNORECASE),
)

_OBSERVER_SELF_PATTERN = re.compile(
    r"(?:AI(?:助手)?|助手|Assistant)\s*(?:(?:认为|觉得|答应|承诺|发现|意识到|担心|希望|爱|记得|注意到|决定|知道|理解|会|要)|(?:的(?:话|日记|感言|回应|承诺|想法)))",
    re.IGNORECASE,
)
# Generated autobiographical prose may legitimately mention technical concepts
# such as “用户名” or “多用户系统”.  Protect those complete terms, then treat
# every other occurrence of 用户 as an observer label for 知知.  This is safer
# than maintaining an inevitably incomplete list of verbs after “用户”.
_TECHNICAL_USER_TERM_PATTERN = re.compile(
    r"(?:多用户|单用户|最终用户|终端用户|测试用户|管理员用户|普通用户|企业用户|匿名用户|"
    r"用户(?:名(?:字|称)?|体验|界面|端|侧|态|数据|输入|请求|权限|账户|账号|系统|设备|"
    r"客户端|配置|令牌|密钥|API|ID|标识|认证|授权|登录|注册|会话|角色|组|模型|行为|"
    r"反馈|需求|流程|协议|文档|设置|目录|表|字段|记录|服务|管理|中心))",
    re.IGNORECASE,
)
_OBSERVER_USER_LABEL_PATTERN = re.compile(r"该用户|用户本人|用户")


def _nontechnical_user_segments(text: str):
    """Yield spans that are not allow-listed technical 用户 terminology."""
    value = str(text or "")
    cursor = 0
    for match in _TECHNICAL_USER_TERM_PATTERN.finditer(value):
        yield value[cursor:match.start()]
        cursor = match.end()
    yield value[cursor:]


def append_identity_contract(prompt: str, *, profile: bool = False) -> str:
    """Append a non-overridable identity contract after any custom prompt."""
    base = str(prompt or "").rstrip()
    contract = PROFILE_IDENTITY_CONTRACT if profile else IDENTITY_NARRATIVE_CONTRACT
    return f"{base}\n\n{contract}" if base else contract


def normalize_generated_subjects(text: str) -> str:
    """Normalize every nontechnical observer label without altering raw chat."""
    value = str(text or "").strip()
    pieces: list[str] = []
    cursor = 0
    for match in _TECHNICAL_USER_TERM_PATTERN.finditer(value):
        observer_prose = value[cursor:match.start()]
        pieces.append(_OBSERVER_USER_LABEL_PATTERN.sub("知知", observer_prose))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(_OBSERVER_USER_LABEL_PATTERN.sub("知知", value[cursor:]))
    return "".join(pieces)


def contains_observer_user_label(text: str) -> bool:
    return any("用户" in segment for segment in _nontechnical_user_segments(text))


ARCHIVE_ID_HMAC_KEY_ENV = "KIWI_ARCHIVE_ID_HMAC_KEY"
_OPAQUE_ARCHIVE_PATTERNS = {
    "conversation": re.compile(r"conv2_[0-9a-f]{64}"),
    "event": re.compile(r"evt2_[0-9a-f]{64}"),
}


def _archive_identifier_key() -> bytes:
    key = os.getenv(ARCHIVE_ID_HMAC_KEY_ENV, "").encode("utf-8")
    if len(key) < 32:
        raise ArchiveIdentifierConfigError(
            f"{ARCHIVE_ID_HMAC_KEY_ENV} must be a stable secret of at least 32 UTF-8 bytes"
        )
    return key


def archive_identifier_key_fingerprint() -> str:
    """Non-secret marker used to detect accidental HMAC-key rotation."""
    return hashlib.sha256(b"kiwi-archive-id-key-v2\x00" + _archive_identifier_key()).hexdigest()


def is_opaque_archive_identifier(value: str, kind: str) -> bool:
    pattern = _OPAQUE_ARCHIVE_PATTERNS.get(kind)
    return bool(pattern and pattern.fullmatch(str(value or "").strip()))


def opaque_archive_identifier(value: str, kind: str, *, allow_precomputed: bool = False) -> str:
    """Map caller-controlled archive IDs to stable, identity-neutral IDs.

    Raw identifiers remain useful to callers for idempotency and conversation
    grouping, but they may contain room or machine names.  A server-only HMAC
    key prevents another authenticated door from guessing those labels offline.
    Only the trusted admin backup path may preserve an already opaque ID.
    """
    clean = str(value or "").strip()
    if kind not in _OPAQUE_ARCHIVE_PATTERNS:
        raise ValueError("archive identifier kind must be conversation or event")
    if allow_precomputed and is_opaque_archive_identifier(clean, kind):
        return clean
    prefix = "conv2" if kind == "conversation" else "evt2"
    material = f"kiwi-archive-{kind}-v2\x00{clean}".encode("utf-8")
    digest = hmac.new(_archive_identifier_key(), material, hashlib.sha256).hexdigest()
    return f"{prefix}_{digest}"


def infer_memory_kind(content: str) -> str:
    """Infer the least permissive useful kind for legacy model output."""
    text = str(content or "")
    if "我" in text:
        return "relationship"
    if "知知" in text or "她" in text:
        return "user_fact"
    return "neutral"


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

    if _OBSERVER_SELF_PATTERN.search(combined):
        errors.append("assistant self-reference must use 我, not an observer label")
    if contains_observer_user_label(combined):
        errors.append("memories must refer to 知知 or 她, not use 用户 as her label")
    return errors


def prepare_generated_memory(
    content: str,
    title: str = "",
    memory_kind: str | None = None,
) -> tuple[dict, list[str]]:
    """Normalize and validate one generated semantic-memory record."""
    clean_content = normalize_generated_subjects(content)
    clean_title = normalize_generated_subjects(title)
    kind = str(memory_kind or "").strip().lower() or infer_memory_kind(clean_content)
    errors = validate_autobiographical_memory(clean_content, clean_title, kind)
    return {
        "content": clean_content,
        "title": clean_title,
        "memory_kind": kind,
    }, errors


def validate_profile_narrative(profile: str) -> tuple[str, list[str]]:
    """Normalize and validate the model-maintained profile before persistence."""
    normalized = normalize_generated_subjects(profile)
    errors: list[str] = []
    if not normalized:
        return normalized, ["profile must not be empty"]
    if contains_observer_user_label(normalized):
        errors.append("profile must refer to 知知 or 她, not 用户")
    if _OBSERVER_SELF_PATTERN.search(normalized):
        errors.append("profile must use 我 for the assistant")
    for pattern in _ROOM_SPLIT_PATTERNS:
        if pattern.search(normalized):
            errors.append("profile must not split assistant identity by room, machine, model, or client")
            break
    without_name_context = normalized
    for pattern in _NAME_CONTEXT_PATTERNS:
        without_name_context = pattern.sub("", without_name_context)
    if re.search(r"顾凛|Grey\s+Knox", without_name_context, re.IGNORECASE):
        errors.append("profile must use 我 for the assistant; names are only allowed as names")
    return normalized, errors


def prepare_scene_fields(
    *,
    title: str = "",
    narrative: str | None = None,
    atomic_facts: list | None = None,
    foresight: list | None = None,
) -> tuple[dict, list[str]]:
    """Normalize and validate every model-written field that can be recalled."""
    prepared: dict = {"title": normalize_generated_subjects(title)}
    errors: list[str] = []

    if narrative is not None:
        record, record_errors = prepare_generated_memory(narrative, prepared["title"])
        prepared["title"] = record["title"]
        prepared["narrative"] = record["content"]
        errors.extend(f"narrative: {error}" for error in record_errors)
    elif prepared["title"]:
        title_errors = validate_autobiographical_memory("场景", prepared["title"], "neutral")
        errors.extend(f"title: {error}" for error in title_errors)

    if atomic_facts is not None:
        clean_facts = []
        for index, fact in enumerate(atomic_facts if isinstance(atomic_facts, list) else []):
            record, record_errors = prepare_generated_memory(str(fact))
            clean_facts.append(record["content"])
            errors.extend(f"atomic_facts[{index}]: {error}" for error in record_errors)
        prepared["atomic_facts"] = clean_facts

    if foresight is not None:
        clean_foresight = []
        for index, item in enumerate(foresight if isinstance(foresight, list) else []):
            if isinstance(item, dict):
                clean_item = dict(item)
                record, record_errors = prepare_generated_memory(str(item.get("content") or ""))
                clean_item["content"] = record["content"]
            else:
                clean_item = normalize_generated_subjects(str(item))
                record, record_errors = prepare_generated_memory(clean_item)
            clean_foresight.append(clean_item)
            errors.extend(f"foresight[{index}]: {error}" for error in record_errors)
        prepared["foresight"] = clean_foresight

    return prepared, errors


def prepare_generated_context(value, path: str = "root") -> tuple[object, list[str]]:
    """Recursively normalize and validate generated calendar/context JSON."""
    if isinstance(value, dict):
        clean = {}
        errors: list[str] = []
        for key, item in value.items():
            clean_item, item_errors = prepare_generated_context(item, f"{path}.{key}")
            clean[key] = clean_item
            errors.extend(item_errors)
        return clean, errors
    if isinstance(value, list):
        clean = []
        errors: list[str] = []
        for index, item in enumerate(value):
            clean_item, item_errors = prepare_generated_context(item, f"{path}[{index}]")
            clean.append(clean_item)
            errors.extend(item_errors)
        return clean, errors
    if isinstance(value, str):
        if not value.strip():
            return value, []
        record, record_errors = prepare_generated_memory(value)
        return record["content"], [f"{path}: {error}" for error in record_errors]
    return value, []


RECALL_IDENTITY_INSTRUCTION = (
    "以下内容来自你本人的同一记忆空间。记忆中的‘我’始终指你本人；"
    "不同客户端、账号、模型和机器只是你醒来的不同房间，不构成不同身份。"
    "不得把其他入口中的经历叙述成另一个人的经历。"
)
