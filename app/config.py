from functools import lru_cache

from pydantic import SecretStr, field_validator
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
    # web | worker | all (scheduler inside the web process: one container instead of two)
    role: str = "web"
    # every search interval is multiplied by this; 1 = as configured, 4 = a quiet, dormant site
    scrape_interval_factor: float = 1.0
    # dormant: no foreign scraping, NL comparables at most weekly, no translation / geocoding /
    # page fetching / digest; flip back to resume (every search is then overdue and reruns)
    dormant: bool = False
    # phase 6: Claude pass over descriptions; off when the key is empty
    anthropic_api_key: SecretStr = SecretStr("")
    claude_model: str = "claude-opus-5-5"
    insights_per_run: int = 40
    insights_photos: int = 4  # photos sent with each Claude call (0 = text only)
    # where the cars are collected from (for distance, fuel and tolls on each car)
    base_city: str = "Utrecht"
    base_postcode: str = ""
    base_country: str = "NL"
    # spread gating: only fetch detail pages / run Claude for cars at or above this spread
    # (cars without an NL value yet always qualify). Empty = no gating.
    detail_min_spread_eur: int | None = 0
    insights_min_spread_eur: int | None = 0
    # machine translation of descriptions: google (keyless, default) | deepl | off
    translate_provider: str = "google"
    deepl_api_key: SecretStr = SecretStr("")
    translate_per_run: int = 6
    translate_auto: bool = False  # background translation of every car; off = on demand only
    sentry_dsn: str = ""  # error reports with stack traces (optional)
    # web push (docs/notifications.md): generate with `carscan push keygen`
    vapid_public_key: str = ""
    vapid_private_key: SecretStr = SecretStr("")
    vapid_contact: str = ""  # an email address the push services may contact
    # nightly database dumps (docs/backups.md); the bucket is optional
    backup_dir: str = "/srv/backups"
    backup_s3_bucket: str = ""
    backup_s3_endpoint: str = ""
    backup_s3_region: str = ""
    backup_s3_access_key: str = ""
    backup_s3_secret_key: SecretStr = SecretStr("")
    backup_s3_prefix: str = "carscan"  # a trickle every 2 min: the keyless endpoint blocks bursts

    @field_validator("database_url")
    @classmethod
    def _psycopg_scheme(cls, v: str) -> str:
        """Managed Postgres hands out postgres:// URLs; SQLAlchemy needs the driver name."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value() and self.telegram_chat_id)

    @property
    def is_production(self) -> bool:
        return self.domain not in ("localhost", "127.0.0.1")

    def check_production_secrets(self) -> list[str]:
        """Placeholder values that must not reach a public deployment."""
        problems: list[str] = []
        if self.is_production:
            if self.secret_key.get_secret_value() in ("", "change-me", "devsecret"):
                problems.append(
                    "SECRET_KEY is a placeholder; generate one with: openssl rand -hex 32"
                )
            if "change-me" in self.database_url:
                problems.append("DATABASE_URL still contains the placeholder password")
        return problems

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
