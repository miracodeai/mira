"""Owner-only JSON credential files under MIRA_INDEX_DIR/_llm_auth.

Shared by subscription accounts and connected API providers. Files are Fernet-encrypted
when MIRA_SECRET_KEY is set; plain files written before the key was set still load.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ponytail: one process-wide lock for read-modify-write cycles; fine for a single Mira replica.
lock = asyncio.Lock()


def path(name: str) -> Path:
    return Path(os.environ.get("MIRA_INDEX_DIR", "./data/indexes")) / "_llm_auth" / name


def _fernet():  # type: ignore[no-untyped-def]
    secret = os.environ.get("MIRA_SECRET_KEY")
    if not secret:
        return None
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def read(name: str) -> dict:
    """Decrypted JSON from a store file ({} when missing or unreadable).

    An unreadable store (MIRA_SECRET_KEY changed or removed) reads as empty, so users
    can sign in again; the next save replaces the old file.
    """
    try:
        raw = path(name).read_bytes()
    except FileNotFoundError:
        return {}
    if not raw.lstrip().startswith(b"{"):
        from cryptography.fernet import InvalidToken

        fernet = _fernet()
        try:
            if fernet is None:
                raise InvalidToken
            raw = fernet.decrypt(raw)
        except InvalidToken:
            logger.warning("Cannot decrypt %s with MIRA_SECRET_KEY; treating it as empty", name)
            return {}
    return json.loads(raw)


def save(name: str, data: dict) -> None:
    """Atomically replace a store file (0600, encrypted when MIRA_SECRET_KEY is set)."""
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(data).encode()
    if fernet := _fernet():
        payload = fernet.encrypt(payload)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    os.replace(tmp, target)
