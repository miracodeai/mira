"""Tests for Origin App private key loading."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mira.platforms.origin.auth import _resolve_private_key


@pytest.fixture
def pem() -> str:
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_raw_pem_passthrough(pem: str) -> None:
    assert _resolve_private_key(pem) == pem


def test_at_path_reads_file(tmp_path, pem: str) -> None:
    key_file = tmp_path / "key.pem"
    key_file.write_text(pem)
    assert _resolve_private_key(f"@{key_file}") == pem


def test_missing_file_raises_clear_error(tmp_path) -> None:
    missing = tmp_path / "nope.pem"
    with pytest.raises(ValueError, match="not found"):
        _resolve_private_key(f"@{missing}")


def test_non_pem_file_rejected(tmp_path) -> None:
    other = tmp_path / "passwd"
    other.write_text("root:x:0:0:root:/root:/bin/bash\n")
    with pytest.raises(ValueError, match="PEM private key"):
        _resolve_private_key(f"@{other}")


def test_non_pem_raw_value_rejected() -> None:
    with pytest.raises(ValueError, match="PEM private key"):
        _resolve_private_key("not-a-key")
