"""Runtime configuration, loaded from the environment and `.env`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_DATA_DIR = Path.home() / ".telegram-mcp"


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

    api_id: int = Field(validation_alias=AliasChoices("TELEGRAM_API_ID", "api_id"))
    api_hash: SecretStr = Field(validation_alias=AliasChoices("TELEGRAM_API_HASH", "api_hash"))

    data_dir: Path = DEFAULT_DATA_DIR
    default_account: str = "main"
    read_only: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Chat ids (as printed by list_chats) that Claude must never see and that are
    # never archived, e.g. "-1001234567890,987654321". Telegram's own service
    # chats are always hidden on top of this, see policy.py.
    hidden_chats: Annotated[list[int], NoDecode] = []

    # Archive on the core server. Both empty: archive tools and sync are disabled.
    core_url: str = ""
    core_token: SecretStr = SecretStr("")
    # Messages taken per chat the first time it is archived; older history is
    # fetched only on request (`telegram-mcp sync --backfill`).
    sync_initial_limit: int = Field(default=300, ge=1, le=5000)

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

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def archive_enabled(self) -> bool:
        return bool(self.core_url and self.core_token.get_secret_value())
