"""Tests unitaires : moteur de processus, routage, argent, foyer, voix."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app import household as hh
from app.messaging import Outbox
from app.models import Approval, Household, Member, Message, Workflow, make_session_factory
from app.money import expected_charges, price_changes, record_charge
from app.proactive import Candidate, is_quiet_time
from app.signals import Signal, route_signals
from app.voice import OpenAITranscriber
from app.workflows import engine
from app.workflows.engine import SourceRef

TODAY = date(2026, 11, 16)


@pytest.fixture
def session(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/u.db")
    with factory() as s:
        household = Household()
        s.add(household)
        s.flush()
        s.add_all([
            Member(household_id=household.id, address="whatsapp:+1", name="Claire"),
            Member(household_id=household.id, address="whatsapp:+2", name="Thomas"),
        ])
        s.flush()
        yield s


def members(session):
    return session.scalars(select(Member).order_by(Member.id)).all()


def invoice(**kw):
    return Signal(**{"type": "invoice", "title": "Facture EDF", "organization": "EDF", "amount_eur": 86.0,
                     "due_date": TODAY + timedelta(days=10), "reference": "F-1", "confidence": 0.9, **kw})


def test_same_invoice_from_two_sources_is_one_process_with_two_sources(session):
    claire, _ = members(session)
    route_signals(session, claire, [invoice()], SourceRef("gmail", "m1", "Mail EDF"), TODAY)
    result = route_signals(session, claire, [invoice(organization="edf")], SourceRef("whatsapp", "message:9"), TODAY)
    workflows = session.scalars(select(Workflow)).all()
    assert len(workflows) == 1 and result.updated == workflows
    assert {s.source_kind for s in workflows[0].sources} == {"gmail", "whatsapp"}


def test_payment_confirmation_resolves_the_matching_invoice_only(session):
    claire, _ = members(session)
    route_signals(session, claire, [invoice(), invoice(organization="Free", reference="F-9")], SourceRef("gmail", "m1"), TODAY)
    result = route_signals(session, claire, [Signal(type="payment_confirmation", title="Paiement reçu",
                                                     organization="EDF", reference="F-1", confidence=0.9)],
                           SourceRef("gmail", "m2"), TODAY)
    statuses = {wf.organization: wf.status for wf in session.scalars(select(Workflow))}
    assert statuses == {"EDF": "resolved", "Free": "active"} and len(result.resolved) == 1


def test_confirmation_without_a_clear_match_changes_nothing(session):
    claire, _ = members(session)
    route_signals(session, claire, [invoice(organization="EDF"), invoice(organization="EDF Pro", reference="X")],
                  SourceRef("gmail", "m1"), TODAY)
    route_signals(session, claire, [Signal(type="payment_confirmation", title="Paiement", organization="EDF",
                                           confidence=0.9)], SourceRef("gmail", "m2"), TODAY)
    assert {wf.status for wf in session.scalars(select(Workflow))} == {"active"}


def test_a_step_awaiting_approval_cannot_be_completed_and_rejection_keeps_it_with_the_person(session):
    claire, _ = members(session)
    route_signals(session, claire, [Signal(type="document_request", title="Attestation", organization="Fac",
                                           requested_document="autre", confidence=0.9)], SourceRef("whatsapp", "m"), TODAY)
    wf = session.scalar(select(Workflow))
    engine.complete_step(session, wf, "find", by="user", member_id=claire.id, today=TODAY)
    assert wf.status == "waiting_approval"
    assert engine.complete_step(session, wf, "send", by="user", today=TODAY) is None
    approval = session.scalar(select(Approval))
    engine.decide(session, approval, False, claire, TODAY)
    send = wf.steps[1]
    assert approval.status == "rejected" and send.status == "todo" and wf.status == "active"


def test_a_process_is_never_resolved_by_a_reminder_only_by_its_steps(session):
    claire, _ = members(session)
    route_signals(session, claire, [invoice()], SourceRef("gmail", "m1"), TODAY)
    wf = session.scalar(select(Workflow))
    engine.refresh(session, wf, TODAY + timedelta(days=30))
    assert wf.status == "active"
    engine.complete_step(session, wf, None, by="user", member_id=claire.id, today=TODAY)
    assert wf.status == "resolved" and wf.resolved_at is not None


def test_only_the_owner_can_share_a_private_process(session):
    claire, thomas = members(session)
    route_signals(session, claire, [invoice()], SourceRef("gmail", "m1"), TODAY)
    wf = session.scalar(select(Workflow))
    with pytest.raises(PermissionError):
        hh.propose(session, wf, claire, thomas, Outbox())
    assignment, delivered = hh.propose(session, wf, thomas, claire, Outbox())
    # Thomas n'a jamais ecrit : rien ne lui est envoye, et la personne le sait.
    assert wf.visibility == "household" and not delivered and not assignment.delivered


def test_invites_expire_and_cannot_be_reused(session):
    claire, _ = members(session)
    old = hh.create_invite(session, claire, "Léo", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    fresh = hh.create_invite(session, claire, "Zoé")
    session.flush()
    assert hh.try_join(session, "whatsapp:+3", f"REJOINDRE {old.code}") is None
    assert hh.try_join(session, "whatsapp:+4", f"rejoindre {fresh.code.lower()}").name == "Zoé"
    assert hh.try_join(session, "whatsapp:+5", f"REJOINDRE {fresh.code}") is None


def test_price_changes_ignore_small_noise_and_recurring_charges_are_projected(session):
    claire, _ = members(session)
    for i, (amount, day) in enumerate([(10.99, date(2026, 9, 1)), (10.99, date(2026, 10, 1)), (12.99, date(2026, 10, 31))]):
        record_charge(session, claire, merchant="Spotify", amount=amount, charged_on=day,
                      category="abonnement", source_kind="gmail", source_ref=f"s{i}")
    for i, (amount, day) in enumerate([(50.0, date(2026, 9, 10)), (50.3, date(2026, 10, 10))]):
        record_charge(session, claire, merchant="Navigo", amount=amount, charged_on=day,
                      category="autre", source_kind="gmail", source_ref=f"n{i}")
    # Le meme document compte une seule fois.
    record_charge(session, claire, merchant="Spotify", amount=12.99, charged_on=date(2026, 10, 31),
                  category="abonnement", source_kind="gmail", source_ref="s2")
    changes = price_changes(session, claire)
    assert [(c.merchant, c.old_amount, c.new_amount) for c in changes] == [("Spotify", 10.99, 12.99)]
    expected = expected_charges(session, claire, TODAY, date(2026, 11, 30))
    # Navigo : 10/10 + 30 j = 9/11, deja passe, donc 9/12, apres la fin du mois.
    assert [(e.merchant, e.amount, e.expected_on) for e in expected] == [("Spotify", 12.99, date(2026, 11, 30))]


def test_purchase_dates_fall_back_to_legal_periods_and_are_flagged(session):
    claire, _ = members(session)
    result = route_signals(session, claire, [Signal(type="order", title="Casque", organization="Fnac",
                                                     reference="A1", event_date=date(2026, 1, 10), confidence=0.9)],
                           SourceRef("gmail", "o1"), TODAY)
    purchase = result.purchases[0]
    assert purchase.return_deadline == date(2026, 1, 24) and purchase.warranty_until == date(2028, 1, 10)
    assert purchase.dates_are_estimates


def test_quiet_hours_and_low_confidence_keep_mumu_silent():
    paris = ZoneInfo("Europe/Paris")
    assert is_quiet_time(datetime(2026, 11, 16, 22, 0, tzinfo=paris), "Europe/Paris")
    assert is_quiet_time(datetime(2026, 11, 16, 7, 30, tzinfo=paris), "Europe/Paris")
    assert not is_quiet_time(datetime(2026, 11, 16, 10, 0, tzinfo=paris), "Europe/Paris")
    assert Candidate("k", 3, 3, 0.5, lambda: "x").score == 0
    assert Candidate("k", 2, 2, 0.6, lambda: "x").score == 4


def test_voice_transcription_request_and_unsupported_formats():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": " Rappelle-moi de payer le loyer "})

    transcriber = OpenAITranscriber("cle", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert transcriber.transcribe(b"ogg", "audio/ogg; codecs=opus") == "Rappelle-moi de payer le loyer"
    body = seen[0].content
    assert b'name="model"' in body and b"gpt-4o-transcribe" in body and b'filename="vocal.ogg"' in body
    assert seen[0].headers["authorization"] == "Bearer cle"
    assert transcriber.transcribe(b"x", "audio/amr") is None and len(seen) == 1
