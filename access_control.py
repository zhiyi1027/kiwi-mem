"""Authentication for Kiwi-Mem's writable/read-sensitive control plane.

Only SHA-256 digests are configured on the server.  Browsers and other
operators present the original secret as a Bearer token; the original is kept
in sessionStorage by the admin UI and is never persisted by Kiwi-Mem.

The mounted MCP server calls a few control-plane endpoints over loopback.  It
uses a process-local random capability so those calls keep working without
putting the operator's original secret in the server environment.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets


ADMIN_DIGEST_ENV = "KIWI_ADMIN_TOKEN_SHA256"
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_INTERNAL_CONTROL_TOKEN = secrets.token_urlsafe(48)


class AdminTokenConfigError(RuntimeError):
    pass


def sha256_digest(secret: str) -> str:
    return hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()


def _configured_admin_digest() -> str:
    digest = os.getenv(ADMIN_DIGEST_ENV, "").strip()
    if not digest:
        raise AdminTokenConfigError(f"{ADMIN_DIGEST_ENV} is not configured")
    if not _SHA256_PATTERN.fullmatch(digest):
        raise AdminTokenConfigError(f"{ADMIN_DIGEST_ENV} must be a 64-character SHA-256 hex digest")
    return digest.lower()


def authenticate_admin_token(token: str) -> bool:
    candidate = str(token or "")
    if not candidate:
        return False
    return hmac.compare_digest(sha256_digest(candidate), _configured_admin_digest())


def internal_control_headers() -> dict[str, str]:
    """Headers for same-process MCP -> loopback control-plane calls."""
    return {"X-Kiwi-Internal-Control": _INTERNAL_CONTROL_TOKEN}


def authenticate_internal_control(token: str) -> bool:
    candidate = str(token or "")
    return bool(candidate) and hmac.compare_digest(candidate, _INTERNAL_CONTROL_TOKEN)


def bearer_token(authorization: str) -> str:
    scheme, separator, token = str(authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


_CONTROL_PREFIXES = (
    "/debug",
    "/sync",
    "/dream",
    "/comments",
    "/reminders",
    "/search/messages",
    "/projects",
)
_PUBLIC_ADMIN_PATHS = ("/admin", "/admin/")
_PUBLIC_ADMIN_ASSET_PREFIXES = ("/admin/css/", "/admin/js/", "/admin/assets/")


def is_control_plane_path(path: str) -> bool:
    """Return whether a request must carry an admin or internal capability.

    The HTML/JS/CSS shell is public but contains no data.  Every dynamic route
    used by that shell is protected.  Mounted MCP transport paths remain on
    their own boundary; their internal loopback calls carry a process-local
    capability.
    """
    clean = str(path or "")
    if clean in _PUBLIC_ADMIN_PATHS or clean.startswith(_PUBLIC_ADMIN_ASSET_PREFIXES):
        return False
    if clean.startswith("/admin/"):
        return True
    if clean == "/calendar" or (clean.startswith("/calendar/") and not clean.startswith("/calendar/mcp")):
        return True
    return any(clean == prefix or clean.startswith(prefix + "/") for prefix in _CONTROL_PREFIXES)
