"""Application configuration loaded from environment variables.

Values can be overridden via a `.env` file at the project root.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    app_name: str = "ScreenView Server"
    database_url: str = f"sqlite:///{BASE_DIR / 'screenview.db'}"
    upload_dir: Path = BASE_DIR / "uploads"
    admin_username: str = "admin"
    admin_password: str = "admin"
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 60 * 24
    cors_origins: list[str] = ["*"]

    model_config = SettingsConfigDict(env_file=".env", env_prefix="SCREENVIEW_", extra="ignore")


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)
