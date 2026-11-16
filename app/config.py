"""Configuration lue dans les variables d'environnement."""

import os
from dataclasses import dataclass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "oui"}


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    model: str
    database_url: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_from: str
    public_url: str
    validate_twilio: bool
    timezone: str = "Europe/Paris"
    # Services connectes. Sans ces valeurs, la connexion Google est desactivee.
    google_client_id: str = ""
    google_client_secret: str = ""
    secret_key: str = ""
    ingest_model: str = "claude-haiku-5-5"
    sync_interval_seconds: int = 300

    @property
    def connections_enabled(self) -> bool:
        return bool(
            self.google_client_id and self.google_client_secret and self.secret_key and self.public_url
        )


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        model=os.getenv("MUMU_MODEL", "claude-sonnet-5-5"),
        database_url=os.getenv("DATABASE_URL") or "sqlite:///mumu.db",
        twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
        twilio_whatsapp_from=os.getenv("TWILIO_WHATSAPP_FROM", ""),
        public_url=os.getenv("PUBLIC_URL", "").rstrip("/"),
        validate_twilio=_bool(os.getenv("VALIDATE_TWILIO"), True),
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        secret_key=os.getenv("MUMU_SECRET_KEY", ""),
        ingest_model=os.getenv("MUMU_INGEST_MODEL", "claude-haiku-5-5"),
        sync_interval_seconds=int(os.getenv("SYNC_INTERVAL_SECONDS") or 300),
    )
