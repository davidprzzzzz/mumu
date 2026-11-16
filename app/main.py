"""Serveur web de MUMU : recoit les messages WhatsApp envoyes par Twilio."""

import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from sqlalchemy.orm import sessionmaker

from .brain import Brain, ClaudeBrain
from .config import Settings, load_settings
from .models import make_session_factory
from .service import handle_incoming
from .whatsapp import Sender, TwilioSender, download_media, signature_is_valid

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mumu")

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
WEBHOOK_PATH = "/webhook/whatsapp"


def create_app(
    settings: Settings | None = None,
    brain: Brain | None = None,
    sender: Sender | None = None,
    session_factory: sessionmaker | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    session_factory = session_factory or make_session_factory(settings.database_url)
    brain = brain or ClaudeBrain(settings.anthropic_api_key, settings.model)
    sender = sender or TwilioSender(
        settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_whatsapp_from
    )

    app = FastAPI(title="MUMU")

    def process(address: str, text: str, media_url: str | None, media_type: str | None) -> None:
        try:
            media = None
            if media_url and media_type:
                media = download_media(
                    media_url, media_type, settings.twilio_account_sid, settings.twilio_auth_token
                )
            reply = handle_incoming(
                session_factory, brain, address, text, media, settings.timezone
            )
            sender.send(address, reply)
        except Exception:
            log.exception("Echec du traitement d'un message de %s", address)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post(WEBHOOK_PATH)
    async def whatsapp_webhook(request: Request, background: BackgroundTasks) -> Response:
        form = dict(await request.form())

        if settings.validate_twilio:
            if not (settings.public_url and settings.twilio_auth_token):
                raise HTTPException(503, "PUBLIC_URL et TWILIO_AUTH_TOKEN sont requis.")
            signature = request.headers.get("X-Twilio-Signature", "")
            url = settings.public_url + WEBHOOK_PATH
            if not signature_is_valid(settings.twilio_auth_token, url, form, signature):
                raise HTTPException(403, "Signature Twilio invalide.")

        address = str(form.get("From", ""))
        if not address:
            raise HTTPException(400, "Expéditeur manquant.")
        text = str(form.get("Body", "")).strip()
        has_media = str(form.get("NumMedia", "0")) not in {"", "0"}
        media_url = str(form.get("MediaUrl0")) if has_media else None
        media_type = str(form.get("MediaContentType0")) if has_media else None

        # Twilio attend une reponse en moins de 15 secondes : on repond tout de
        # suite et MUMU ecrit au membre une fois le message compris.
        background.add_task(process, address, text, media_url, media_type)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    return app


def app() -> FastAPI:
    """Point d'entree pour uvicorn : `uvicorn app.main:app --factory`."""
    return create_app()
