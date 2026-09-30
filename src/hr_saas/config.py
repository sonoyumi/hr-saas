"""Settings from .env in the current folder. Every mistake is reported at once."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

AI_MODES = ("off", "mock", "claude")


class ConfigError(Exception):
    """Wrong or missing settings; the message lists every problem."""


@dataclass(frozen=True)
class Settings:
    secret_key: str = field(repr=False)
    master_key: str = field(repr=False)
    public_url: str = "http://127.0.0.1:8098"
    host: str = "127.0.0.1"
    port: int = 8098
    cookie_secure: bool = False
    database_path: Path = Path("data/hr.db")
    ai_mode: str = "off"
    anthropic_api_key: str | None = field(default=None, repr=False)
    claude_model: str = "claude-opus-5-5"
    platform_name: str = "HR Screening"


def _int(name: str, default: int, errors: list[str]) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{name}: нужно целое число, получено {raw!r}")
        return default


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    if env_file is not None:
        load_dotenv(env_file)
    errors: list[str] = []
    secret = os.getenv("SECRET_KEY", "").strip()
    if len(secret) < 32:
        errors.append("SECRET_KEY: случайная строка не короче 32 символов "
                      "(python3 -c \"import secrets; print(secrets.token_hex(32))\")")
    master = os.getenv("MASTER_KEY", "").strip()
    if len(master) != 44:
        errors.append("MASTER_KEY: ключ шифрования токенов ботов (hr-saas gen-key)")
    public_url = (os.getenv("PUBLIC_URL", "").strip() or "http://127.0.0.1:8098").rstrip("/")
    if not public_url.startswith(("http://", "https://")):
        errors.append("PUBLIC_URL: нужен адрес вида https://hr.example.com")
    ai_mode = (os.getenv("AI_MODE", "").strip() or "off").lower()
    key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
    if ai_mode not in AI_MODES:
        errors.append(f"AI_MODE: одно из {', '.join(AI_MODES)}, получено {ai_mode!r}")
    settings = Settings(
        secret_key=secret,
        master_key=master,
        public_url=public_url,
        host=os.getenv("HOST", "").strip() or "127.0.0.1",
        port=_int("PORT", 8098, errors),
        cookie_secure=os.getenv("COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"},
        database_path=Path(os.getenv("DATABASE_PATH", "").strip() or "data/hr.db"),
        ai_mode=ai_mode,
        anthropic_api_key=key,
        claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-opus-5-5",
        platform_name=os.getenv("PLATFORM_NAME", "").strip() or "HR Screening",
    )
    if errors:
        raise ConfigError("Ошибки в .env:\n  - " + "\n  - ".join(errors))
    return settings
