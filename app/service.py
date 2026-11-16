"""Traitement d'un message entrant, de la reception a la reponse.

Le modele comprend ; ce module controle et applique. Chaque identifiant
renvoye par le modele (processus, accord, proposition, document) est verifie :
il doit appartenir au foyer et etre visible par la personne qui parle.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import household as hh
from .agenda import agenda_lines, week_overview
from .audit import log_action
from .brain import Brain, Context, Media, Understanding
from .calendar_logic import describe_calendar
from .documents import DocumentFile, document_line, store_document, visible_documents
from .messaging import Outbox
from .models import (
    Approval,
    Assignment,
    CalendarEvent,
    Connection,
    Document,
    Fact,
    Household,
    Item,
    Member,
    Message,
    Person,
    Purchase,
    Workflow,
)
from .money import money_lines
from .persona import FALLBACK_REPLY
from .signals import person_for, route_signals
from .text import eur
from .workflows import engine
from .workflows.engine import SourceRef

log = logging.getLogger("mumu.service")

RECENT_MESSAGES = 10
MAX_FACTS_IN_CONTEXT = 60
MAX_PURCHASES_IN_CONTEXT = 15
MAX_WORKFLOWS_IN_CONTEXT = 25

CONNECT_UNAVAILABLE = (
    "La connexion à ta boîte mail n'est pas encore ouverte, je te préviens dès que c'est possible. "
    "En attendant, transfère-moi ce que tu veux que je regarde."
)
NOTHING_TO_DISCONNECT = "Je n'ai aucun accès à ta boîte mail ni à ton agenda, il n'y a rien à retirer."
STATUS_LABELS = {
    "active": "en cours",
    "waiting_approval": "attend un accord",
    "waiting_other": "en attente",
    "blocked": "bloqué",
}


@dataclass
class Services:
    """Ce que le service peut faire au-dela de la base."""

    connect_link: Callable[[int], str | None] | None = None
    disconnect: Callable[[int], bool] | None = None
    document_link: Callable[[Document, Member], str | None] | None = None
    whatsapp_from: str = ""
    # Envoi aux autres membres du foyer (propositions, confirmations).
    send: Callable[[str, str], None] | None = None


@dataclass
class Inbound:
    address: str
    text: str
    media: Media | None = None
    # Ou retrouver la piece jointe plus tard (URL du media chez Twilio).
    media_ref: str | None = None


def get_or_create_member(session: Session, address: str, text: str = "") -> tuple[Member, bool]:
    """Trouve le membre, fait rejoindre un foyer avec un code, ou ouvre un foyer.

    Renvoie (membre, vient de rejoindre un foyer existant).
    """
    member = session.scalar(select(Member).where(Member.address == address))
    if member is not None:
        return member, False
    joined = hh.try_join(session, address, text)
    if joined is not None:
        return joined, True
    household = Household()
    session.add(household)
    session.flush()
    member = Member(household_id=household.id, address=address)
    session.add(member)
    session.flush()
    return member, False


def visible_to(model, member: Member):
    """Filtre de confidentialite : le foyer voit ce qui est partage, et chacun
    voit en plus ce qui lui appartient."""
    return or_(model.visibility == "household", model.member_id == member.id)


def _who(session: Session, member_id: int | None, viewer: Member) -> str:
    if member_id is None or member_id == viewer.id:
        return "toi"
    return hh.display_name(session.get(Member, member_id))


def workflow_line(session: Session, wf: Workflow, viewer: Member) -> str:
    parts = [f"- [W{wf.id}] {wf.title} ({STATUS_LABELS.get(wf.status, wf.status)})"]
    if wf.person_id:
        person = session.get(Person, wf.person_id)
        if person:
            parts.append(f"pour {person.name}")
    if wf.amount_eur:
        parts.append(eur(wf.amount_eur))
    if wf.due_date:
        parts.append(f"avant le {wf.due_date.isoformat()}")
    if wf.event_date:
        parts.append(f"le {wf.event_date.isoformat()}")
    step = engine.current_step(wf)
    if step:
        who = "MUMU" if step.owner == "mumu" else (
            wf.organization or "organisme" if step.owner == "evidence"
            else _who(session, wf.responsible_member_id or wf.owner_member_id, viewer)
        )
        parts.append(f"suite : {step.title} ({who})")
    steps = ", ".join(f"{s.key}={s.status}" for s in wf.steps if s.status != "skipped")
    parts.append(f"étapes : {steps}")
    if wf.blocker:
        parts.append(f"blocage : {wf.blocker}")
    # Le resume et les sources ne sont montres qu'au proprietaire.
    if wf.owner_member_id == viewer.id:
        if wf.summary:
            parts.append(f"résumé : {wf.summary}")
        labels = [s.label for s in wf.sources if s.label]
        if labels:
            parts.append(f"sources : {', '.join(labels[:3])}")
        suggested = (wf.data or {}).get("suggested_responsible_id")
        if suggested:
            parts.append(f"responsable suggéré : {_who(session, suggested, viewer)} (rien ne lui a été transmis)")
    else:
        parts.append(f"partagé par {_who(session, wf.owner_member_id, viewer)}")
    return " — ".join(parts)


def _purchase_line(purchase: Purchase) -> str:
    parts = [f"- {purchase.merchant} : {purchase.description}"]
    if purchase.amount_eur is not None:
        parts.append(eur(purchase.amount_eur))
    if purchase.order_ref:
        parts.append(f"commande {purchase.order_ref}")
    if purchase.ordered_on:
        parts.append(f"commandé le {purchase.ordered_on.isoformat()}")
    if purchase.status != "ordered":
        parts.append({"delivered": "livré", "returned": "renvoyé", "refunded": "remboursé"}[purchase.status])
    elif purchase.delivery_expected:
        parts.append(f"livraison prévue le {purchase.delivery_expected.isoformat()}")
    estimate = " (délai légal par défaut)" if purchase.dates_are_estimates else ""
    if purchase.return_deadline and purchase.status in ("ordered", "delivered"):
        parts.append(f"retour possible jusqu'au {purchase.return_deadline.isoformat()}{estimate}")
    if purchase.warranty_until:
        parts.append(f"garantie jusqu'au {purchase.warranty_until.isoformat()}{estimate}")
    return ", ".join(parts)


def build_context(
    session: Session, member: Member, before_message_id: int | None, timezone: str
) -> Context:
    household_id = member.household_id
    today = datetime.now(ZoneInfo(timezone)).date()
    facts = session.scalars(
        select(Fact.text)
        .where(Fact.household_id == household_id, visible_to(Fact, member))
        .order_by(Fact.id.desc())
        .limit(MAX_FACTS_IN_CONTEXT)
    ).all()
    open_items = session.execute(
        select(Item.id, Item.title, Item.due_date)
        .where(Item.household_id == household_id, Item.status == "open", visible_to(Item, member))
        .order_by(Item.due_date.is_(None), Item.due_date, Item.id)
    ).all()
    # Les echanges des autres membres ne sont jamais montres.
    recent_query = select(Message.direction, Message.body).where(Message.member_id == member.id)
    if before_message_id is not None:
        recent_query = recent_query.where(Message.id < before_message_id)
    recent = session.execute(recent_query.order_by(Message.id.desc()).limit(RECENT_MESSAGES)).all()

    events = session.scalars(select(CalendarEvent).where(CalendarEvent.member_id == member.id)).all()
    purchases = session.scalars(
        select(Purchase)
        .where(Purchase.member_id == member.id)
        .order_by(Purchase.id.desc())
        .limit(MAX_PURCHASES_IN_CONTEXT)
    ).all()
    connected = session.scalar(
        select(func.count())
        .select_from(Connection)
        .where(Connection.member_id == member.id, Connection.status == "active")
    )
    workflows = hh.visible_workflows(session, member)[:MAX_WORKFLOWS_IN_CONTEXT]
    approvals = session.scalars(
        select(Approval).where(Approval.member_id == member.id, Approval.status == "pending")
    ).all()
    approval_lines = []
    for approval in approvals:
        wf = session.get(Workflow, approval.workflow_id)
        approval_lines.append(f"- [A{approval.id}] pour [W{wf.id}] {wf.title} : brouillon « {approval.draft[:400]} »")
    household = session.get(Household, household_id)
    people = session.scalars(select(Person).where(Person.household_id == household_id)).all()
    return Context(
        today=today,
        member_name=member.name,
        facts=list(reversed(facts)),
        open_items=[(row.id, row.title, row.due_date) for row in open_items],
        recent_messages=[(row.direction, row.body) for row in reversed(recent)],
        calendar_lines=describe_calendar(list(events), timezone),
        purchase_lines=[_purchase_line(p) for p in purchases],
        google_connected=bool(connected),
        household_kind=household.kind if household else None,
        member_lines=[f"- {hh.display_name(m)}" for m in hh.other_members(session, member)],
        people_lines=[f"- {p.name} ({p.relation})" for p in people],
        workflow_lines=[workflow_line(session, wf, member) for wf in workflows],
        approval_lines=approval_lines,
        assignment_lines=[hh.assignment_line(session, a) for a in hh.pending_for(session, member)],
        document_lines=[document_line(d, today) for d in visible_documents(session, member)],
        money_lines=money_lines(session, member, today),
        agenda_lines=agenda_lines(week_overview(session, member, today, timezone=timezone)),
    )


def _apply(
    session: Session,
    member: Member,
    inbound_message: Message,
    inbound: Inbound,
    understanding: Understanding,
    services: Services,
    outbox: Outbox,
    today,
) -> list[str]:
    """Applique ce que le modele a compris. Renvoie les lignes a ajouter a la
    reponse (textes prets, liens, informations que la personne doit avoir)."""
    extras: list[str] = []
    household_id = member.household_id

    if understanding.member_name and not member.name:
        member.name = understanding.member_name
    if understanding.household_kind:
        household = session.get(Household, household_id)
        if household and household.kind != understanding.household_kind:
            household.kind = understanding.household_kind
    for person in understanding.people:
        person_for(session, household_id, person["name"])

    for item in understanding.items:
        session.add(
            Item(
                household_id=household_id, source_message_id=inbound_message.id, member_id=member.id,
                # La liste de courses est commune ; le reste est personnel.
                visibility="household" if item["kind"] == "course" else "private",
                **item,
            )
        )
    for fact in understanding.facts:
        session.add(
            Fact(
                household_id=household_id, source_message_id=inbound_message.id, member_id=member.id,
                visibility="private", text=fact,
            )
        )
    if understanding.done_item_ids:
        for done in session.scalars(
            select(Item).where(
                Item.household_id == household_id,
                Item.id.in_(understanding.done_item_ids),
                visible_to(Item, member),
            )
        ):
            done.status = "done"

    # Documents envoyes en photo ou en PDF.
    if inbound.media is not None:
        for meta in understanding.documents:
            if meta.index != 0:
                continue
            store_document(
                session, member,
                {"kind": meta.kind, "title": meta.title, "issuer": meta.issuer, "issued_on": meta.issued_on,
                 "valid_until": meta.valid_until, "summary": meta.summary},
                DocumentFile(
                    data=inbound.media.data, mime_type=inbound.media.content_type, filename=inbound.media.filename,
                    source_kind="whatsapp", source_ref=f"message:{inbound_message.id}",
                    attachment_ref=inbound.media_ref,
                ),
            )

    # Situations a suivre.
    source = SourceRef(
        "whatsapp", f"message:{inbound_message.id}", f"Message du {today.strftime('%d/%m')}"
    )
    routed = route_signals(session, member, understanding.signals, source, today)
    for wf, other in routed.to_assign:
        _, delivered = hh.propose(session, wf, other, member, outbox)
        if not delivered:
            extras.append(_not_delivered(other))

    for approval_id, approved in [(i, True) for i in understanding.approve_ids] + [
        (i, False) for i in understanding.reject_ids
    ]:
        approval = session.get(Approval, approval_id)
        if approval is None or approval.member_id != member.id or approval.status != "pending":
            continue
        wf = engine.decide(session, approval, approved, member, today)
        if approved:
            extras.append(f"Voici le texte, prêt à envoyer :\n\n{approval.draft}")
            doc_id = (wf.data or {}).get("document_id")
            doc = session.get(Document, doc_id) if doc_id else None
            if doc and doc.member_id == member.id and services.document_link:
                link = services.document_link(doc, member)
                if link:
                    extras.append(f"Le document à joindre ({doc.title}) : {link}")
            extras.append("Dis-moi quand c'est envoyé.")

    for workflow_id, step_key in understanding.completed_steps:
        wf = session.get(Workflow, workflow_id)
        if wf is None or not hh.can_see(session, wf, member):
            continue
        if engine.complete_step(session, wf, step_key, by="user", member_id=member.id, today=today):
            hh.after_step_done(session, wf, member, outbox)

    for workflow_id in understanding.close_workflow_ids:
        wf = session.get(Workflow, workflow_id)
        if wf is None or wf.household_id != household_id:
            continue
        if member.id in (wf.owner_member_id, wf.responsible_member_id):
            engine.close(session, wf, member)

    others = {hh.display_name(m).lower(): m for m in hh.other_members(session, member)}
    for workflow_id, name in understanding.assign:
        wf = session.get(Workflow, workflow_id)
        if wf is None or not hh.can_see(session, wf, member):
            continue
        if wf.owner_member_id != member.id and wf.visibility != "household":
            continue
        other = others.get(name.lower())
        if other is None:
            extras.append(f"Je ne connais pas encore {name} dans le foyer. Tu veux que je l'invite ?")
            continue
        _, delivered = hh.propose(session, wf, other, member, outbox)
        if not delivered:
            extras.append(_not_delivered(other))

    for assignment_id, accepted in [(i, True) for i in understanding.accept_ids] + [
        (i, False) for i in understanding.decline_ids
    ]:
        assignment = session.get(Assignment, assignment_id)
        if assignment is None or assignment.member_id != member.id or assignment.status != "proposed":
            continue
        hh.respond(session, assignment, accepted, member, outbox, today)

    if understanding.invite_name:
        invite = hh.create_invite(session, member, understanding.invite_name)
        extras.append(hh.invite_text(invite, services.whatsapp_from))

    for doc_id in understanding.send_document_ids:
        doc = session.get(Document, doc_id)
        if doc is None or doc.member_id != member.id:
            continue
        link = services.document_link(doc, member) if services.document_link else None
        if link:
            log_action(session, "document_link_sent", household_id=household_id, member_id=member.id,
                       target=f"document:{doc.id}")
            extras.append(f"{doc.title} : {link} (lien valable une heure)")
        else:
            extras.append(f"Je retrouve « {doc.title} », mais je ne peux pas encore te renvoyer le fichier.")
    return extras


def _not_delivered(other: Member) -> str:
    return (
        f"{hh.display_name(other)} ne m'a pas écrit depuis plus de 24 h, et WhatsApp m'empêche de lui "
        "écrire en premier. Je lui transmets dès qu'il ou elle m'écrit."
    )


def handle_incoming(
    session_factory: sessionmaker,
    brain: Brain,
    address: str,
    text: str,
    media: Media | None,
    timezone: str = "Europe/Paris",
    services: Services | None = None,
    media_ref: str | None = None,
    external_id: str | None = None,
) -> str | None:
    """Enregistre le message, le fait comprendre, applique le resultat.

    Renvoie le texte a envoyer au membre, ou None si ce message a deja ete
    traite (meme `external_id`). Les messages destines a d'autres membres
    partent apres l'enregistrement en base.
    """
    services = services or Services()
    inbound = Inbound(address, text, media, media_ref)
    outbox = Outbox()
    with session_factory() as session:
        if external_id and session.scalar(select(Message.id).where(Message.external_id == external_id)):
            log.info("Message %s deja traite, ignore", external_id)
            return None
        member, joined = get_or_create_member(session, address, text)
        inbound_message = Message(
            household_id=member.household_id, member_id=member.id, direction="in", body=text,
            media_type=media.content_type if media else None, external_id=external_id,
        )
        session.add(inbound_message)
        try:
            session.flush()
        except IntegrityError:
            # Deux livraisons simultanees du meme message : une seule passe.
            session.rollback()
            log.info("Message %s deja en cours de traitement, ignore", external_id)
            return None

        if joined:
            reply = hh.welcome_text(session, member)
            session.add(Message(household_id=member.household_id, member_id=member.id, direction="out", body=reply))
            session.commit()
            return reply

        # Les propositions en attente sont vues des que la personne ecrit.
        for assignment in hh.pending_for(session, member):
            assignment.delivered = True
        context = build_context(session, member, inbound_message.id, timezone)
        # Le message est garde meme si la suite echoue.
        session.commit()

        action = None
        extras: list[str] = []
        try:
            understanding = brain.understand(context, text, media)
        except Exception:
            log.exception("Comprehension impossible pour le message %s", inbound_message.id)
            reply = FALLBACK_REPLY
        else:
            reply = understanding.reply
            action = understanding.action
            try:
                extras = _apply(session, member, inbound_message, inbound, understanding, services, outbox, context.today)
            except Exception:
                # Rien n'est applique a moitie : on annule et on le dit.
                session.rollback()
                outbox.messages.clear()
                log.exception("Application impossible pour le message %s", inbound_message.id)
                reply = FALLBACK_REPLY
                extras = []

        full_reply = "\n\n".join([reply, *extras]) if extras else reply
        session.add(Message(household_id=member.household_id, member_id=member.id, direction="out", body=full_reply))
        session.commit()
        member_id = member.id

    for to, body in outbox.messages:
        if services.send:
            try:
                services.send(to, body)
            except Exception:
                log.exception("Message non envoyé à %s", to)

    if action == "connect_google":
        link = services.connect_link(member_id) if services.connect_link else None
        full_reply = f"{full_reply}\n{link}" if link else CONNECT_UNAVAILABLE
    elif action == "disconnect_google":
        if not (services.disconnect and services.disconnect(member_id)):
            full_reply = NOTHING_TO_DISCONNECT
    return full_reply
