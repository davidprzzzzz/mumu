"""Tests des parties qui n'en avaient pas encore : renouvellement, premier
logement, effacement des conversations, journal, vocal de bout en bout."""

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.brain import Understanding
from app.documents import DocumentFile, store_document
from app.main import VOICE_UNAVAILABLE, WEBHOOK_PATH, create_app
from app.models import AuditLog, Message, Workflow, make_session_factory
from app.money import record_charge
from app.retention import purge_old_messages
from app.signals import Signal, route_signals
from app.brain import Media
from app.workflows import engine
from app.workflows.engine import SourceRef
from tests.test_engine import TODAY, members, session  # noqa: F401 (fixture)
from tests.test_pipeline import ALICE, FakeBrain, FakeSender, make_settings


def test_renewal_waits_for_the_person_to_review_and_decide(session):
    claire, _ = members(session)
    due = TODAY + timedelta(days=30)
    result = route_signals(session, claire, [Signal(
        type="renewal", title="Assurance habitation", organization="MAIF", amount_eur=132.0,
        due_date=due, confidence=0.9)], SourceRef("gmail", "r1"), TODAY)
    wf = result.created[0]
    assert [s.title for s in wf.steps] == ["Vérifier la couverture et le prix", f"Garder ou changer avant le {due.day} décembre"]
    assert wf.status == "active" and wf.visibility == "private"
    # La meme echeance relue dans un autre mail ne cree pas un second processus.
    again = route_signals(session, claire, [Signal(
        type="renewal", title="Rappel", organization="MAIF", due_date=due, confidence=0.9)], SourceRef("gmail", "r2"), TODAY)
    assert again.created == [] and len(session.scalars(select(Workflow)).all()) == 1


def test_move_in_playbook_only_asks_for_what_is_really_missing(session):
    claire, _ = members(session)
    # Deja connus : une attestation valable et une facture d'electricite.
    store_document(
        session, claire,
        {"kind": "attestation_assurance", "title": "Attestation", "valid_until": TODAY + timedelta(days=200)},
        DocumentFile(b"att", "application/pdf", "att.pdf", "whatsapp", "message:1"),
    )
    record_charge(session, claire, merchant="EDF", amount=40.0, charged_on=TODAY, category="energie",
                  source_kind="gmail", source_ref="m1")
    result = route_signals(session, claire, [Signal(
        type="life_event", life_event="emmenagement", title="Nouveau studio", confidence=0.9)],
        SourceRef("whatsapp", "message:2"), TODAY)
    wf = result.created[0]
    status = {s.key: (s.status, s.done_by) for s in wf.steps}
    assert status["insurance"] == ("done", "mumu") and status["energy"] == ("done", "mumu")
    assert status["caf"] == ("todo", None) and status["inventory"] == ("todo", None)
    assert wf.next_action == "Demande d'aide au logement à la CAF"

    # Une demarche CAF ouverte ensuite coche l'etape au prochain recalcul.
    route_signals(session, claire, [Signal(
        type="admin_letter", title="Demande d'APL", organization="CAF", actions=["Envoyer le formulaire"],
        confidence=0.9)], SourceRef("whatsapp", "message:3"), TODAY)
    engine.refresh(session, wf, TODAY)
    assert next(s for s in wf.steps if s.key == "caf").status == "done"


def test_old_conversation_text_is_erased_but_its_row_and_the_log_remain(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/r.db")
    from app.models import Household, Member, now_utc

    with factory() as s:
        household = Household()
        s.add(household)
        s.flush()
        member = Member(household_id=household.id, address="whatsapp:+9")
        s.add(member)
        s.flush()
        s.add_all([
            Message(household_id=household.id, member_id=member.id, direction="in", body="vieux",
                    created_at=now_utc() - timedelta(days=200)),
            Message(household_id=household.id, member_id=member.id, direction="in", body="récent"),
        ])
        s.commit()

    assert purge_old_messages(factory, 180) == 1
    assert purge_old_messages(factory, 0) == 0
    with factory() as s:
        assert sorted(m.body for m in s.scalars(select(Message))) == ["", "récent"]
        log = s.scalar(select(AuditLog).where(AuditLog.action == "messages_purged"))
        assert log.detail == {"count": 1, "days": 180}


def test_important_actions_are_written_to_the_audit_log(session):
    claire, _ = members(session)
    route_signals(session, claire, [Signal(
        type="invoice", title="Facture", organization="Free", amount_eur=29.99,
        due_date=TODAY + timedelta(days=5), confidence=0.9)], SourceRef("gmail", "m9"), TODAY)
    session.flush()
    actions = [a.action for a in session.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert "workflow_created" in actions and "signals_routed" in actions


class FakeTranscriber:
    def __init__(self, text):
        self.text = text

    def transcribe(self, data, content_type):
        return self.text


def _voice_client(tmp_path, transcriber):
    settings = make_settings(tmp_path)
    brain, sender = FakeBrain(), FakeSender()
    brain.next = Understanding(reply="Noté, loyer vendredi.")
    client = TestClient(create_app(
        settings, brain, sender, make_session_factory(settings.database_url), run_background=False,
        transcriber=transcriber, media_downloader=lambda url, content_type: Media(content_type, b"ogg"),
    ))
    data = {"From": ALICE, "Body": "", "NumMedia": "1", "MediaUrl0": "https://api.twilio.test/V1",
            "MediaContentType0": "audio/ogg"}
    return client, brain, sender, data


def test_a_voice_note_is_transcribed_and_understood_like_a_written_message(tmp_path):
    client, brain, sender, data = _voice_client(tmp_path, FakeTranscriber("Rappelle-moi le loyer vendredi"))
    client.post(WEBHOOK_PATH, data=data)
    context, text, media = brain.calls[0]
    assert text == "[Vocal transcrit] Rappelle-moi le loyer vendredi" and media is None
    assert sender.sent == [(ALICE, "Noté, loyer vendredi.")]


def test_without_transcription_mumu_says_so_instead_of_guessing(tmp_path):
    client, brain, sender, data = _voice_client(tmp_path, None)
    client.post(WEBHOOK_PATH, data=data)
    assert brain.calls == [] and sender.sent == [(ALICE, VOICE_UNAVAILABLE)]
