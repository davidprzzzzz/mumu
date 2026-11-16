"""Tests unitaires des protections ajoutees apres revue : migrations,
usage des jetons, message livre deux fois, plusieurs fournisseurs."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app.brain import Understanding
from app.crypto import sign_payload, sign_state, verify_payload, verify_state
from app.documents import LINK_PURPOSE
from app.main import WEBHOOK_PATH, create_app
from app.models import Base, Connection, Member, Message, make_session_factory
from app.sync import sync_all
from tests.test_pipeline import ALICE, FakeBrain, FakeSender, make_settings

ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migrations_build_exactly_the_schema_of_the_models(tmp_path):
    url = f"sqlite:///{tmp_path}/migrated.db"
    config = alembic_config(url)
    command.upgrade(config, "head")
    # Leve une erreur si les modeles et la base migree different.
    command.check(config)
    tables = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    command.downgrade(config, "base")
    assert set(inspect(create_engine(url)).get_table_names()) <= {"alembic_version"}


def test_production_databases_are_not_created_behind_the_migrations_back(tmp_path):
    # Hors SQLite, make_session_factory ne cree rien : seul Alembic le fait.
    url = f"sqlite:///{tmp_path}/empty.db"
    make_session_factory(url, create_tables=False)
    assert inspect(create_engine(url)).get_table_names() == []


def test_a_token_only_works_for_the_purpose_it_was_made_for():
    secret = "cle"
    document_token = sign_payload(secret, {"d": 4, "m": 7}, 60, LINK_PURPOSE)
    assert verify_payload(secret, document_token, LINK_PURPOSE) == {"d": 4, "m": 7}
    # Un lien de document ne peut pas servir a connecter un compte Google...
    assert verify_state(secret, document_token) is None
    # ... ni l'inverse.
    assert verify_payload(secret, sign_state(secret, 7), LINK_PURPOSE) is None
    assert verify_state(secret, sign_state(secret, 7)) == 7
    expired = sign_payload(secret, {"m": 7}, -1, "connect")
    assert verify_state(secret, expired) is None


def test_a_message_delivered_twice_is_processed_once(tmp_path):
    settings = make_settings(tmp_path)
    sessions = make_session_factory(settings.database_url)
    brain, sender = FakeBrain(), FakeSender()
    brain.next = Understanding(reply="Noté.")
    client = TestClient(create_app(settings, brain, sender, sessions, run_background=False))
    data = {"From": ALICE, "Body": "Bonjour", "NumMedia": "0", "MessageSid": "SM123"}

    for _ in range(3):
        assert client.post(WEBHOOK_PATH, data=data).status_code == 200

    assert len(brain.calls) == 1 and sender.sent == [(ALICE, "Noté.")]
    with sessions() as session:
        assert session.scalar(select(Message).where(Message.direction == "in")).external_id == "SM123"
        assert len(session.scalars(select(Message)).all()) == 2


def test_sync_all_routes_each_connection_to_its_own_provider(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    sessions = make_session_factory(settings.database_url)
    with sessions() as session:
        from app.models import Household

        household = Household()
        session.add(household)
        session.flush()
        for index, provider in enumerate(["google", "outlook", "google"]):
            member = Member(household_id=household.id, address=f"whatsapp:+3360000000{index}")
            session.add(member)
            session.flush()
            session.add(Connection(member_id=member.id, provider=provider, token_encrypted="x"))
        session.commit()

    seen = []
    monkeypatch.setattr(
        "app.sync.sync_connection",
        lambda factory, connection_id, provider, *args: seen.append((connection_id, provider)),
    )
    google, outlook = object(), object()

    sync_all(sessions, {"google": google}, None, None, "cle")
    assert seen == [(1, google), (3, google)]
    seen.clear()
    sync_all(sessions, {"google": google, "outlook": outlook}, None, None, "cle")
    assert seen == [(1, google), (2, outlook), (3, google)]
