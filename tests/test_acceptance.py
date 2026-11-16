"""Les dix scenarios d'acceptation : MUMU aide-t-il vraiment ?

Nature de ces tests : integration avec des services simules. WhatsApp,
Google et le modele sont remplaces par des doublures deterministes ; la
base, le moteur de processus, le routage, la confidentialite, le moteur
proactif et les routes HTTP sont les vrais. Ce que le modele *comprendrait*
d'un vrai mail est ici ecrit a la main : ces tests prouvent ce que MUMU fait
de cette comprehension, pas la qualite de la comprehension elle-meme.
"""

import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agenda import week_overview
from app.brain import DocumentMeta, Ingestion, Media, Understanding
from app.connectors.base import AttachmentRef, Event, SourceDocument
from app.crypto import sign_payload, sign_state
from app.documents import resolve_link
from app.main import GOOGLE_CALLBACK_PATH, WEBHOOK_PATH, create_app
from app.models import (
    Approval,
    Assignment,
    Charge,
    Document,
    Member,
    Person,
    Purchase,
    Workflow,
    make_session_factory,
)
from app.money import price_changes
from app.proactive import tick
from app.service import build_context
from app.signals import Signal
from app.sync import sync_connection
from tests.test_connected import FakeGoogle
from tests.test_pipeline import FakeSender, make_settings

PARIS = ZoneInfo("Europe/Paris")
TODAY = datetime.now(PARIS).date()
# Les passages proactifs se font « aujourd'hui a 10 h », hors heures calmes.
MORNING = datetime.combine(TODAY, time(10, 0), PARIS)
SECRET = "cle-acceptance"

CLAIRE = "whatsapp:+33611111111"
THOMAS = "whatsapp:+33622222222"
ALICE = "whatsapp:+33633333333"
BOB = "whatsapp:+33644444444"


class ScriptedBrain:
    """Doublure du modele : chaque adresse recoit les reponses prevues."""

    def __init__(self):
        self.next: dict[str, Understanding] = {}
        self.ingestions: dict[str, Ingestion] = {}
        self.contexts: dict[str, list] = {}

    def understand(self, context, text, media):
        self.contexts.setdefault(context.member_name or "?", []).append(context)
        return self._pending.pop(0) if self._pending else Understanding(reply="Noté.")

    def ingest(self, context, document, attachments=None):
        return self.ingestions.get(document.external_id, Ingestion(relevant=False))

    _pending: list[Understanding] = []


@pytest.fixture
def world(tmp_path):
    settings = replace(
        make_settings(tmp_path), google_client_id="id", google_client_secret="secret", secret_key=SECRET
    )
    sessions = make_session_factory(settings.database_url)
    brain, sender, google = ScriptedBrain(), FakeSender(), FakeGoogle()
    brain._pending = []
    media_store: dict[str, Media] = {}
    client = TestClient(
        create_app(
            settings, brain, sender, sessions, google, run_background=False,
            media_downloader=lambda url, content_type: media_store.get(url),
        ),
        follow_redirects=False,
    )

    class World:
        pass

    w = World()
    w.client, w.brain, w.sender, w.sessions, w.google, w.media = client, brain, sender, sessions, google, media_store

    def say(address, text, understanding=None, media_url=None, media_type=None):
        if understanding is not None:
            brain._pending.append(understanding)
        data = {"From": address, "Body": text, "NumMedia": "0"}
        if media_url:
            data.update({"NumMedia": "1", "MediaUrl0": media_url, "MediaContentType0": media_type})
        assert client.post(WEBHOOK_PATH, data=data).status_code == 200
        return [body for to, body in sender.sent if to == address][-1]

    def member(address) -> Member:
        with sessions() as session:
            return session.scalar(select(Member).where(Member.address == address))

    def connect(address) -> int:
        state = sign_state(SECRET, member(address).id)
        assert client.get(GOOGLE_CALLBACK_PATH, params={"state": state, "code": "abc"}).status_code == 200
        with sessions() as session:
            from app.models import Connection

            return session.scalar(select(Connection.id).where(Connection.member_id == member(address).id))

    def sync(connection_id):
        return sync_connection(sessions, connection_id, google, brain, sender, SECRET, now=MORNING)

    def messages_to(address):
        return [body for to, body in sender.sent if to == address]

    def invite_into_household(host, guest, guest_name):
        reply = say(host, f"invite {guest_name}", Understanding(reply="Voici le lien.", invite_name=guest_name))
        code = re.search(r"REJOINDRE%20([A-Z0-9]{6})", reply).group(1)
        welcome = say(guest, f"REJOINDRE {code}")
        assert welcome.startswith(f"Bienvenue {guest_name}")
        return code

    w.say, w.member, w.connect, w.sync, w.messages_to, w.invite = say, member, connect, sync, messages_to, invite_into_household
    return w


