"""Application configuration, loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Service ---
    app_name: str = "LinkedIn Profile API"
    version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # --- API auth ---
    # Comma-separated in the environment; parsed by `api_key_set`.
    api_keys: str = ""

    # --- Provider selection ---
    provider: Literal[
        "voyager", "public", "embedded", "linkedin_scraper", "proxycurl"
    ] = "voyager"

    # --- LinkedIn credentials ---
    linkedin_li_at: SecretStr | None = None
    linkedin_email: str | None = None
    linkedin_password: SecretStr | None = None
    session_state_path: str = ".session/storage_state.json"
    cookie_store_path: str = ".session/cookies.json"

    # --- Proxy ---
    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: SecretStr | None = None

    # --- Proxycurl ---
    proxycurl_api_key: SecretStr | None = None

    # --- Browser tuning ---
    headless: bool = True
    nav_timeout_ms: int = 45_000
    page_settle_ms: int = 1_200
    fetch_detail_pages: bool = True
    max_concurrent_scrapes: int = Field(default=1, ge=1, le=8)
    scrape_min_interval_seconds: float = Field(default=6.0, ge=0)

    # --- Cache / rate limit ---
    cache_ttl_seconds: int = Field(default=86_400, ge=0)
    cache_max_entries: int = Field(default=500, ge=1)
    rate_limit_requests: int = Field(default=30, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key_set)

    @property
    def has_li_at(self) -> bool:
        """The Voyager API authenticates with the session cookie only."""
        return bool(self.linkedin_li_at and self.linkedin_li_at.get_secret_value().strip())

    @property
    def has_login_credentials(self) -> bool:
        """Enough to mint a fresh session without human help."""
        return bool(
            self.linkedin_email
            and self.linkedin_password
            and self.linkedin_password.get_secret_value().strip()
        )

    @property
    def has_linkedin_credentials(self) -> bool:
        return self.has_li_at or self.has_login_credentials

    @property
    def playwright_proxy(self) -> dict[str, str] | None:
        if not self.proxy_server:
            return None
        proxy: dict[str, str] = {"server": self.proxy_server}
        if self.proxy_username:
            proxy["username"] = self.proxy_username
        if self.proxy_password:
            proxy["password"] = self.proxy_password.get_secret_value()
        return proxy


@lru_cache
def get_settings() -> Settings:
    return Settings()
