"""Tests contre les vrais services. Ignores par defaut.

Ils demandent de vrais identifiants et coutent quelques centimes :
    MUMU_LIVE=1 python -m pytest -m live tests/live

- Google : utilise la premiere connexion Google active de la base pointee
  par DATABASE_URL (celle de ton deploiement, apres avoir connecte ta boite),
  avec GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET et MUMU_SECRET_KEY. Lecture
  seule : rien n'est modifie chez Google ni dans la base.
- Claude : ANTHROPIC_API_KEY. Verifie que le vrai modele comprend une facture
  inventee, ce que les autres tests ne peuvent pas prouver.
"""

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("MUMU_LIVE") != "1", reason="MUMU_LIVE=1 non défini"),
]


def test_google_connection_refreshes_and_reads_mail_and_calendar():
    from app.config import load_settings
    from app.connectors.google import GoogleClient
    from app.crypto import decrypt
    from app.models import Connection, make_session_factory

    settings = load_settings()
    if not settings.connections_enabled:
        pytest.skip("Connexion Google non configurée")
    with make_session_factory(settings.database_url, create_tables=False)() as session:
        connection = session.scalar(
            select(Connection).where(Connection.provider == "google", Connection.status == "active")
        )
    if connection is None:
        pytest.skip("Aucune connexion Google active dans la base")

    google = GoogleClient(settings.google_client_id, settings.google_client_secret, settings.public_url)
    access = google.refresh(decrypt(settings.secret_key, connection.token_encrypted))
    since = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    ids = google.list_message_ids(access, since, limit=5)
    for message_id in ids:
        # Un message peut etre ignore (publicite) : il doit alors valoir None.
        document = google.get_message(access, message_id)
        assert document is None or document.external_id == message_id
    events = google.list_events(access, datetime.now(timezone.utc))
    assert isinstance(events, list)


def test_claude_understands_an_invented_invoice():
    from app.brain import ClaudeBrain, Context
    from app.config import load_settings
    from app.connectors.base import SourceDocument

    settings = load_settings()
    if not settings.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY non défini")
    brain = ClaudeBrain(settings.anthropic_api_key, settings.model, settings.ingest_model)
    today = date.today()
    due = today + timedelta(days=10)
    mail = SourceDocument(
        "gmail", "live-test", "Electricite Test <factures@exemple.fr>", "Votre facture d'électricité",
        datetime.now(timezone.utc),
        f"Bonjour, votre facture n° F-2026-118 d'un montant de 64,20 € sera prélevée le {due:%d/%m/%Y}.",
    )
    context = Context(today=today, member_name="Test", facts=[], open_items=[], recent_messages=[])

    ingestion = brain.ingest(context, mail)

    assert ingestion.relevant
    invoices = [s for s in ingestion.signals if s.type in ("invoice", "subscription_charge")]
    assert invoices and invoices[0].amount_eur == pytest.approx(64.20)
    assert any(s.due_date == due or s.event_date == due for s in invoices)
