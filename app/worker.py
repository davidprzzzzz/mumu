"""Travail d'arriere-plan dans un processus separe : `python -m app.worker`.

Utile quand le serveur web tourne en plusieurs exemplaires (lancer alors le
serveur avec RUN_BACKGROUND=false). En beta, le serveur web fait deja ce
travail lui-meme.
"""

import logging
import time

from .brain import ClaudeBrain
from .config import load_settings
from .connectors.google import GoogleClient
from .main import GOOGLE_CALLBACK_PATH
from .models import make_session_factory
from .proactive import tick
from .retention import purge_old_messages
from .sync import sync_all
from .whatsapp import TwilioSender

log = logging.getLogger("mumu.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    sessions = make_session_factory(settings.database_url)
    brain = ClaudeBrain(settings.anthropic_api_key, settings.model, settings.ingest_model)
    sender = TwilioSender(settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_whatsapp_from)
    google = None
    if settings.connections_enabled:
        google = GoogleClient(
            settings.google_client_id, settings.google_client_secret, settings.public_url + GOOGLE_CALLBACK_PATH
        )
    while True:
        if google is not None:
            sync_all(sessions, google, brain, sender, settings.secret_key, settings.timezone)
        try:
            tick(sessions, sender, settings.timezone)
            purge_old_messages(sessions, settings.message_retention_days)
        except Exception:
            log.exception("Passage d'arrière-plan impossible")
        time.sleep(settings.sync_interval_seconds)


if __name__ == "__main__":
    main()
