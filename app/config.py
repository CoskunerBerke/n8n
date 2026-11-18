from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
CREDENTIALS_DIR = BASE_DIR / "credentials"

SECRET_FIELDS = {
    "gemini_api_key",
    "openai_api_key",
    "twitch_client_secret",
    "kick_client_secret",
    "youtube_data_api_key",
    "telegram_bot_token",
    "instagram_global_access_token",
    "instagram_tr_access_token",
}


class Settings(BaseSettings):
    """Application settings loaded from .env plus runtime JSON overrides."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = ""
    internal_scheduler_enabled: bool = True
    scan_interval_minutes: int = 10
    live_check_interval_seconds: int = 60
    max_concurrent_live_streams: int = 4

    ai_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    whisper_enabled: bool = True
    whisper_model: str = "base"

    min_viral_score: float = 7.0
    clip_min_seconds: int = 28
    clip_max_seconds: int = 45
    min_speech_words: int = 8
    min_non_silent_ratio: float = 0.25
    max_daily_youtube_per_pipeline: int = 3
    min_publish_interval_minutes: int = 120

    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    kick_client_id: str = ""
    kick_client_secret: str = ""
    youtube_data_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""

    n8n_webhook_url: str = ""

    youtube_global_client_secrets: str = "credentials/youtube_client_secret.json"
    instagram_global_user_id: str = ""
    instagram_global_access_token: str = ""

    youtube_tr_client_secrets: str = "credentials/youtube_client_secret.json"
    instagram_tr_user_id: str = ""
    instagram_tr_access_token: str = ""

    temp_public_upload_enabled: bool = False
    data_dir_override: str = ""

    @property
    def data_dir(self) -> Path:
        raw = self.data_dir_override or os.environ.get("CLIPPER_DATA_DIR", "")
        return Path(raw) if raw else (BASE_DIR / "data")

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def rendered_dir(self) -> Path:
        return self.data_dir / "rendered"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def live_buffer_dir(self) -> Path:
        return self.data_dir / "live_buffer"

    @property
    def runtime_settings_path(self) -> Path:
        return self.data_dir / "runtime_settings.json"


def ensure_directories(settings: Settings) -> None:
    """Create required runtime directories if missing."""
    for folder in (
        settings.data_dir,
        settings.raw_dir,
        settings.rendered_dir,
        settings.work_dir,
        settings.live_buffer_dir,
        CREDENTIALS_DIR,
    ):
        folder.mkdir(parents=True, exist_ok=True)


def _load_runtime_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return Settings merged with data/runtime_settings.json."""
    base = Settings()
    ensure_directories(base)
    overrides = _load_runtime_overrides(base.runtime_settings_path)
    if overrides:
        valid_fields = set(Settings.model_fields.keys())
        filtered = {k: v for k, v in overrides.items() if k in valid_fields}
        base = base.model_copy(update=filtered)
        ensure_directories(base)
    return base


def reload_settings() -> Settings:
    """Clear settings cache and reload from .env and runtime overrides."""
    get_settings.cache_clear()
    return get_settings()


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 6)}{value[-4:]}"


def get_public_settings_dict() -> dict[str, Any]:
    """Return settings dict safe for frontend consumption with secrets masked."""
    settings = get_settings()
    data: dict[str, Any] = {}
    for field_name in Settings.model_fields:
        if field_name == "data_dir_override":
            continue
        val = getattr(settings, field_name)
        if field_name in SECRET_FIELDS:
            data[field_name] = _mask_secret(str(val))
            data[f"{field_name}_set"] = bool(val)
        else:
            data[field_name] = val
    data["youtube_global_connected"] = (CREDENTIALS_DIR / "youtube_global_token.json").exists()
    data["youtube_tr_connected"] = (CREDENTIALS_DIR / "youtube_tr_token.json").exists()
    data["youtube_client_secrets_exists"] = resolve_path(settings.youtube_global_client_secrets).exists()
    return data


def update_runtime_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Persist runtime setting updates from the Web/Mobile setup wizard."""
    settings = get_settings()
    existing = _load_runtime_overrides(settings.runtime_settings_path)
    valid_fields = set(Settings.model_fields.keys()) - {"data_dir_override"}

    for key, val in patch.items():
        if key not in valid_fields:
            continue
        if key in SECRET_FIELDS:
            if val is None:
                continue
            sval = str(val).strip()
            # Ignore masked placeholder strings returned unchanged by UI inputs.
            if sval and "*" in sval and sval == _mask_secret(str(getattr(settings, key))):
                continue
            existing[key] = sval
        else:
            existing[key] = val

    settings.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runtime_settings_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    reload_settings()
    return get_public_settings_dict()


def resolve_path(raw_path: str) -> Path:
    """Resolve relative paths against project root."""
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return BASE_DIR / candidate