def mail(external_id, subject="Mail", attachments=None) -> SourceDocument:
    return SourceDocument("gmail", external_id, "expediteur@exemple.fr", subject,
                          datetime.combine(TODAY, time(8, 0), PARIS), "contenu du mail", attachments or [])


# --- Test 1 et 6 : sortie scolaire, partage entre parents -------------------------


def test_1_and_6_school_email_becomes_a_shared_process_accepted_and_finished_by_the_other_parent(world):
    world.say(CLAIRE, "Bonjour", Understanding(reply="Bonjour Claire !", member_name="Claire",
                                               household_kind="famille", people=[{"name": "Léa", "relation": "enfant"}]))
    world.invite(CLAIRE, THOMAS, "Thomas")

    # Test 1 : le mail de l'ecole arrive dans la boite de Claire.
    world.google.documents = {"ecole1": mail("ecole1", "Sortie au zoo du CE2")}
    world.brain.ingestions["ecole1"] = Ingestion(
        relevant=True, label="Mail de l'école du 16/11", notify="recap",
        signals=[Signal(
            type="school_event", title="Sortie au zoo", person="Léa", organization="École Jules Ferry",
            event_date=TODAY + timedelta(days=5), due_date=TODAY + timedelta(days=3), amount_eur=8.0,
            actions=["Signer l'autorisation de sortie", "Prévoir un pique-nique"],
            responsible="Thomas", confidence=0.9,
        )],
    )
    world.connect(CLAIRE)

    with world.sessions() as session:
        wf = session.scalar(select(Workflow))
        thomas = session.scalar(select(Member).where(Member.address == THOMAS))
        assert wf.template == "school_event"
        assert session.get(Person, wf.person_id).name == "Léa"
        assert wf.event_date == TODAY + timedelta(days=5) and wf.due_date == TODAY + timedelta(days=3)
        assert [s.title for s in wf.steps] == ["Signer l'autorisation de sortie", "Prévoir un pique-nique", "Payer 8 €"]
        # Le parent responsable est identifie, mais rien ne lui est montre
        # tant que Claire n'a pas accepte de partager.
        assert wf.data["suggested_responsible_id"] == thomas.id and wf.visibility == "private"
        assert build_context(session, thomas, None, "Europe/Paris").workflow_lines == []
        workflow_id = wf.id

    world.sender.sent.clear()
    tick(world.sessions, world.sender, now=MORNING)
    suggestion = world.messages_to(CLAIRE)[0]
    assert "je propose à Thomas" in suggestion and "sans ton accord" in suggestion
    assert world.messages_to(THOMAS) == []

    world.say(CLAIRE, "Oui, propose-le à Thomas",
              Understanding(reply="Je lui propose.", assign=[(workflow_id, "Thomas")]))
    ask = world.messages_to(THOMAS)[-1]
    assert "Claire te propose" in ask and "Léa" in ask and "Réponds oui ou non" in ask

    # Test 6 : Thomas accepte, puis fait les trois etapes.
    with world.sessions() as session:
        assignment = session.scalar(select(Assignment))
        thomas = session.get(Member, assignment.member_id)
        context = build_context(session, thomas, None, "Europe/Paris")
        assert any(f"[R{assignment.id}]" in line for line in context.assignment_lines)
        step_keys = [s.key for s in session.get(Workflow, workflow_id).steps]

    world.say(THOMAS, "oui", Understanding(reply="Super, merci !", accept_ids=[assignment.id]))
    assert world.messages_to(CLAIRE)[-1] == "Thomas s'occupe de : Sortie au zoo. Tu n'as plus à y penser."
    with world.sessions() as session:
        assert session.get(Workflow, workflow_id).responsible_member_id == thomas.id
        assert session.get(Assignment, assignment.id).status == "accepted"

    world.say(THOMAS, "C'est fait : signé, payé et pique-nique prévu",
              Understanding(reply="Parfait.", completed_steps=[(workflow_id, key) for key in step_keys]))
    with world.sessions() as session:
        wf = session.get(Workflow, workflow_id)
        assert wf.status == "resolved"
        assert all(s.done_by == "user" and s.done_by_member_id == thomas.id for s in wf.steps)
        assert session.get(Assignment, assignment.id).status == "done"
    assert world.messages_to(CLAIRE)[-1] == "C'est réglé : Sortie au zoo (Thomas s'en est occupé)."


