"""Tests de la vie connectee : Gmail, agenda, confidentialite, deconnexion."""

import base64
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.brain import Ingestion, Understanding, parse_ingestion
from app.calendar_logic import describe_calendar, find_tight_spots
from app.connectors.base import AccessRevoked, Event, SourceDocument
from app.connectors.google import GoogleClient, parse_calendar_event, parse_gmail_message
from app.crypto import decrypt, sign_state
from app.main import GOOGLE_CALLBACK_PATH, GOOGLE_START_PATH, WEBHOOK_PATH, create_app
from app.models import (
    CalendarEvent,
    Connection,
    Fact,
    Item,
    Member,
    Message,
    Purchase,
    SeenDocument,
    make_session_factory,
)
from app.service import CONNECT_UNAVAILABLE, build_context
from app.sync import ACCESS_LOST, sync_connection
from tests.test_pipeline import ALICE, FakeSender, make_settings

SECRET = "cle-de-test"
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_raw(message_id="m1", labels=("INBOX",), plain=None, html=None, subject="Facture"):
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    return {
        "id": message_id,
        "labelIds": list(labels),
        "internalDate": str(int(NOW.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "EDF <factures@edf.fr>"},
                {"name": "Subject", "value": subject},
            ],
            "parts": parts,
        },
    }


def document(message_id="m1") -> SourceDocument:
    return SourceDocument("gmail", message_id, "EDF <factures@edf.fr>", "Votre facture", NOW, "86 €")


class FakeGoogle:
    def __init__(self):
        self.documents: dict[str, SourceDocument | None] = {}
        self.events: list[Event] = []
        self.revoked_tokens: list[str] = []
        self.refresh_fails = False

    def auth_url(self, state):
        return f"https://accounts.google.test/auth?state={state}"

    def exchange_code(self, code):
        return {"refresh_token": "refresh-secret", "access_token": "access", "scope": "gmail"}

    def refresh(self, refresh_token):
        if self.refresh_fails:
            raise AccessRevoked("invalid_grant")
        return "access"

    def revoke(self, refresh_token):
        self.revoked_tokens.append(refresh_token)

    def list_message_ids(self, access, after_ts, limit=50):
        return list(self.documents)[:limit]

    def get_message(self, access, message_id):
        return self.documents[message_id]

    def list_events(self, access, start, days=14):
        return self.events


class ConnectedBrain:
    """Faux modele : repond aux messages et classe les documents."""

    def __init__(self):
        self.next = Understanding(reply="Noté.")
        self.ingestions: dict[str, Ingestion] = {}
        self.ingested: list[str] = []

    def understand(self, context, text, media):
        return self.next

    def ingest(self, context, doc):
        self.ingested.append(doc.external_id)
        return self.ingestions.get(doc.external_id, Ingestion(relevant=False))


BILL = Ingestion(
    relevant=True,
    label="Mail EDF du 16/11",
    items=[{"kind": "paiement", "title": "Payer la facture EDF", "due_date": date(2026, 11, 30),
            "amount_eur": 86.0, "owner": None, "confidence": 0.9}],
    purchases=[{"merchant": "Amazon", "description": "Bouilloire", "amount_eur": 24.9,
                "ordered_on": date(2026, 11, 15), "delivery_expected": date(2026, 11, 18),
                "order_ref": "402-1"}],
    facts=["Client EDF"],
    notify="maintenant",
    message="Dans ton mail EDF : 86 € à payer avant le 30.",
)


@pytest.fixture
def env(tmp_path):
    settings = replace(
        make_settings(tmp_path),
        google_client_id="id",
        google_client_secret="secret",
        secret_key=SECRET,
    )
    sessions = make_session_factory(settings.database_url)
    brain, sender, google = ConnectedBrain(), FakeSender(), FakeGoogle()
    client = TestClient(
        create_app(settings, brain, sender, sessions, google, run_sync_loop=False),
        follow_redirects=False,
    )
    return client, brain, sender, sessions, google


def say(client, text, address=ALICE):
    return client.post(WEBHOOK_PATH, data={"From": address, "Body": text, "NumMedia": "0"})


