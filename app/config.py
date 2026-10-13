from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    secret_key: SecretStr
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    telegram_admin_chat_id: str = ""
    domain: str = "localhost"
    log_level: str = "INFO"
    scrape_interval_minutes: int = 30
    # phase 6: Claude pass over descriptions; off when the key is empty
    anthropic_api_key: SecretStr = SecretStr("")
    claude_model: str = "claude-opus-5-5"
    insights_per_run: int = 40

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value() and self.telegram_chat_id)

    @property
    def insights_enabled(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())

    @property
    def site_url(self) -> str:
        if self.domain in ("localhost", "127.0.0.1"):
            return "http://localhost:8000"
        return f"https://{self.domain}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
