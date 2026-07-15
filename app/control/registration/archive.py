"""Encrypted storage and export helpers for browser-registration account profiles."""
from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.platform.paths import data_path

ARCHIVE_EXT_KEY = "registration_archive"
ARCHIVE_VERSION = 1
_KEY_ENV = "REGISTRATION_ARCHIVE_KEY"
_KEY_FILE = "archive.key"


def _key_path() -> Path:
    path = data_path("registration", _KEY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_key(raw: bytes) -> bytes:
    Fernet(raw)
    return raw


def _load_or_create_key() -> bytes:
    configured = os.getenv(_KEY_ENV, "").strip()
    if configured:
        return _validate_key(configured.encode("ascii"))

    path = _key_path()
    try:
        return _validate_key(path.read_bytes().strip())
    except FileNotFoundError:
        pass

    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _validate_key(path.read_bytes().strip())
    with os.fdopen(fd, "wb") as handle:
        handle.write(key + b"\n")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Encrypt sensitive registration data before it is written into ``ext``."""
    raw = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "version": ARCHIVE_VERSION,
        "ciphertext": _fernet().encrypt(raw).decode("ascii"),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def decrypt_profile(ext: dict[str, Any]) -> dict[str, Any] | None:
    archive = ext.get(ARCHIVE_EXT_KEY) if isinstance(ext, dict) else None
    if not isinstance(archive, dict) or archive.get("version") != ARCHIVE_VERSION:
        return None
    ciphertext = archive.get("ciphertext")
    if not isinstance(ciphertext, str) or not ciphertext:
        return None
    try:
        value = json.loads(_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def build_export_record(token: str, ext: dict[str, Any], *, include_password: bool = False) -> dict[str, Any] | None:
    """Return a screenshot-compatible Grok Build profile without exposing the SSO."""
    profile = decrypt_profile(ext)
    if not profile:
        return None
    oauth = profile.get("oauth") if isinstance(profile.get("oauth"), dict) else {}
    email = str(profile.get("email") or oauth.get("email") or "")
    result: dict[str, Any] = {
        "provider": str(profile.get("provider") or "grok_build"),
        "name": email,
        "client_id": str(oauth.get("client_id") or ""),
        "access_token": str(oauth.get("access_token") or ""),
        "refresh_token": str(oauth.get("refresh_token") or ""),
        "id_token": str(oauth.get("id_token") or ""),
        "token_type": str(oauth.get("token_type") or "Bearer"),
        "scope": str(oauth.get("scope") or ""),
        "expires_at": str(oauth.get("expires_at") or ""),
        "expires_in": oauth.get("expires_in", 0),
        "email": email,
        "user_id": str(oauth.get("user_id") or ""),
        "principal_id": str(oauth.get("principal_id") or ""),
        "team_id": str(oauth.get("team_id") or ""),
    }
    if include_password:
        result["password"] = str(profile.get("password") or "")
    return result


__all__ = ["ARCHIVE_EXT_KEY", "build_export_record", "decrypt_profile", "encrypt_profile"]