def connect(client, sessions) -> int:
    """Parcourt la connexion Google pour Alice et renvoie l'id de la connexion."""
    with sessions() as session:
        member_id = session.scalar(select(Member.id).where(Member.address == ALICE))
    state = sign_state(SECRET, member_id)
    assert client.get(GOOGLE_CALLBACK_PATH, params={"state": state, "code": "abc"}).status_code == 200
    with sessions() as session:
        return session.scalar(select(Connection.id))


def run_sync(env, connection_id, now=NOW):
    _, brain, sender, sessions, google = env
    return sync_connection(sessions, connection_id, google, brain, sender, SECRET, now=now)


# --- Lecture des formats Google ------------------------------------------


def test_gmail_parsing_skips_promotions_and_falls_back_to_html():
    assert parse_gmail_message(gmail_raw(labels=("INBOX", "CATEGORY_PROMOTIONS"), plain="x")) is None
    plain = parse_gmail_message(gmail_raw(plain="Montant : 86 €"))
    assert plain.text == "Montant : 86 €" and plain.subject == "Facture"
    assert plain.sender == "EDF <factures@edf.fr>" and plain.received_at == NOW
    rich = parse_gmail_message(gmail_raw(html="<style>p{}</style><p>Total&nbsp;: <b>86 €</b></p>"))
    assert rich.text == "Total : 86 €"


def test_calendar_parsing_handles_timed_all_day_and_cancelled():
    timed = parse_calendar_event(
        {"id": "e1", "summary": "Dentiste",
         "start": {"dateTime": "2026-11-19T17:30:00+01:00"},
         "end": {"dateTime": "2026-11-19T18:00:00+01:00"}}
    )
    assert timed.title == "Dentiste" and not timed.all_day
    assert timed.starts_at.utcoffset() == timedelta(hours=1)
    assert parse_calendar_event({"start": {"date": "2026-11-20"}, "end": {"date": "2026-11-21"}}).all_day
    assert parse_calendar_event({"status": "cancelled"}) is None


