"""Traitement d'un message entrant, de la reception a la reponse."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .brain import Brain, Context, Media
from .models import Fact, Household, Item, Member, Message
from .persona import FALLBACK_REPLY

log = logging.getLogger("mumu.service")

RECENT_MESSAGES = 10
MAX_FACTS_IN_CONTEXT = 60


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


def build_context(session: Session, member: Member, before_message_id: int, timezone: str) -> Context:
    household_id = member.household_id
    facts = session.scalars(
        select(Fact.text)
        .where(Fact.household_id == household_id)
        .order_by(Fact.id.desc())
        .limit(MAX_FACTS_IN_CONTEXT)
    ).all()
    open_items = session.execute(
        select(Item.id, Item.title, Item.due_date)
        .where(Item.household_id == household_id, Item.status == "open")
        .order_by(Item.due_date.is_(None), Item.due_date, Item.id)
    ).all()
    recent = session.execute(
        select(Message.direction, Message.body)
        .where(Message.household_id == household_id, Message.id < before_message_id)
        .order_by(Message.id.desc())
        .limit(RECENT_MESSAGES)
    ).all()
    return Context(
        today=datetime.now(ZoneInfo(timezone)).date(),
        member_name=member.name,
        facts=list(reversed(facts)),
        open_items=[(row.id, row.title, row.due_date) for row in open_items],
        recent_messages=[(row.direction, row.body) for row in reversed(recent)],
    )


def handle_incoming(
    session_factory: sessionmaker,
    brain: Brain,
    address: str,
    text: str,
    media: Media | None,
    timezone: str = "Europe/Paris",
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

        try:
            understanding = brain.understand(context, text, media)
        except Exception:
            log.exception("Comprehension impossible pour le message %s", inbound.id)
            reply = FALLBACK_REPLY
        else:
            reply = understanding.reply
            if understanding.member_name and not member.name:
                member.name = understanding.member_name
            for item in understanding.items:
                session.add(
                    Item(
                        household_id=member.household_id,
                        source_message_id=inbound.id,
                        **item,
                    )
                )
            for fact in understanding.facts:
                session.add(
                    Fact(
                        household_id=member.household_id,
                        source_message_id=inbound.id,
                        text=fact,
                    )
                )
            if understanding.done_item_ids:
                # Seuls les elements du foyer peuvent etre clos.
                for done in session.scalars(
                    select(Item).where(
                        Item.household_id == member.household_id,
                        Item.id.in_(understanding.done_item_ids),
                    )
                ):
                    done.status = "done"

        session.add(Message(household_id=member.household_id, direction="out", body=reply))
        session.commit()
        return reply
