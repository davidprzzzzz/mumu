"""Lecture en arriere-plan des services connectes.

Pour chaque connexion active : rafraichir l'agenda, lire les nouveaux mails,
en tirer ce qui compte, et prevenir la personne seulement si c'est utile.
Le contenu des mails n'est jamais enregistre : seuls restent les elements
structures et une reference vers le message d'origine.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from .brain import Brain, Ingestion
from .connectors.base import AccessRevoked, SourceDocument
from .connectors.google import GoogleClient
from .crypto import decrypt
from .models import (
    CalendarEvent,
    Connection,
    Fact,
    Item,
    Member,
    Purchase,
    SeenDocument,
    now_utc,
)
from .service import build_context, can_write_first, record_proactive
from .whatsapp import Sender

log = logging.getLogger("mumu.sync")

BACKFILL_DAYS = 30
BACKFILL_LIMIT = 100
SYNC_LIMIT = 50
# Marge relue a chaque passage ; les doublons sont ecartes par SeenDocument.
CURSOR_OVERLAP_SECONDS = 3600
CONNECTED_SOURCES = ("gmail", "calendar")

ACCESS_LOST = (
    "Je n'ai plus accès à ta boîte mail, l'autorisation a expiré. "
    "Dis-moi « reconnecte » si tu veux que je reprenne."
)


@dataclass
class SyncStats:
    read: int = 0
    relevant: int = 0
    items: int = 0
    purchases: int = 0
    notified: int = 0
    revoked: bool = False


def store_ingestion(
    session: Session, member: Member, document: SourceDocument, ingestion: Ingestion
) -> None:
    """Enregistre ce qui a ete tire d'un document, en prive pour ce membre."""
    source = {
        "source_kind": document.source_kind,
        "source_ref": document.external_id,
        "source_label": ingestion.label or document.subject[:200],
        "member_id": member.id,
        "visibility": "private",
    }
    for item in ingestion.items:
        session.add(Item(household_id=member.household_id, **source, **item))
    for fact in ingestion.facts:
        session.add(Fact(household_id=member.household_id, text=fact, **source))
    for purchase in ingestion.purchases:
        session.add(
            Purchase(
                household_id=member.household_id,
                member_id=member.id,
                source_kind=document.source_kind,
                source_ref=document.external_id,
                **purchase,
            )
        )


def _refresh_calendar(session: Session, member: Member, google: GoogleClient, access: str, now: datetime) -> None:
    events = google.list_events(access, now)
    session.execute(delete(CalendarEvent).where(CalendarEvent.member_id == member.id))
    for event in events:
        session.add(
            CalendarEvent(
                member_id=member.id,
                external_id=event.external_id,
                title=event.title,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                all_day=event.all_day,
            )
        )


def _backfill_summary(session: Session, member: Member, stats: SyncStats) -> str:
    lines = [
        f"J'ai fait le tour de tes mails des {BACKFILL_DAYS} derniers jours : "
        f"{stats.items} chose(s) à suivre et {stats.purchases} achat(s) repérés."
    ]
    nearest = session.execute(
        select(Item.title, Item.due_date)
        .where(
            Item.member_id == member.id,
            Item.status == "open",
            Item.source_kind == "gmail",
            Item.due_date.is_not(None),
            Item.due_date >= now_utc().date(),
        )
        .order_by(Item.due_date)
        .limit(1)
    ).first()
    if nearest:
        lines.append(f"La plus proche : {nearest.title}, pour le {nearest.due_date.isoformat()}.")
    lines.append("Tu n'as rien à faire, je te préviens au bon moment.")
    return "\n".join(lines)


