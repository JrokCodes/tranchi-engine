"""
Tranchi Engine — Application Settings
Loaded from environment variables / .env file via pydantic-settings.
No hardcoded secrets — all sensitive values must be set in environment.
"""
from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── Auth ─────────────────────────────────────────────────────────────────
    JWT_SECRET: str = ""
    JWT_ACCESS_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_EXPIRE_DAYS: int = 7
    DEV_USER_EMAIL: str = ""

    # ── CORS ─────────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # ── External APIs ─────────────────────────────────────────────────────────
    # Google Maps Static API key (Street View Static API). Used to build
    # deterministic property-image URLs from the address at response time — no
    # storage, no geocoding. Empty key → street_view_url returns None and the UI
    # shows a placeholder. The key ends up embedded in image URLs visible to the
    # browser, so it MUST be HTTP-referrer-restricted to the tranchi hostname in
    # the Google Cloud console.
    GOOGLE_MAPS_API_KEY: str = ""

    # ── Feature flags ─────────────────────────────────────────────────────────
    DEBUG: bool = False

    # ─────────────────────────────────────────────────────────────────────────
    # Validators
    # ─────────────────────────────────────────────────────────────────────────

    @field_validator("DATABASE_URL")
    @classmethod
    def database_url_must_be_set(cls, v: str) -> str:
        if not v or v.strip() == "":
            raise ValueError("DATABASE_URL must be set in .env")
        return v

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
