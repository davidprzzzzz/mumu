"""Serveur web de MUMU : messages WhatsApp, connexion des comptes, documents."""

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .audit import log_action
from .brain import Brain, ClaudeBrain, Media
from .config import Settings, load_settings
from .connectors.base import AccessRevoked
from .connectors.google import SCOPES, GoogleClient
from .crypto import decrypt, encrypt, sign_state, verify_state
from .documents import document_link, resolve_link
from .models import Connection, Member, make_session_factory
from .proactive import tick
from .retention import purge_old_messages
from .service import Services, handle_incoming
from .sync import disconnect_google, sync_all, sync_connection
from .voice import OpenAITranscriber, Transcriber
from .whatsapp import Sender, TwilioSender, download_media, signature_is_valid

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mumu")

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
WEBHOOK_PATH = "/webhook/whatsapp"
GOOGLE_START_PATH = "/connect/google"
GOOGLE_CALLBACK_PATH = "/connect/google/callback"
DOCUMENT_PATH = "/documents"

CONNECTED_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MUMU</title>
<body style="font-family: system-ui; text-align: center; padding: 3rem 1.5rem">
<p style="font-size: 3rem; margin: 0">☁️</p>
<h1>C'est connecté.</h1>
<p>Tu peux fermer cette page et retourner sur WhatsApp.</p>
<p style="color: #666">MUMU lit tes mails en lecture seule. Écris-lui « déconnecte »
à tout moment pour retirer l'accès et effacer ce qu'il en a tiré.</p>
</body>"""

CONNECTED_MESSAGE = (
    "C'est connecté ☁️ Je fais le tour de tes 30 derniers jours de mails "
    "(factures, commandes, démarches, rendez-vous) et je reviens vers toi avec ce qui compte."
)
VOICE_UNAVAILABLE = "Je ne sais pas encore écouter les vocaux. Tu peux me l'écrire ?"


def create_app(
    settings: Settings | None = None,
    brain: Brain | None = None,
    sender: Sender | None = None,
    session_factory: sessionmaker | None = None,
    google: GoogleClient | None = None,
    run_background: bool = True,
    transcriber: Transcriber | None = None,
    media_downloader=None,
) -> FastAPI:
    settings = settings or load_settings()
    session_factory = session_factory or make_session_factory(settings.database_url)
    brain = brain or ClaudeBrain(settings.anthropic_api_key, settings.model, settings.ingest_model)
    sender = sender or TwilioSender(
        settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_whatsapp_from
    )
    if transcriber is None and settings.openai_api_key:
        transcriber = OpenAITranscriber(settings.openai_api_key, settings.transcription_model)
    download = media_downloader or (
        lambda url, content_type: download_media(
            url, content_type, settings.twilio_account_sid, settings.twilio_auth_token
        )
    )
    if google is None and settings.connections_enabled:
        google = GoogleClient(
            settings.google_client_id, settings.google_client_secret, settings.public_url + GOOGLE_CALLBACK_PATH
        )
    google_enabled = google is not None and settings.connections_enabled

    services = Services(
        whatsapp_from=settings.twilio_whatsapp_from,
        send=sender.send,
        document_link=(
            (lambda doc, member: document_link(settings.secret_key, settings.public_url, doc, member))
            if settings.secret_key and settings.public_url
            else None
        ),
    )
    if google_enabled:
        services.connect_link = lambda member_id: (
            f"{settings.public_url}{GOOGLE_START_PATH}?state={sign_state(settings.secret_key, member_id)}"
        )
        services.disconnect = lambda member_id: disconnect_google(
            session_factory, member_id, google, settings.secret_key
        )

    def background_loop() -> None:
        while True:
            time.sleep(settings.sync_interval_seconds)
            if google_enabled:
                sync_all(session_factory, google, brain, sender, settings.secret_key, settings.timezone)
            try:
                tick(session_factory, sender, settings.timezone)
                purge_old_messages(session_factory, settings.message_retention_days)
            except Exception:
                log.exception("Passage d'arrière-plan impossible")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # En beta, le travail d'arriere-plan tourne dans le meme processus.
        # Avec plusieurs exemplaires du serveur, utiliser app.worker a la place.
        if run_background:
            threading.Thread(target=background_loop, daemon=True, name="mumu-background").start()
        yield

    app = FastAPI(title="MUMU", lifespan=lifespan)

    def process(
        address: str, text: str, media_url: str | None, media_type: str | None, message_sid: str | None = None
    ) -> None:
        try:
            media = None
            if media_url and media_type:
                media = download(media_url, media_type)
            if media is not None and media.content_type.startswith("audio/"):
                transcript = transcriber.transcribe(media.data, media.content_type) if transcriber else None
                if transcript is None:
                    sender.send(address, VOICE_UNAVAILABLE)
                    return
                text = f"{text}\n[Vocal transcrit] {transcript}".strip()
                media, media_url = None, None
            reply = handle_incoming(
                session_factory, brain, address, text, media, settings.timezone, services,
                media_ref=media_url, external_id=message_sid,
            )
            if reply is not None:
                sender.send(address, reply)
        except Exception:
            log.exception("Echec du traitement d'un message de %s", address)

    def first_sync(connection_id: int) -> None:
        try:
            sync_connection(
                session_factory, connection_id, google, brain, sender, settings.secret_key, settings.timezone
            )
        except Exception:
            log.exception("Premier passage impossible pour la connexion %s", connection_id)

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
        message_sid = str(form.get("MessageSid") or "") or None

        # Twilio attend une reponse en moins de 15 secondes : on repond tout de
        # suite et MUMU ecrit au membre une fois le message compris.
        background.add_task(process, address, text, media_url, media_type, message_sid)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    @app.get(f"{DOCUMENT_PATH}/{{token}}")
    def open_document(token: str) -> Response:
        if not settings.secret_key:
            raise HTTPException(404)
        with session_factory() as session:
            doc = resolve_link(session, settings.secret_key, token)
            if doc is None:
                raise HTTPException(404, "Lien expiré. Redemande le document à MUMU.")
            data = None
            if doc.source_kind == "gmail" and google is not None and doc.attachment_ref:
                connection = session.scalar(
                    select(Connection).where(
                        Connection.member_id == doc.member_id, Connection.status == "active"
                    )
                )
                token_plain = decrypt(settings.secret_key, connection.token_encrypted) if connection else None
                if token_plain:
                    try:
                        access = google.refresh(token_plain)
                        data = google.get_attachment(access, doc.source_ref, doc.attachment_ref)
                    except AccessRevoked:
                        data = None
            elif doc.source_kind == "whatsapp" and doc.attachment_ref:
                media: Media | None = download(doc.attachment_ref, doc.mime_type)
                data = media.data if media else None
            if data is None:
                raise HTTPException(410, "Ce document n'est plus accessible à sa source.")
            log_action(session, "document_opened", household_id=doc.household_id, member_id=doc.member_id,
                       actor="member", target=f"document:{doc.id}")
            session.commit()
            filename = (doc.filename or "document").replace('"', "")
            return Response(
                content=data, media_type=doc.mime_type,
                headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"},
            )

    @app.get(GOOGLE_START_PATH)
    def google_start(state: str) -> RedirectResponse:
        if not google_enabled:
            raise HTTPException(404, "Connexion Google désactivée.")
        if verify_state(settings.secret_key, state) is None:
            raise HTTPException(400, "Lien expiré. Redemande un lien à MUMU.")
        return RedirectResponse(google.auth_url(state))

    @app.get(GOOGLE_CALLBACK_PATH)
    def google_callback(background: BackgroundTasks, state: str = "", code: str = "", error: str = "") -> HTMLResponse:
        if not google_enabled:
            raise HTTPException(404, "Connexion Google désactivée.")
        member_id = verify_state(settings.secret_key, state)
        if member_id is None:
            raise HTTPException(400, "Lien expiré. Redemande un lien à MUMU.")
        if error or not code:
            return HTMLResponse("<p>Connexion annulée. Rien n'a été partagé avec MUMU.</p>")

        tokens = google.exchange_code(code)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise HTTPException(400, "Google n'a pas donné d'accès durable. Réessaie depuis le lien.")

        with session_factory() as session:
            member = session.get(Member, member_id)
            if member is None:
                raise HTTPException(400, "Compte MUMU introuvable.")
            connection = session.scalar(
                select(Connection).where(Connection.member_id == member_id, Connection.provider == "google")
            )
            if connection is None:
                connection = Connection(member_id=member_id, provider="google", token_encrypted="")
                session.add(connection)
            connection.token_encrypted = encrypt(settings.secret_key, refresh_token)
            connection.scopes = tokens.get("scope") or " ".join(SCOPES)
            connection.status = "active"
            log_action(session, "connection_created", household_id=member.household_id, member_id=member_id,
                       actor="member", provider="google")
            session.commit()
            connection_id, address = connection.id, member.address

        try:
            sender.send(address, CONNECTED_MESSAGE)
        except Exception:
            log.exception("Message de confirmation non envoyé à %s", address)
        background.add_task(first_sync, connection_id)
        return HTMLResponse(CONNECTED_PAGE)

    return app


def app() -> FastAPI:
    """Point d'entree pour uvicorn : `uvicorn app.main:app --factory`.

    RUN_BACKGROUND=false desactive le travail d'arriere-plan (a confier alors
    a `python -m app.worker`).
    """
    import os

    return create_app(run_background=os.getenv("RUN_BACKGROUND", "true").lower() != "false")