def sync_connection(
    session_factory: sessionmaker,
    connection_id: int,
    google: GoogleClient,
    brain: Brain,
    sender: Sender,
    secret_key: str,
    timezone: str = "Europe/Paris",
    now: datetime | None = None,
) -> SyncStats:
    stats = SyncStats()
    now = now or now_utc()
    with session_factory() as session:
        connection = session.get(Connection, connection_id)
        if connection is None or connection.status != "active":
            return stats
        member = session.get(Member, connection.member_id)

        def tell(body: str) -> bool:
            if not can_write_first(session, member, now):
                return False
            sender.send(member.address, body)
            record_proactive(session, member, body)
            return True

        refresh_token = decrypt(secret_key, connection.token_encrypted)
        try:
            if refresh_token is None:
                raise AccessRevoked("jeton illisible")
            access = google.refresh(refresh_token)
        except AccessRevoked:
            connection.status = "revoked"
            stats.revoked = True
            tell(ACCESS_LOST)
            session.commit()
            return stats

        backfill = not connection.backfilled
        now_ts = int(now.timestamp())
        after_ts = connection.cursor_ts or now_ts - BACKFILL_DAYS * 86400

        try:
            _refresh_calendar(session, member, google, access, now)
            session.commit()
        except Exception:
            session.rollback()
            log.exception("Agenda illisible pour la connexion %s", connection_id)

        message_ids = google.list_message_ids(
            access, after_ts, BACKFILL_LIMIT if backfill else SYNC_LIMIT
        )
        already_seen = set(
            session.scalars(
                select(SeenDocument.external_id).where(
                    SeenDocument.connection_id == connection.id,
                    SeenDocument.external_id.in_(message_ids),
                )
            )
        )
        for message_id in message_ids:
            if message_id in already_seen:
                continue
            try:
                document = google.get_message(access, message_id)
                ingestion = None
                if document is not None:
                    context = build_context(session, member, None, timezone)
                    ingestion = brain.ingest(context, document)
            except AccessRevoked:
                raise
            except Exception:
                # Non marque comme lu : il sera repris au prochain passage.
                session.rollback()
                log.exception("Document %s illisible", message_id)
                continue

            session.add(SeenDocument(connection_id=connection.id, external_id=message_id))
            stats.read += 1
            if ingestion is not None and ingestion.relevant:
                stats.relevant += 1
                stats.items += len(ingestion.items)
                stats.purchases += len(ingestion.purchases)
                store_ingestion(session, member, document, ingestion)
                # Pendant le premier passage, MUMU ne commente pas chaque mail.
                if ingestion.notify == "maintenant" and not backfill and tell(ingestion.message):
                    stats.notified += 1
            session.commit()

        connection.cursor_ts = now_ts - CURSOR_OVERLAP_SECONDS
        connection.last_sync_at = now
        if backfill:
            connection.backfilled = True
            if stats.items or stats.purchases:
                session.flush()
                if tell(_backfill_summary(session, member, stats)):
                    stats.notified += 1
        session.commit()
    return stats


def sync_all(
    session_factory: sessionmaker,
    google: GoogleClient,
    brain: Brain,
    sender: Sender,
    secret_key: str,
    timezone: str = "Europe/Paris",
) -> None:
    with session_factory() as session:
        ids = session.scalars(select(Connection.id).where(Connection.status == "active")).all()
    for connection_id in ids:
        try:
            stats = sync_connection(
                session_factory, connection_id, google, brain, sender, secret_key, timezone
            )
            log.info("Connexion %s : %s", connection_id, stats)
        except Exception:
            log.exception("Synchronisation impossible pour la connexion %s", connection_id)


def disconnect_google(
    session_factory: sessionmaker, member_id: int, google: GoogleClient, secret_key: str
) -> bool:
    """Retire l'acces et efface tout ce qui a ete tire des comptes connectes."""
    with session_factory() as session:
        connection = session.scalar(
            select(Connection).where(
                Connection.member_id == member_id, Connection.provider == "google"
            )
        )
        if connection is None:
            return False
        refresh_token = decrypt(secret_key, connection.token_encrypted)
        if refresh_token:
            try:
                google.revoke(refresh_token)
            except Exception:
                log.exception("Revocation du jeton impossible chez Google")
        session.execute(delete(SeenDocument).where(SeenDocument.connection_id == connection.id))
        session.execute(delete(CalendarEvent).where(CalendarEvent.member_id == member_id))
        for model in (Item, Fact, Purchase):
            session.execute(
                delete(model).where(
                    model.member_id == member_id, model.source_kind.in_(CONNECTED_SOURCES)
                )
            )
        session.delete(connection)
        session.commit()
        return True
