"""Lecture en arriere-plan des services connectes.

Pour chaque connexion active : rafraichir l'agenda, lire les nouveaux mails
et leurs pieces jointes, en tirer ce qui compte (processus, documents,
achats, montants), et prevenir la personne seulement si c'est utile.
Le contenu des mails n'est jamais enregistre : seuls restent les elements
structures et une reference vers le message d'origine.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from .audit import log_action
from .brain import Brain, Ingestion, Media
from .connectors.base import AccessRevoked, Provider, SourceDocument
from .crypto import decrypt
from .documents import DocumentFile, store_document
from .messaging import can_write_first, record_proactive
from .models import (
    Approval,
    Assignment,
    CalendarEvent,
    Charge,
    Connection,
    Document,
    Fact,
    Item,
    Member,
    Notification,
    Purchase,
    SeenDocument,
    Workflow,
    WorkflowSource,
    now_utc,
)
from .service import build_context
from .signals import route_signals
from .whatsapp import Sender
from .workflows.engine import SourceRef

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
    workflows: int = 0
    purchases: int = 0
    documents: int = 0
    notified: int = 0
    revoked: bool = False


def store_ingestion(
    session: Session,
    member: Member,
    document: SourceDocument,
    ingestion: Ingestion,
    files: list[Media],
    today,
) -> tuple[list[Workflow], int, int]:
    """Enregistre ce qui a ete tire d'un mail, en prive pour ce membre.

    Renvoie (processus crees, achats, documents).
    """
    label = ingestion.label or document.subject[:200]
    source = {
        "source_kind": document.source_kind,
        "source_ref": document.external_id,
        "source_label": label,
        "member_id": member.id,
        "visibility": "private",
    }
    for item in ingestion.items:
        session.add(Item(household_id=member.household_id, **source, **item))
    for fact in ingestion.facts:
        session.add(Fact(household_id=member.household_id, text=fact, **source))

    stored_documents = 0
    for meta in ingestion.documents:
        if not 0 <= meta.index < len(files) or meta.index >= len(document.attachments):
            continue
        attachment = document.attachments[meta.index]
        _, new = store_document(
            session, member,
            {"kind": meta.kind, "title": meta.title, "issuer": meta.issuer, "issued_on": meta.issued_on,
             "valid_until": meta.valid_until, "summary": meta.summary},
            DocumentFile(
                data=files[meta.index].data, mime_type=attachment.mime_type, filename=attachment.filename,
                source_kind=document.source_kind, source_ref=document.external_id,
                attachment_ref=attachment.attachment_id,
            ),
        )
        stored_documents += int(new)

    routed = route_signals(
        session, member, ingestion.signals,
        SourceRef(document.source_kind, document.external_id, label),
        today, received_on=document.received_at.date(),
    )
    return routed.created, len(routed.purchases), stored_documents


def _refresh_calendar(session: Session, member: Member, provider: Provider, access: str, now: datetime) -> None:
    events = provider.list_events(access, now)
    session.execute(delete(CalendarEvent).where(CalendarEvent.member_id == member.id))
    for event in events:
        session.add(
            CalendarEvent(
                member_id=member.id, external_id=event.external_id, title=event.title,
                starts_at=event.starts_at, ends_at=event.ends_at, all_day=event.all_day,
            )
        )


def _backfill_summary(session: Session, member: Member, stats: SyncStats, today) -> str:
    found = []
    if stats.workflows:
        found.append(f"{stats.workflows} démarche(s) à suivre")
    if stats.items:
        found.append(f"{stats.items} chose(s) avec une date")
    if stats.purchases:
        found.append(f"{stats.purchases} achat(s)")
    if stats.documents:
        found.append(f"{stats.documents} document(s) gardé(s)")
    lines = [f"J'ai fait le tour de tes mails des {BACKFILL_DAYS} derniers jours : {', '.join(found)}."]
    nearest = session.scalar(
        select(Workflow)
        .where(
            Workflow.owner_member_id == member.id,
            Workflow.status.in_(["active", "waiting_approval", "blocked"]),
            Workflow.due_date.is_not(None),
            Workflow.due_date >= today,
        )
        .order_by(Workflow.due_date)
        .limit(1)
    )
    if nearest:
        lines.append(f"Le plus proche : {nearest.title}, pour le {nearest.due_date.isoformat()}.")
    lines.append("Tu n'as rien à faire, je te préviens au bon moment.")
    return "\n".join(lines)


def _download_attachments(provider: Provider, access: str, document: SourceDocument) -> list[Media]:
    files = []
    for attachment in document.attachments:
        try:
            data = provider.get_attachment(access, document.external_id, attachment.attachment_id)
        except AccessRevoked:
            raise
        except Exception:
            log.exception("Pièce jointe illisible dans %s", document.external_id)
            data = b""
        files.append(Media(content_type=attachment.mime_type, data=data, filename=attachment.filename))
    return files


def sync_connection(
    session_factory: sessionmaker,
    connection_id: int,
    provider: Provider,
    brain: Brain,
    sender: Sender,
    secret_key: str,
    timezone: str = "Europe/Paris",
    now: datetime | None = None,
) -> SyncStats:
    stats = SyncStats()
    now = now or now_utc()
    today = now.astimezone(ZoneInfo(timezone)).date()
    with session_factory() as session:
        connection = session.get(Connection, connection_id)
        if connection is None or connection.status != "active":
            return stats
        member = session.get(Member, connection.member_id)

        def tell(body: str, keys: list[str]) -> bool:
            if not can_write_first(session, member, now):
                return False
            sender.send(member.address, body)
            record_proactive(session, member, body, key=keys[0] if keys else None)
            for key in keys[1:]:
                session.add(Notification(member_id=member.id, key=key, body=body))
            return True

        refresh_token = decrypt(secret_key, connection.token_encrypted)
        try:
            if refresh_token is None:
                raise AccessRevoked("jeton illisible")
            access = provider.refresh(refresh_token)
        except AccessRevoked:
            connection.status = "revoked"
            stats.revoked = True
            log_action(session, "connection_revoked", member_id=member.id, actor="system", target=f"connection:{connection.id}")
            tell(ACCESS_LOST, [f"conn:{connection.id}:revoked:{today.isoformat()}"])
            session.commit()
            return stats

        backfill = not connection.backfilled
        now_ts = int(now.timestamp())
        after_ts = connection.cursor_ts or now_ts - BACKFILL_DAYS * 86400

        try:
            _refresh_calendar(session, member, provider, access, now)
            session.commit()
        except Exception:
            session.rollback()
            log.exception("Agenda illisible pour la connexion %s", connection_id)

        message_ids = provider.list_message_ids(access, after_ts, BACKFILL_LIMIT if backfill else SYNC_LIMIT)
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
                document = provider.get_message(access, message_id)
                ingestion, files = None, []
                if document is not None:
                    files = _download_attachments(provider, access, document)
                    context = build_context(session, member, None, timezone)
                    ingestion = brain.ingest(context, document, files)
                session.add(SeenDocument(connection_id=connection.id, external_id=message_id))
                stats.read += 1
                if ingestion is not None and ingestion.relevant:
                    stats.relevant += 1
                    stats.items += len(ingestion.items)
                    created, purchases, documents = store_ingestion(session, member, document, ingestion, files, today)
                    stats.workflows += len(created)
                    stats.purchases += purchases
                    stats.documents += documents
                    # Pendant le premier passage, MUMU ne commente pas chaque mail.
                    if ingestion.notify == "maintenant" and not backfill:
                        # Le meme sujet ne sera pas rappele une seconde fois
                        # par le moteur proactif.
                        keys = [f"doc:{connection.id}:{message_id}"] + [
                            f"wf:{wf.id}:due_soon" for wf in created
                        ]
                        if tell(ingestion.message, keys):
                            stats.notified += 1
                session.commit()
            except AccessRevoked:
                session.rollback()
                connection.status = "revoked"
                stats.revoked = True
                tell(ACCESS_LOST, [f"conn:{connection.id}:revoked:{today.isoformat()}"])
                session.commit()
                return stats
            except Exception:
                # Non marque comme lu : il sera repris au prochain passage.
                session.rollback()
                log.exception("Document %s illisible", message_id)
                continue

        connection.cursor_ts = now_ts - CURSOR_OVERLAP_SECONDS
        connection.last_sync_at = now
        if backfill:
            connection.backfilled = True
            if stats.workflows or stats.items or stats.purchases or stats.documents:
                session.flush()
                if tell(_backfill_summary(session, member, stats, today), [f"conn:{connection.id}:backfill"]):
                    stats.notified += 1
        session.commit()
    return stats


def sync_all(
    session_factory: sessionmaker,
    providers: Provider | dict[str, Provider],
    brain: Brain,
    sender: Sender,
    secret_key: str,
    timezone: str = "Europe/Paris",
) -> None:
    """Synchronise toutes les connexions actives.

    `providers` associe un nom de fournisseur (« google ») a son client. Un
    client seul vaut pour Google.
    """
    if not isinstance(providers, dict):
        providers = {"google": providers}
    with session_factory() as session:
        rows = session.execute(
            select(Connection.id, Connection.provider).where(Connection.status == "active")
        ).all()
    for connection_id, provider_name in rows:
        provider = providers.get(provider_name)
        if provider is None:
            continue
        try:
            stats = sync_connection(session_factory, connection_id, provider, brain, sender, secret_key, timezone)
            log.info("Connexion %s : %s", connection_id, stats)
        except Exception:
            log.exception("Synchronisation impossible pour la connexion %s", connection_id)


def erase_connected_data(session: Session, member_id: int) -> None:
    """Efface tout ce qui a ete tire des comptes connectes d'un membre."""
    session.execute(delete(CalendarEvent).where(CalendarEvent.member_id == member_id))
    for model in (Item, Fact):
        session.execute(
            delete(model).where(model.member_id == member_id, model.source_kind.in_(CONNECTED_SOURCES))
        )
    for model in (Purchase, Charge, Document):
        session.execute(
            delete(model).where(model.member_id == member_id, model.source_kind.in_(CONNECTED_SOURCES))
        )
    workflows = session.scalars(select(Workflow).where(Workflow.owner_member_id == member_id)).all()
    for wf in workflows:
        connected = [s for s in wf.sources if s.source_kind in CONNECTED_SOURCES]
        if not connected:
            continue
        if len(connected) == len(wf.sources):
            # Le processus n'existe que grace aux mails : il disparait entierement.
            session.execute(delete(Approval).where(Approval.workflow_id == wf.id))
            session.execute(delete(Assignment).where(Assignment.workflow_id == wf.id))
            session.delete(wf)
        else:
            for source in connected:
                session.execute(delete(WorkflowSource).where(WorkflowSource.id == source.id))


def disconnect_google(session_factory: sessionmaker, member_id: int, google: Provider, secret_key: str) -> bool:
    """Retire l'acces et efface tout ce qui a ete tire des comptes connectes."""
    with session_factory() as session:
        connection = session.scalar(
            select(Connection).where(Connection.member_id == member_id, Connection.provider == "google")
        )
        if connection is None:
            return False
        refresh_token = decrypt(secret_key, connection.token_encrypted)
        if refresh_token:
            try:
                google.revoke(refresh_token)
            except Exception:
                log.exception("Révocation du jeton impossible chez Google")
        session.execute(delete(SeenDocument).where(SeenDocument.connection_id == connection.id))
        erase_connected_data(session, member_id)
        session.delete(connection)
        log_action(session, "connection_deleted", member_id=member_id, actor="member", provider="google")
        session.commit()
        return True
