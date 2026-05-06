"""
core/security.py
Symmetric encryption vault for sensitive .env values (API keys, bot tokens).

Underlying primitive: Fernet (AES-128-CBC + HMAC-SHA256, RFC-recommended).
Naming kept as "AES-256" colloquially in the spec — the actual scheme is
authenticated encryption that prevents tampering.

Key resolution order:
  1. $WHALE_VAULT_KEY environment variable (for ops/CI/KMS injection)
  2. Persistent key file in the user config directory:
       Linux/macOS  : $XDG_CONFIG_HOME/whale-hunter/vault.key
                       (default ~/.config/whale-hunter/vault.key)
       Windows      : %APPDATA%\\whale-hunter\\vault.key
     Auto-created with 0600 permissions on POSIX.

Legacy migration: if the new dir has no key but the old `antigravity/vault.key`
still exists, we read it transparently so existing encrypted .env files keep
working without manual re-keying.

Plaintext values are encrypted to "ENC:<token>" strings. Vault.decrypt() is a
no-op for non-prefixed values, so old plaintext .env files keep working.
On decryption failure we return an EMPTY STRING — never the raw "ENC:..." —
so the prefix can never leak into a Telegram URL or other downstream caller.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet


_PREFIX = "ENC:"
_LEGACY_DIR_NAME = "antigravity"
_DIR_NAME = "whale-hunter"

log = logging.getLogger("whale_hunter.security")


def _user_config_base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def _user_config_dir() -> Path:
    return _user_config_base() / _DIR_NAME


def _legacy_config_dir() -> Path:
    return _user_config_base() / _LEGACY_DIR_NAME


def _load_or_create_key() -> bytes:
    """Resolve the vault key. Persists a new one on first run.

    Migration: if a legacy ~/.config/antigravity/vault.key exists and the
    new whale-hunter dir has no key yet, copy the bytes over so existing
    encrypted .env values stay decryptable.
    """
    env_key = os.environ.get("WHALE_VAULT_KEY")
    if env_key:
        return env_key.encode() if isinstance(env_key, str) else env_key

    config_dir = _user_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    key_file = config_dir / "vault.key"

    if key_file.exists():
        return key_file.read_bytes().strip()

    legacy_key = _legacy_config_dir() / "vault.key"
    if legacy_key.exists():
        try:
            data = legacy_key.read_bytes().strip()
            key_file.write_bytes(data)
            if os.name != "nt":
                try:
                    os.chmod(key_file, 0o600)
                except OSError:
                    pass
            return data
        except OSError:
            pass

    new_key = Fernet.generate_key()
    key_file.write_bytes(new_key)
    if os.name != "nt":
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    return new_key


class Vault:
    """Process-wide symmetric vault. Key is loaded once and cached."""

    _key: Optional[bytes] = None
    _fernet: Optional[Fernet] = None

    @classmethod
    def _get_fernet(cls) -> Fernet:
        if cls._fernet is None:
            cls._key = _load_or_create_key()
            cls._fernet = Fernet(cls._key)
        return cls._fernet

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        if not plaintext or plaintext.startswith(_PREFIX):
            return plaintext
        return _PREFIX + cls._get_fernet().encrypt(plaintext.encode()).decode()

    @classmethod
    def decrypt(cls, cipher_text: str) -> str:
        """Return the plaintext for an `ENC:`-prefixed value.

        - Non-prefixed values pass through unchanged (back-compat for
          plaintext .env files).
        - On decryption failure (stale key, corrupted ciphertext) we
          return an empty string and log a warning. We never return the
          raw `ENC:` value, because callers (Telegram URL builder,
          WSS connector) would otherwise splice the prefix into a
          downstream request and produce confusing 404s.
        """
        if not cipher_text:
            return cipher_text
        if not cipher_text.startswith(_PREFIX):
            return cipher_text
        try:
            return cls._get_fernet().decrypt(cipher_text[len(_PREFIX):].encode()).decode()
        except Exception as exc:
            log.warning(
                "Vault decrypt failed (%s). Treating value as empty — re-run "
                "`--setup` to re-encrypt with the current vault key.",
                type(exc).__name__,
            )
            return ""
