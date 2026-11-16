"""Regles d'envoi : quand MUMU a le droit d'ecrire de lui-meme."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Member, Message, Notification, now_utc

# WhatsApp n'autorise un message libre que dans les 24 h qui suivent le
# dernier message de la personne. Au-dela, il faut un modele approuve par Meta.
WHATSAPP_WINDOW = timedelta(hours=24)
MAX_PROACTIVE_PER_DAY = 2


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=now_utc().tzinfo)


def in_window(session: Session, member: Member, now: datetime | None = None) -> bool:
    since = (now or now_utc()) - WHATSAPP_WINDOW
    return bool(
        session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.member_id == member.id, Message.direction == "in", Message.created_at >= since)
        )
    )


def proactive_sent_today(session: Session, member: Member, now: datetime | None = None) -> int:
    since = (now or now_utc()) - WHATSAPP_WINDOW
    return session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.member_id == member.id, Message.kind == "proactive", Message.created_at >= since)
    ) or 0


def can_write_first(session: Session, member: Member, now: datetime | None = None) -> bool:
    """MUMU peut-il ecrire de lui-meme a ce membre maintenant ?

    Oui si la personne a ecrit dans les dernieres 24 h (regle WhatsApp) et si
    MUMU ne lui a pas deja ecrit deux fois de lui-meme sur la meme periode.
    """
    return in_window(session, member, now) and proactive_sent_today(session, member, now) < MAX_PROACTIVE_PER_DAY


def already_notified(session: Session, member: Member, key: str) -> bool:
    return session.scalar(
        select(Notification.id).where(Notification.member_id == member.id, Notification.key == key)
    ) is not None


def record_proactive(session: Session, member: Member, body: str, key: str | None = None) -> None:
    session.add(
        Message(
            household_id=member.household_id,
            member_id=member.id,
            direction="out",
            kind="proactive",
            body=body,
        )
    )
    if key:
        session.add(Notification(member_id=member.id, key=key[:200], body=body))


@dataclass
class Outbox:
    """Messages a envoyer a d'autres personnes que celle qui parle.

    Ils partent apres l'enregistrement en base, jamais avant.
    """

    messages: list[tuple[str, str]] = field(default_factory=list)

    def add(self, address: str, body: str) -> None:
        self.messages.append((address, body))
