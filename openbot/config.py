"""Application config — single source of truth for env vars.

PRD §7: `setup.sh` wizard writes `.env`; this module loads it.
Secrets are wrapped in `SecretStr` so they never leak into logs or repr.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="OPENBOT_",
        extra="ignore",
        frozen=True,
    )

    # GitHub App webhook (PRD §5.1: verify signature is the trust boundary)
    github_webhook_secret: SecretStr | None = Field(
        default=None,
        description=(
            "HMAC-SHA-256 secret set when creating the GitHub App. "
            "If unset, /webhook/github responds 503 — webhooks are intentionally "
            "off until the wizard has run."
        ),
    )

    debug: bool = Field(default=False, description="Verbose logs; never enable in production.")


@lru_cache
def get_settings() -> Settings:
    """Cached accessor. Tests call `get_settings.cache_clear()` after monkeypatching env."""
    return Settings()
