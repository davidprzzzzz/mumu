"""Documents : identification, doublons, recherche et lien de recuperation.

Le fichier lui-meme n'est jamais copie dans la base. MUMU garde une
reference (piece jointe Gmail, media WhatsApp) et le recupere a la demande.
"""

import hashlib
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import log_action
from .crypto import sign_payload, verify_payload
from .models import Document, Member
from .workflows.templates import DOCUMENT_KINDS

LINK_TTL_SECONDS = 3600
LINK_PURPOSE = "document"


@dataclass
class DocumentFile:
    data: bytes
    mime_type: str
    filename: str | None
    # Ou retrouver le fichier plus tard.
    source_kind: str
    source_ref: str
    attachment_ref: str | None = None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_document(session: Session, member: Member, meta: dict, file: DocumentFile) -> tuple[Document, bool]:
    """Enregistre un document identifie. Un meme fichier n'est garde qu'une fois.

    Renvoie (document, nouveau).
    """
    digest = sha256(file.data)
    existing = session.scalar(
        select(Document).where(Document.member_id == member.id, Document.sha256 == digest)
    )
    if existing is not None:
        return existing, False
    kind = meta.get("kind") if meta.get("kind") in DOCUMENT_KINDS else "autre"
    doc = Document(
        household_id=member.household_id,
        member_id=member.id,
        kind=kind,
        title=(meta.get("title") or DOCUMENT_KINDS[kind])[:200],
        issuer=(meta.get("issuer") or None),
        issued_on=meta.get("issued_on"),
        valid_until=meta.get("valid_until"),
        summary=(meta.get("summary") or "")[:2000],
        sha256=digest,
        mime_type=file.mime_type,
        filename=file.filename,
        source_kind=file.source_kind,
        source_ref=file.source_ref,
        attachment_ref=file.attachment_ref,
    )
    session.add(doc)
    session.flush()
    log_action(
        session, "document_stored", household_id=member.household_id, member_id=member.id,
        target=f"document:{doc.id}", kind=kind, source=file.source_kind,
    )
    return doc, True


def visible_documents(session: Session, member: Member) -> list[Document]:
    return list(
        session.scalars(
            select(Document)
            .where(Document.member_id == member.id)
            .order_by(Document.id.desc())
            .limit(30)
        )
    )


def document_line(doc: Document, today: date) -> str:
    validity = ""
    if doc.valid_until:
        state = "valable" if doc.valid_until >= today else "expiré"
        validity = f", {state} jusqu'au {doc.valid_until.isoformat()}"
    issuer = f" ({doc.issuer})" if doc.issuer else ""
    return f"- [D{doc.id}] {doc.title}{issuer}{validity}"


def document_link(secret_key: str, public_url: str, doc: Document, member: Member) -> str:
    token = sign_payload(secret_key, {"d": doc.id, "m": member.id}, LINK_TTL_SECONDS, LINK_PURPOSE)
    return f"{public_url}/documents/{token}"


def resolve_link(session: Session, secret_key: str, token: str) -> Document | None:
    """Verifie le lien et renvoie le document, seulement pour son proprietaire."""
    payload = verify_payload(secret_key, token, LINK_PURPOSE)
    if payload is None:
        return None
    doc = session.get(Document, payload.get("d"))
    if doc is None or doc.member_id != payload.get("m"):
        return None
    return doc