# --- Test 2 : attestation d'assurance demandee -------------------------------


def test_2_student_insurance_request_finds_the_document_and_prepares_the_reply(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut Alice !", member_name="Alice", household_kind="seul"))
    world.media["https://api.twilio.test/ME1"] = Media("image/jpeg", b"photo-attestation-maif")
    world.say(
        ALICE, "voici mon attestation d'assurance",
        Understanding(reply="Je la garde.", documents=[DocumentMeta(
            0, "attestation_assurance", "Attestation d'assurance habitation", issuer="MAIF",
            valid_until=TODAY + timedelta(days=300))]),
        media_url="https://api.twilio.test/ME1", media_type="image/jpeg",
    )

    world.say(ALICE, "Studapart me demande mon attestation d'assurance",
              Understanding(reply="Je m'en occupe.", signals=[Signal(
                  type="document_request", title="Attestation demandée par Studapart",
                  organization="Studapart", requested_document="attestation_assurance", confidence=0.9)]))
    with world.sessions() as session:
        wf = session.scalar(select(Workflow))
        doc = session.scalar(select(Document))
        approval = session.scalar(select(Approval))
        assert [(s.key, s.status) for s in wf.steps] == [("find", "done"), ("send", "awaiting_approval")]
        assert wf.steps[0].evidence == f"document:{doc.id}" and wf.status == "waiting_approval"
        assert "attestation d'assurance habitation" in approval.draft and "Alice" in approval.draft
        assert doc.attachment_ref == "https://api.twilio.test/ME1"

    reply = world.say(ALICE, "oui", Understanding(reply="Voilà.", approve_ids=[approval.id]))
    assert "Voici le texte, prêt à envoyer" in reply and "Dis-moi quand c'est envoyé." in reply
    link = re.search(r"https://mumu\.example(/documents/\S+)", reply).group(1)
    opened = world.client.get(link)
    assert opened.status_code == 200 and opened.content == b"photo-attestation-maif"
    with world.sessions() as session:
        # Approuve n'est pas envoye : l'etape reste ouverte.
        assert session.get(Workflow, wf.id).status == "active"

    world.say(ALICE, "c'est envoyé", Understanding(reply="Top.", completed_steps=[(wf.id, "send")]))
    with world.sessions() as session:
        assert session.get(Workflow, wf.id).status == "resolved"


def test_2b_expired_insurance_blocks_with_an_explanation(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut", member_name="Alice"))
    world.media["https://api.twilio.test/ME2"] = Media("application/pdf", b"%PDF-ancienne")
    world.say(ALICE, "mon attestation", Understanding(reply="Gardée.", documents=[DocumentMeta(
        0, "attestation_assurance", "Attestation 2025", issuer="MAIF", valid_until=TODAY - timedelta(days=10))]),
        media_url="https://api.twilio.test/ME2", media_type="application/pdf")
    world.say(ALICE, "la fac veut mon attestation", Understanding(reply="Je regarde.", signals=[Signal(
        type="document_request", title="Attestation pour la fac", organization="Université",
        requested_document="attestation_assurance", confidence=0.9)]))
    with world.sessions() as session:
        wf = session.scalar(select(Workflow))
        assert wf.status == "blocked" and "a expiré le" in wf.blocker and "MAIF" in wf.blocker
        assert session.scalar(select(Approval)) is None


# --- Test 3 : hausse d'un abonnement --------------------------------------------


def test_3_subscription_increase_is_detected_from_history_and_explained_once(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut", member_name="Alice"))
    world.google.documents = {"sp1": mail("sp1"), "sp2": mail("sp2")}
    for ref, amount, day in (("sp1", 10.99, TODAY - timedelta(days=31)), ("sp2", 12.99, TODAY - timedelta(days=1))):
        world.brain.ingestions[ref] = Ingestion(relevant=True, label=f"Reçu Spotify {ref}", signals=[Signal(
            type="subscription_charge", title="Spotify Premium", organization="Spotify",
            amount_eur=amount, event_date=day, category="abonnement", confidence=0.95)])
    world.connect(ALICE)

    with world.sessions() as session:
        alice = session.scalar(select(Member).where(Member.address == ALICE))
        changes = price_changes(session, alice)
        assert len(changes) == 1
        assert changes[0].explain() == (
            f"Spotify est passé de 10,99 € ({(TODAY - timedelta(days=31)).isoformat()}) "
            f"à 12,99 € ({(TODAY - timedelta(days=1)).isoformat()}), soit 2 € de plus (+18%)."
        )
        assert any("Hausse" in line for line in build_context(session, alice, None, "Europe/Paris").money_lines)

    world.sender.sent.clear()
    tick(world.sessions, world.sender, now=MORNING)
    tick(world.sessions, world.sender, now=MORNING)
    alerts = world.messages_to(ALICE)
    assert len(alerts) == 1 and "Spotify est passé de 10,99 €" in alerts[0]


# --- Test 4 : retour d'un produit -------------------------------------------------


def test_4_return_is_linked_to_the_order_followed_up_when_late_and_closed_by_the_refund(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut", member_name="Alice"))
    returned_on = TODAY - timedelta(days=20)
    world.google.documents = {"cmd": mail("cmd"), "ret": mail("ret")}
    world.brain.ingestions["cmd"] = Ingestion(relevant=True, label="Commande Zalando", signals=[Signal(
        type="order", title="Baskets", product="Baskets blanches", organization="Zalando",
        reference="402-1", amount_eur=59.9, event_date=TODAY - timedelta(days=30), confidence=0.95)])
    world.brain.ingestions["ret"] = Ingestion(relevant=True, label="Retour Zalando", signals=[Signal(
        type="return_started", title="Retour des baskets", organization="Zalando",
        reference="402-1", event_date=returned_on, confidence=0.95)])
    connection_id = world.connect(ALICE)

    with world.sessions() as session:
        purchase = session.scalar(select(Purchase))
        wf = session.scalar(select(Workflow))
        assert purchase.status == "returned" and purchase.order_ref == "402-1"
        assert wf.template == "refund" and wf.data["purchase_id"] == purchase.id
        assert wf.amount_eur == 59.9 and wf.due_date == returned_on + timedelta(days=14)
        assert wf.data["refund_date_is_estimate"] is True
        assert [(s.key, s.status) for s in wf.steps] == [("return", "done"), ("refund", "todo"), ("followup", "skipped")]

    world.sender.sent.clear()
    tick(world.sessions, world.sender, now=MORNING)
    followup = world.messages_to(ALICE)[0]
    assert "aurait dû arriver" in followup and "(date estimée)" in followup
    assert "J'ai renvoyé ma commande n° 402-1" in followup and "Réponds oui" in followup
    with world.sessions() as session:
        assert session.scalar(select(Approval)).status == "pending"
        assert session.get(Workflow, wf.id).status == "waiting_approval"

    world.google.documents["rbt"] = mail("rbt")
    world.brain.ingestions["rbt"] = Ingestion(relevant=True, label="Remboursement Zalando", signals=[Signal(
        type="refund_received", title="Remboursement", organization="Zalando", reference="402-1", confidence=0.95)])
    world.sync(connection_id)
    with world.sessions() as session:
        wf = session.get(Workflow, wf.id)
        assert wf.status == "resolved"
        assert session.scalar(select(Approval)).status == "rejected"
        assert session.scalar(select(Purchase)).status == "refunded"


# --- Test 5 : courrier administratif ------------------------------------------------


def test_5_administrative_letter_is_explained_and_tracked_with_its_documents(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut", member_name="Alice"))
    world.media["https://api.twilio.test/ATT"] = Media("application/pdf", b"%PDF-attestation")
    world.say(ALICE, "mon attestation", Understanding(reply="Gardée.", documents=[DocumentMeta(
        0, "attestation_assurance", "Attestation MAIF", issuer="MAIF", valid_until=TODAY + timedelta(days=200))]),
        media_url="https://api.twilio.test/ATT", media_type="application/pdf")

    world.media["https://api.twilio.test/CAF"] = Media("image/jpeg", b"photo-courrier-caf")
    summary = "La CAF a besoin de deux justificatifs pour continuer à verser ton aide au logement."
    world.say(ALICE, "j'ai reçu ça de la CAF", Understanding(reply="Je t'explique.", signals=[Signal(
        type="admin_letter", title="Justificatifs pour l'aide au logement", organization="CAF",
        reference="DOS-7781", summary=summary, due_date=TODAY + timedelta(days=15),
        required_documents=[{"label": "Quittance de loyer d'octobre", "kind": "autre"},
                            {"label": "Attestation d'assurance habitation", "kind": "attestation_assurance"}],
        confidence=0.9)]), media_url="https://api.twilio.test/CAF", media_type="image/jpeg")

    with world.sessions() as session:
        wf = session.scalar(select(Workflow))
        assert wf.template == "admin" and wf.summary == summary and wf.reference == "DOS-7781"
        steps = [(s.title, s.status, s.done_by) for s in wf.steps]
        assert steps == [
            ("Réunir : Quittance de loyer d'octobre", "todo", None),
            ("Réunir : Attestation d'assurance habitation", "done", "mumu"),
            ("Préparer la réponse à CAF", "todo", None),
            ("Envoyer la réponse à CAF", "todo", None),
            ("Réponse de CAF", "todo", None),
        ]
        assert wf.status == "active" and wf.next_action == "Réunir : Quittance de loyer d'octobre"
        alice = session.scalar(select(Member).where(Member.address == ALICE))
        line = next(l for l in build_context(session, alice, None, "Europe/Paris").workflow_lines if "CAF" in l)
        assert summary in line and "Message du" in line
        first_key = wf.steps[0].key

    world.say(ALICE, "j'ai la quittance", Understanding(reply="Parfait.", completed_steps=[(wf.id, first_key)]))
    with world.sessions() as session:
        wf = session.get(Workflow, wf.id)
        approval = session.scalar(select(Approval))
        assert wf.status == "waiting_approval" and approval.step_key == "prepare"
        assert "Quittance de loyer d'octobre" in approval.draft and "DOS-7781" in approval.draft


# --- Test 7 : conflit d'agenda ------------------------------------------------------


def test_7_calendar_conflict_is_detected_with_a_free_slot_suggested_once(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut", member_name="Alice"))
    tomorrow = TODAY + timedelta(days=1)
    at = lambda h, m: datetime.combine(tomorrow, time(h, m), PARIS)
    world.google.events = [
        Event("meet", "Réunion d'équipe", at(16, 30), at(17, 15)),
        Event("dent", "Dentiste", at(17, 30), at(18, 0)),
    ]
    world.connect(ALICE)

    with world.sessions() as session:
        alice = session.scalar(select(Member).where(Member.address == ALICE))
        lines = build_context(session, alice, None, "Europe/Paris").calendar_lines
        assert any(l.startswith("! « Réunion d'équipe » et « Dentiste » s'enchaînent sans marge") and "08:00-08:30" in l
                   for l in lines)

    world.sender.sent.clear()
    tick(world.sessions, world.sender, now=MORNING)
    tick(world.sessions, world.sender, now=MORNING)
    alerts = world.messages_to(ALICE)
    assert len(alerts) == 1
    assert "« Réunion d'équipe » et « Dentiste » s'enchaînent sans marge" in alerts[0]
    assert "libre de 08:00 à 08:30" in alerts[0]


# --- Test 8 : confidentialite dans une coloc ---------------------------------


def test_8_private_email_is_never_exposed_to_a_flatmate_even_when_the_model_tries(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut", member_name="Alice", household_kind="coloc"))
    world.invite(ALICE, BOB, "Bob")
    world.google.documents = {"banque": mail("banque", attachments=[AttachmentRef("att1", "releve.pdf", "application/pdf", 10)])}
    world.brain.ingestions["banque"] = Ingestion(
        relevant=True, label="Mail de la banque",
        documents=[DocumentMeta(0, "releve_bancaire", "Relevé de novembre", issuer="Banque")],
        signals=[Signal(type="admin_letter", title="Découvert à régulariser", organization="Banque",
                        summary="Ton compte est à découvert de 450 €.", due_date=TODAY + timedelta(days=5),
                        actions=["Régulariser le découvert"], confidence=0.9)],
        facts=["A un découvert autorisé de 500 €"],
    )
    world.connect(ALICE)

    with world.sessions() as session:
        wf = session.scalar(select(Workflow))
        doc = session.scalar(select(Document))
        bob = session.scalar(select(Member).where(Member.address == BOB))
        alice = session.scalar(select(Member).where(Member.address == ALICE))
        context = build_context(session, bob, None, "Europe/Paris")
        everything_bob_sees = " ".join(
            context.workflow_lines + context.agenda_lines + context.document_lines + context.money_lines
            + context.facts + [body for _, body in context.recent_messages]
        )
        assert "450" not in everything_bob_sees and "Banque" not in everything_bob_sees
        assert context.workflow_lines == [] and context.document_lines == []
        # Un lien vers le document d'Alice signe pour Bob ne s'ouvre pas.
        forged = sign_payload(SECRET, {"d": doc.id, "m": bob.id}, 60, "document")
        assert resolve_link(session, SECRET, forged) is None
        assert any("Découvert" in line for line in build_context(session, alice, None, "Europe/Paris").workflow_lines)

    # Le modele, mal guide, tente d'agir sur le processus prive d'Alice.
    reply = world.say(BOB, "c'est quoi les soucis d'Alice ?", Understanding(
        reply="Je ne peux pas te le dire.",
        completed_steps=[(wf.id, None)], close_workflow_ids=[wf.id], assign=[(wf.id, "Bob")],
        send_document_ids=[doc.id],
    ))
    assert "/documents/" not in reply and "450" not in reply
    with world.sessions() as session:
        wf = session.get(Workflow, wf.id)
        assert wf.status == "active" and wf.visibility == "private"
        assert all(s.status == "todo" for s in wf.steps if s.key.startswith("action"))
        assert session.scalar(select(Assignment)) is None


# --- Test 9 : une echeance, une seule intervention -----------------------------------


def test_9_upcoming_deadline_triggers_exactly_one_message(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut", member_name="Alice"))
    world.google.documents = {"edf": mail("edf")}
    world.brain.ingestions["edf"] = Ingestion(relevant=True, label="Facture EDF", notify="recap", signals=[Signal(
        type="invoice", title="Facture EDF", organization="EDF", amount_eur=86.0,
        due_date=TODAY + timedelta(days=1), confidence=0.95)])
    world.connect(ALICE)
    world.sender.sent.clear()

    for _ in range(3):
        tick(world.sessions, world.sender, now=MORNING)
    assert world.messages_to(ALICE) == ["Petit rappel : Payer 86 € à EDF, c'est pour demain."]


# --- Test 10 : « qu'est-ce que je dois faire cette semaine ? » ----------------


def test_10_week_summary_is_prioritised_from_real_data_and_given_to_the_model(world):
    world.say(ALICE, "Salut", Understanding(reply="Salut", member_name="Alice"))
    world.media["https://api.twilio.test/ATT"] = Media("application/pdf", b"%PDF-att")
    world.say(ALICE, "attestation", Understanding(reply="Ok", documents=[DocumentMeta(
        0, "attestation_assurance", "Attestation MAIF", valid_until=TODAY + timedelta(days=100))]),
        media_url="https://api.twilio.test/ATT", media_type="application/pdf")
    world.say(ALICE, "plein de choses", Understanding(
        reply="Noté.",
        items=[{"kind": "rendez_vous", "title": "Rendez-vous chez le médecin", "due_date": TODAY + timedelta(days=2),
                "amount_eur": None, "owner": None, "confidence": 0.9}],
        signals=[
            Signal(type="invoice", title="Loyer de novembre", organization="Propriétaire", amount_eur=520.0,
                   due_date=TODAY - timedelta(days=2), confidence=0.95),
            Signal(type="document_request", title="Attestation pour la résidence", organization="Résidence",
                   requested_document="attestation_assurance", confidence=0.9),
            Signal(type="invoice", title="Facture box", organization="Free", amount_eur=29.99,
                   due_date=TODAY + timedelta(days=6), confidence=0.9),
        ],
    ))

    with world.sessions() as session:
        alice = session.scalar(select(Member).where(Member.address == ALICE))
        entries = week_overview(session, alice, TODAY)
        kinds = [e.kind for e in entries]
        assert kinds[:4] == ["overdue", "approval", "item", "due"]
        assert "Loyer de novembre" in entries[0].text and "En retard" in entries[0].text
        assert "Attestation pour la résidence" in entries[1].text
        assert "Rendez-vous chez le médecin" in entries[2].text
        assert "Facture box" in entries[3].text

    world.say(ALICE, "Qu'est-ce que je dois faire cette semaine ?", Understanding(reply="Voici."))
    context = world.brain.contexts["Alice"][-1]
    assert context.agenda_lines[0].startswith("1. En retard depuis le") and "Loyer" in context.agenda_lines[0]
