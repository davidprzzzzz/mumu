"""Tests de la chaine complete, avec un faux modele et un faux envoi WhatsApp."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.brain import Context, Media, Understanding, parse_understanding, render_context
from app.config import Settings
from app.main import WEBHOOK_PATH, create_app
from app.models import Fact, Item, Member, Message, make_session_factory
from app.persona import FALLBACK_REPLY
from app.whatsapp import split_message

ALICE = "whatsapp:+33600000001"
BOB = "whatsapp:+33600000002"


class FakeBrain:
    def __init__(self):
        self.calls: list[tuple[Context, str, Media | None]] = []
        self.next: Understanding | Exception = Understanding(reply="Noté.")

    def understand(self, context, text, media):
        self.calls.append((context, text, media))
        if isinstance(self.next, Exception):
            raise self.next
        return self.next


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, to, body):
        self.sent.append((to, body))


def make_settings(tmp_path, validate=False) -> Settings:
    return Settings(
        anthropic_api_key="test",
        model="test",
        database_url=f"sqlite:///{tmp_path}/test.db",
        twilio_account_sid="ACtest",
        twilio_auth_token="token",
        twilio_whatsapp_from="whatsapp:+14155238886",
        public_url="https://mumu.example",
        validate_twilio=validate,
    )


@pytest.fixture
def env(tmp_path):
    settings = make_settings(tmp_path)
    sessions = make_session_factory(settings.database_url)
    brain, sender = FakeBrain(), FakeSender()
    client = TestClient(create_app(settings, brain, sender, sessions))
    return client, brain, sender, sessions


def post(client, sender_address, body):
    return client.post(WEBHOOK_PATH, data={"From": sender_address, "Body": body, "NumMedia": "0"})


def test_message_creates_household_item_and_reply(env):
    client, brain, sender, sessions = env
    brain.next = Understanding(
        reply="Noté : assurance habitation à renouveler avant le 1er décembre.",
        items=[
            {
                "kind": "echeance",
                "title": "Renouveler l'assurance habitation",
                "due_date": date(2026, 12, 1),
                "amount_eur": 14.5,
                "owner": "Alice",
                "confidence": 0.9,
            }
        ],
        facts=["Alice vit seule dans un studio à Paris"],
        member_name="Alice",
    )

    response = post(client, ALICE, "Mon assurance habitation finit le 1er décembre")

    assert response.status_code == 200
    assert sender.sent == [(ALICE, brain.next.reply)]
    with sessions() as session:
        member = session.scalar(select(Member))
        assert member.name == "Alice"
        inbound = session.scalar(select(Message).where(Message.direction == "in"))
        item = session.scalar(select(Item))
        fact = session.scalar(select(Fact))
        # Chaque element et chaque fait pointe vers le message d'origine.
        assert item.source_message_id == inbound.id
        assert fact.source_message_id == inbound.id
        assert item.status == "open" and item.due_date == date(2026, 12, 1)
        assert session.scalars(select(Message).where(Message.direction == "out")).one().body


def test_context_carries_memory_and_open_items(env):
    client, brain, _, _ = env
    brain.next = Understanding(
        reply="Noté.",
        items=[{"kind": "tache", "title": "Appeler le propriétaire", "due_date": None,
                "amount_eur": None, "owner": None, "confidence": 0.8}],
        facts=["Le propriétaire s'appelle M. Garcia"],
    )
    post(client, ALICE, "Faut que j'appelle mon proprio M. Garcia")
    brain.next = Understanding(reply="Oui ?")
    post(client, ALICE, "Tu te souviens ?")

    context = brain.calls[1][0]
    assert context.facts == ["Le propriétaire s'appelle M. Garcia"]
    assert [title for _, title, _ in context.open_items] == ["Appeler le propriétaire"]
    assert context.recent_messages[0] == ("in", "Faut que j'appelle mon proprio M. Garcia")
    assert "M. Garcia" in render_context(context)


def test_done_ids_close_only_own_household_items(env):
    client, brain, _, sessions = env
    item = {"kind": "tache", "title": "Sortir les poubelles", "due_date": None,
            "amount_eur": None, "owner": None, "confidence": 1.0}
    brain.next = Understanding(reply="Noté.", items=[item])
    post(client, ALICE, "poubelles")
    post(client, BOB, "poubelles")
    with sessions() as session:
        ids = session.scalars(select(Item.id).order_by(Item.id)).all()

    # Bob tente de clore les deux : seul le sien doit l'etre.
    brain.next = Understanding(reply="Bravo.", done_item_ids=ids)
    post(client, BOB, "c'est fait")

    with sessions() as session:
        statuses = session.scalars(select(Item.status).order_by(Item.id)).all()
    assert statuses == ["open", "done"]


def test_model_failure_sends_honest_fallback_and_keeps_message(env):
    client, brain, sender, sessions = env
    brain.next = RuntimeError("panne")

    post(client, ALICE, "Bonjour")

    assert sender.sent == [(ALICE, FALLBACK_REPLY)]
    with sessions() as session:
        assert session.scalar(select(Message).where(Message.direction == "in")).body == "Bonjour"


def test_invalid_signature_is_rejected(tmp_path):
    settings = make_settings(tmp_path, validate=True)
    brain, sender = FakeBrain(), FakeSender()
    client = TestClient(
        create_app(settings, brain, sender, make_session_factory(settings.database_url))
    )

    response = client.post(
        WEBHOOK_PATH,
        data={"From": ALICE, "Body": "Bonjour"},
        headers={"X-Twilio-Signature": "faux"},
    )

    assert response.status_code == 403
    assert brain.calls == [] and sender.sent == []


def test_valid_signature_is_accepted(tmp_path):
    from twilio.request_validator import RequestValidator

    settings = make_settings(tmp_path, validate=True)
    brain, sender = FakeBrain(), FakeSender()
    client = TestClient(
        create_app(settings, brain, sender, make_session_factory(settings.database_url))
    )
    data = {"From": ALICE, "Body": "Bonjour", "NumMedia": "0"}
    signature = RequestValidator(settings.twilio_auth_token).compute_signature(
        settings.public_url + WEBHOOK_PATH, data
    )

    response = client.post(WEBHOOK_PATH, data=data, headers={"X-Twilio-Signature": signature})

    assert response.status_code == 200
    assert len(sender.sent) == 1


def test_parse_understanding_drops_bad_items_and_dates():
    parsed = parse_understanding(
        {
            "reply": " Ok ",
            "items": [
                {"kind": "inconnu", "title": "x", "confidence": 1},
                {"kind": "paiement", "title": "Cantine", "due_date": "pas une date",
                 "amount_eur": "86", "confidence": 0.7},
            ],
            "facts": ["", "Fait utile"],
            "done_item_ids": [3, "4"],
        }
    )
    assert parsed.reply == "Ok"
    assert len(parsed.items) == 1
    assert parsed.items[0]["due_date"] is None and parsed.items[0]["amount_eur"] is None
    assert parsed.facts == ["Fait utile"]
    assert parsed.done_item_ids == [3]


def test_split_message_respects_limit():
    body = "\n".join(f"ligne {i} " + "x" * 80 for i in range(40))
    parts = split_message(body, limit=500)
    assert all(len(part) <= 500 for part in parts)
    assert "".join(parts).replace("\n", "") == body.replace("\n", "")