def test_google_client_pages_through_ids_and_detects_revocation():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(400, json={"error": "invalid_grant"})
        if "pageToken" in request.url.params:
            return httpx.Response(200, json={"messages": [{"id": "b"}]})
        return httpx.Response(200, json={"messages": [{"id": "a"}], "nextPageToken": "p2"})

    client = GoogleClient("id", "secret", "https://x/cb", httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.list_message_ids("tok", 1700000000) == ["a", "b"]
    assert "after:1700000000" in calls[0].url.params["q"]
    assert "-category:promotions" in calls[0].url.params["q"]
    assert calls[0].headers["authorization"] == "Bearer tok"
    with pytest.raises(AccessRevoked):
        client.refresh("vieux-jeton")
    url = client.auth_url("etat")
    assert "gmail.readonly" in url and "access_type=offline" in url and "state=etat" in url


def test_tight_spots_flag_overlap_and_short_gap_only():
    def at(hour, minute=0):
        return datetime(2026, 11, 19, hour, minute, tzinfo=timezone.utc)

    events = [
        Event("1", "Réunion", at(15), at(16)),
        Event("2", "Dentiste", at(16, 15), at(17)),
        Event("3", "Appel", at(16, 30), at(16, 45)),
        Event("4", "Dîner", at(20), at(22)),
        Event("5", "Férié", at(0), at(23), all_day=True),
    ]
    spots = [(a.title, b.title, kind) for a, b, kind in find_tight_spots(events)]
    assert spots == [("Réunion", "Dentiste", "serre"), ("Dentiste", "Appel", "chevauchement")]
    lines = describe_calendar(events, "Europe/Paris")
    assert lines[0].startswith("- 2026-11-19 (journée)")
    assert any(line.startswith("!") and "Dentiste" in line for line in lines)


def test_parse_ingestion_downgrades_urgent_without_message():
    parsed = parse_ingestion({"relevant": True, "notify": "maintenant", "message": " ",
                              "purchases": [{"merchant": "X"}]})
    assert parsed.notify == "recap" and parsed.purchases == []


# --- Connexion ------------------------------------------------------------


def test_connect_flow_gives_link_then_stores_encrypted_token(env):
    client, brain, sender, sessions, _ = env
    brain.next = Understanding(reply="Voici le lien.", action="connect_google")
    say(client, "connecte mon gmail")

    reply = sender.sent[-1][1]
    link = reply.split("\n")[1]
    assert reply.startswith("Voici le lien.") and GOOGLE_START_PATH in link
    start = client.get(link.replace("https://mumu.example", ""))
    assert start.status_code == 307 and "accounts.google.test" in start.headers["location"]

    state = link.split("state=")[1]
    assert client.get(GOOGLE_CALLBACK_PATH, params={"state": state, "code": "abc"}).status_code == 200
    with sessions() as session:
        connection = session.scalar(select(Connection))
        assert "refresh-secret" not in connection.token_encrypted
        assert decrypt(SECRET, connection.token_encrypted) == "refresh-secret"
        # Le premier passage a tourne juste apres la connexion.
        assert connection.backfilled and connection.status == "active"
    assert "connecté" in sender.sent[-1][1]


def test_tampered_or_missing_state_is_rejected(env):
    client, *_ = env
    assert client.get(GOOGLE_START_PATH, params={"state": "faux.jeton"}).status_code == 400
    assert client.get(GOOGLE_CALLBACK_PATH, params={"state": "", "code": "abc"}).status_code == 400


def test_connect_is_honest_when_google_is_not_configured(tmp_path):
    settings = make_settings(tmp_path)
    brain, sender = ConnectedBrain(), FakeSender()
    brain.next = Understanding(reply="Voici le lien.", action="connect_google")
    client = TestClient(create_app(settings, brain, sender, make_session_factory(settings.database_url)))

    say(client, "connecte mon gmail")

    assert sender.sent == [(ALICE, CONNECT_UNAVAILABLE)]


# --- Lecture en arriere-plan ---------------------------------------------


def test_first_pass_stores_privately_with_source_and_sends_one_summary(env):
    client, brain, sender, sessions, google = env
    say(client, "bonjour")
    google.documents = {"m1": document("m1"), "m2": document("m2"), "pub": None}
    brain.ingestions = {"m1": BILL}
    sender.sent.clear()

    connect(client, sessions)

    # Un seul message apres la confirmation : le resume, pas un message par mail.
    assert [body for _, body in sender.sent][1:] == [
        "J'ai fait le tour de tes mails des 30 derniers jours : 1 chose(s) à suivre et 1 achat(s) repérés.\n"
        "La plus proche : Payer la facture EDF, pour le 2026-11-30.\n"
        "Tu n'as rien à faire, je te préviens au bon moment."
    ]
    with sessions() as session:
        item = session.scalar(select(Item))
        assert (item.source_kind, item.source_ref, item.source_label) == ("gmail", "m1", "Mail EDF du 16/11")
        assert item.visibility == "private" and item.source_message_id is None
        assert session.scalar(select(Purchase)).merchant == "Amazon"
        assert session.scalar(select(Fact).where(Fact.source_kind == "gmail")).visibility == "private"
        assert len(session.scalars(select(SeenDocument)).all()) == 3
        # Le texte du mail n'est enregistre nulle part.
        assert not session.scalars(select(Message).where(Message.body.contains("86 €"))).all()


def test_documents_are_never_read_twice_and_failures_are_retried(env):
    client, brain, _, sessions, google = env
    say(client, "bonjour")
    connection_id = connect(client, sessions)
    google.documents = {"m1": document("m1")}

    brain.ingest = lambda context, doc: (_ for _ in ()).throw(RuntimeError("panne"))
    assert run_sync(env, connection_id).read == 0
    del brain.ingest
    assert run_sync(env, connection_id).read == 1
    assert run_sync(env, connection_id).read == 0
    assert brain.ingested == ["m1"]


def test_urgent_mail_is_announced_within_window_and_capped(env):
    client, brain, sender, sessions, google = env
    say(client, "bonjour")
    connection_id = connect(client, sessions)
    google.documents = {f"m{i}": document(f"m{i}") for i in range(4)}
    brain.ingestions = {key: BILL for key in google.documents}
    sender.sent.clear()

    stats = run_sync(env, connection_id, now=datetime.now(timezone.utc))

    # Quatre mails urgents, mais MUMU n'ecrit de lui-meme que deux fois par jour.
    assert stats.relevant == 4 and stats.notified == 2
    assert [body for _, body in sender.sent] == [BILL.message] * 2


def test_no_proactive_message_outside_whatsapp_window(env):
    client, brain, sender, sessions, google = env
    say(client, "bonjour")
    connection_id = connect(client, sessions)
    google.documents = {"m1": document("m1")}
    brain.ingestions = {"m1": BILL}
    sender.sent.clear()

    stats = run_sync(env, connection_id, now=datetime.now(timezone.utc) + timedelta(hours=30))

    assert stats.relevant == 1 and stats.notified == 0 and sender.sent == []


def test_calendar_is_cached_and_reaches_the_context(env):
    client, _, _, sessions, google = env
    say(client, "bonjour")
    google.events = [
        Event("1", "Réunion", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1)),
        Event("2", "Dentiste", NOW + timedelta(days=1, hours=1, minutes=10), NOW + timedelta(days=1, hours=2)),
    ]
    connect(client, sessions)

    with sessions() as session:
        member = session.scalar(select(Member))
        context = build_context(session, member, None, "Europe/Paris")
    assert context.google_connected
    assert len(context.calendar_lines) == 3 and context.calendar_lines[2].startswith("!")


