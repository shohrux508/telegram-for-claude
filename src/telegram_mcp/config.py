"""Runtime configuration.

Sources, strongest first: environment variables, `.env` files, `settings.json`
in the data folder (written by the settings page), then built-in defaults.
People who use the settings page never see the first two; a developer's `.env`
still wins over whatever the page saved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_DATA_DIR = Path.home() / ".telegram-mcp"
SETTINGS_FILE_NAME = "settings.json"

ArchiveMode = Literal["off", "local", "core"]


def data_dir_from_env() -> Path:
    """Where settings.json lives. Only the environment can move it, not the file itself."""
    return Path(os.environ.get("TELEGRAM_MCP_DATA_DIR") or DEFAULT_DATA_DIR).expanduser()


def settings_file() -> Path:
    return data_dir_from_env() / SETTINGS_FILE_NAME


def embedded_api() -> tuple[int, str] | None:
    """The app's own Telegram API ID, baked into release builds by scripts/build.ps1."""
    try:
        from telegram_mcp import _embedded  # type: ignore[attr-defined]
    except ImportError:
        return None
    api_id, api_hash = int(_embedded.API_ID), str(_embedded.API_HASH)
    return (api_id, api_hash) if api_id and api_hash else None


class Settings(BaseSettings):
    """Everything the server needs to know before talking to Telegram."""

    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_MCP_",
        # Later files win: a project-local `.env` overrides the one in the data dir.
        env_file=(DEFAULT_DATA_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # 0 / empty means "use the app's built-in one" (see embedded_api).
    api_id: int = Field(default=0, validation_alias=AliasChoices("TELEGRAM_API_ID", "api_id"))
    api_hash: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("TELEGRAM_API_HASH", "api_hash")
    )

    data_dir: Path = DEFAULT_DATA_DIR
    default_account: str = "main"
    read_only: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Chat ids (as printed by list_chats) that Claude must never see and that are
    # never archived, e.g. "-1001234567890,987654321". Telegram's own service
    # chats are always hidden on top of this, see policy.py.
    hidden_chats: Annotated[list[int], NoDecode] = []

    # Where the history archive lives: nowhere, in a file on this computer, or on
    # the core server. When unset: "core" if core_url and core_token are given,
    # otherwise "local".
    archive_mode: ArchiveMode | None = None
    core_url: str = ""
    core_token: SecretStr = SecretStr("")
    # Messages taken per chat the first time it is archived; older history is
    # fetched only on request (`telegram-mcp sync --backfill`).
    sync_initial_limit: int = Field(default=300, ge=1, le=5000)
    # While Claude has the server running, keep the archive up to date on its own.
    auto_sync: bool = True
    sync_interval_minutes: int = Field(default=10, ge=1, le=1440)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            JsonConfigSettingsSource(settings_cls, json_file=settings_file()),
        )

    @field_validator("data_dir")
    @classmethod
    def _expand_data_dir(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("hidden_chats", mode="before")
    @classmethod
    def _split_hidden_chats(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        return value

    @field_validator("core_url")
    @classmethod
    def _normalize_core_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _fill_defaults(self) -> Settings:
        if not (self.api_id and self.api_hash.get_secret_value()):
            built_in = embedded_api()
            if built_in is not None:
                self.api_id, self.api_hash = built_in[0], SecretStr(built_in[1])
        if self.archive_mode is None:
            has_core = bool(self.core_url and self.core_token.get_secret_value())
            self.archive_mode = "core" if has_core else "local"
        return self

    @property
    def api_configured(self) -> bool:
        return bool(self.api_id and self.api_hash.get_secret_value())

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def archive_db_path(self) -> Path:
        return self.data_dir / "archive.db"

    @property
    def archive_enabled(self) -> bool:
        if self.archive_mode == "core":
            return bool(self.core_url and self.core_token.get_secret_value())
        return self.archive_mode == "local"


# Keys the settings page may write. Anything else in the file is left alone.
EDITABLE_KEYS = frozenset(
    {
        "api_id",
        "api_hash",
        "read_only",
        "hidden_chats",
        "archive_mode",
        "core_url",
        "core_token",
        "auto_sync",
        "sync_interval_minutes",
        "sync_initial_limit",
    }
)


def read_settings_file() -> dict[str, Any]:
    path = settings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def write_settings_file(changes: dict[str, Any]) -> None:
    """Merge `changes` into settings.json atomically, so a crash never leaves half a file."""
    unknown = set(changes) - EDITABLE_KEYS
    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}")
    data = {**read_settings_file(), **changes}
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
