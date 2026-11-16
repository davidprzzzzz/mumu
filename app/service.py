"""Traitement d'un message entrant, de la reception a la reponse."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .brain import Brain, Context, Media
from .calendar_logic import describe_calendar
from .models import CalendarEvent, Connection, Fact, Household, Item, Member, Message, Purchase, now_utc
from .persona import FALLBACK_REPLY

log = logging.getLogger("mumu.service")

RECENT_MESSAGES = 10
MAX_FACTS_IN_CONTEXT = 60
MAX_PURCHASES_IN_CONTEXT = 15
# WhatsApp n'autorise un message libre que dans les 24 h qui suivent le
# dernier message de la personne. Au-dela, il faut un modele approuve par Meta.
WHATSAPP_WINDOW = timedelta(hours=24)
MAX_PROACTIVE_PER_DAY = 2

CONNECT_UNAVAILABLE = (
    "La connexion à ta boîte mail n'est pas encore ouverte, je te préviens dès que c'est possible. "
    "En attendant, transfère-moi ce que tu veux que je regarde."
)
NOTHING_TO_DISCONNECT = "Je n'ai aucun accès à ta boîte mail ni à ton agenda, il n'y a rien à retirer."


@dataclass
class ConnectionActions:
    """Ce que le service peut faire quand un membre veut connecter un compte."""

    connect_link: Callable[[int], str | None]
    disconnect: Callable[[int], bool]


def get_or_create_member(session: Session, address: str) -> Member:
    """Trouve le membre par son adresse WhatsApp, ou cree un nouveau foyer.

    Pour l'instant, chaque nouvelle adresse ouvre son propre foyer. Rejoindre
    un foyer existant viendra avec le relais entre membres.
    """
    member = session.scalar(select(Member).where(Member.address == address))
    if member is None:
        household = Household()
        session.add(household)
        session.flush()
        member = Member(household_id=household.id, address=address)
        session.add(member)
        session.flush()
    return member


def visible_to(model, member: Member):
    """Filtre de confidentialite : le foyer voit ce qui est partage, et chacun
    voit en plus ce qui vient de ses propres comptes."""
    return or_(model.visibility == "household", model.member_id == member.id)


def build_context(
    session: Session, member: Member, before_message_id: int | None, timezone: str
) -> Context:
    household_id = member.household_id
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
    recent_query = select(Message.direction, Message.body).where(
        # Les echanges des autres membres ne sont pas montres.
        Message.member_id == member.id,
    )
    if before_message_id is not None:
        recent_query = recent_query.where(Message.id < before_message_id)
    recent = session.execute(recent_query.order_by(Message.id.desc()).limit(RECENT_MESSAGES)).all()

    events = session.scalars(
        select(CalendarEvent).where(CalendarEvent.member_id == member.id)
    ).all()
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
    return Context(
        today=datetime.now(ZoneInfo(timezone)).date(),
        member_name=member.name,
        facts=list(reversed(facts)),
        open_items=[(row.id, row.title, row.due_date) for row in open_items],
        recent_messages=[(row.direction, row.body) for row in reversed(recent)],
        calendar_lines=describe_calendar(list(events), timezone),
        purchase_lines=[_purchase_line(p) for p in purchases],
        google_connected=bool(connected),
    )


def _purchase_line(purchase: Purchase) -> str:
    parts = [f"- {purchase.merchant} : {purchase.description}"]
    if purchase.amount_eur is not None:
        parts.append(f"{purchase.amount_eur:.2f} €")
    if purchase.ordered_on:
        parts.append(f"commandé le {purchase.ordered_on.isoformat()}")
    if purchase.delivery_expected:
        parts.append(f"livraison prévue le {purchase.delivery_expected.isoformat()}")
    return ", ".join(parts)


def can_write_first(session: Session, member: Member, now: datetime | None = None) -> bool:
    """MUMU peut-il ecrire de lui-meme a ce membre maintenant ?

    Oui si la personne a ecrit dans les dernieres 24 h (regle WhatsApp) et si
    MUMU ne lui a pas deja ecrit deux fois de lui-meme sur la meme periode.
    """
    since = (now or now_utc()) - WHATSAPP_WINDOW
    last_inbound = session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.member_id == member.id, Message.direction == "in", Message.created_at >= since)
    )
    if not last_inbound:
        return False
    proactive = session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.member_id == member.id,
            Message.kind == "proactive",
            Message.created_at >= since,
        )
    )
    return (proactive or 0) < MAX_PROACTIVE_PER_DAY


def record_proactive(session: Session, member: Member, body: str) -> None:
    session.add(
        Message(
            household_id=member.household_id,
            member_id=member.id,
            direction="out",
            kind="proactive",
            body=body,
        )
    )


def handle_incoming(
    session_factory: sessionmaker,
    brain: Brain,
    address: str,
    text: str,
    media: Media | None,
    timezone: str = "Europe/Paris",
    actions: ConnectionActions | None = None,
) -> str:
    """Enregistre le message, le fait comprendre, enregistre le resultat.

    Renvoie le texte a envoyer au membre.
    """
    with session_factory() as session:
        member = get_or_create_member(session, address)
        inbound = Message(
            household_id=member.household_id,
            member_id=member.id,
            direction="in",
            body=text,
            media_type=media.content_type if media else None,
        )
        session.add(inbound)
        session.flush()
        context = build_context(session, member, inbound.id, timezone)
        # Le message est garde meme si la suite echoue.
        session.commit()

        action = None
        try:
            understanding = brain.understand(context, text, media)
        except Exception:
            log.exception("Comprehension impossible pour le message %s", inbound.id)
            reply = FALLBACK_REPLY
        else:
            reply = understanding.reply
            action = understanding.action
            if understanding.member_name and not member.name:
                member.name = understanding.member_name
            for item in understanding.items:
                session.add(
                    Item(
                        household_id=member.household_id,
                        source_message_id=inbound.id,
                        member_id=member.id,
                        **item,
                    )
                )
            for fact in understanding.facts:
                session.add(
                    Fact(
                        household_id=member.household_id,
                        source_message_id=inbound.id,
                        member_id=member.id,
                        text=fact,
                    )
                )
            if understanding.done_item_ids:
                # Seuls les elements que ce membre peut voir peuvent etre clos.
                for done in session.scalars(
                    select(Item).where(
                        Item.household_id == member.household_id,
                        Item.id.in_(understanding.done_item_ids),
                        visible_to(Item, member),
                    )
                ):
                    done.status = "done"

        session.add(
            Message(
                household_id=member.household_id, member_id=member.id, direction="out", body=reply
            )
        )
        session.commit()
        member_id = member.id

    # Les actions de connexion ouvrent leur propre session.
    if action == "connect_google":
        link = actions.connect_link(member_id) if actions else None
        reply = f"{reply}\n{link}" if link else CONNECT_UNAVAILABLE
    elif action == "disconnect_google":
        if not (actions and actions.disconnect(member_id)):
            reply = NOTHING_TO_DISCONNECT
    return reply