def test_revoked_access_stops_sync_and_tells_the_member_once(env):
    client, _, sender, sessions, google = env
    say(client, "bonjour")
    connection_id = connect(client, sessions)
    google.refresh_fails = True
    sender.sent.clear()

    assert run_sync(env, connection_id, now=datetime.now(timezone.utc)).revoked
    run_sync(env, connection_id, now=datetime.now(timezone.utc))

    assert [body for _, body in sender.sent] == [ACCESS_LOST]
    with sessions() as session:
        assert session.scalar(select(Connection.status)) == "revoked"


# --- Confidentialite ------------------------------------------------------


def test_mail_derived_items_stay_private_to_their_owner(env):
    client, brain, _, sessions, google = env
    say(client, "bonjour")
    google.documents = {"m1": document("m1")}
    brain.ingestions = {"m1": BILL}
    connect(client, sessions)

    with sessions() as session:
        alice = session.scalar(select(Member))
        flatmate = Member(household_id=alice.household_id, address="whatsapp:+33600000009")
        session.add(flatmate)
        session.flush()
        mine = build_context(session, alice, None, "Europe/Paris")
        theirs = build_context(session, flatmate, None, "Europe/Paris")

    assert [title for _, title, _ in mine.open_items] == ["Payer la facture EDF"]
    assert "Client EDF" in mine.facts and mine.purchase_lines
    assert theirs.open_items == [] and theirs.facts == [] and theirs.purchase_lines == []
    assert theirs.calendar_lines == [] and theirs.recent_messages == []


def test_disconnect_revokes_and_erases_everything_derived(env):
    client, brain, sender, sessions, google = env
    brain.next = Understanding(
        reply="Noté.",
        items=[{"kind": "tache", "title": "Appeler le proprio", "due_date": None,
                "amount_eur": None, "owner": None, "confidence": 1.0}],
    )
    say(client, "faut que j'appelle mon proprio")
    google.documents = {"m1": document("m1")}
    google.events = [Event("1", "Réunion", NOW, NOW + timedelta(hours=1))]
    brain.ingestions = {"m1": BILL}
    connect(client, sessions)

    brain.next = Understanding(reply="C'est retiré, j'ai tout effacé.", action="disconnect_google")
    say(client, "déconnecte")

    assert sender.sent[-1][1] == "C'est retiré, j'ai tout effacé."
    assert google.revoked_tokens == ["refresh-secret"]
    with sessions() as session:
        for model in (Connection, SeenDocument, CalendarEvent, Purchase):
            assert session.scalars(select(model)).all() == []
        # Ce qui venait de WhatsApp reste.
        assert [i.title for i in session.scalars(select(Item))] == ["Appeler le proprio"]
        assert session.scalars(select(Fact)).all() == []

    # Une seconde demande n'a plus rien a retirer, et MUMU le dit.
    say(client, "déconnecte")
    assert "rien à retirer" in sender.sent[-1][1]
