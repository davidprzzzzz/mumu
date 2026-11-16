"""Schema de la base : foyers, membres, messages, elements a suivre, memoire.

Chaque element et chaque fait garde un lien vers le message qui l'a cree :
MUMU doit toujours pouvoir dire d'ou il tient une information.
"""

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    members: Mapped[list["Member"]] = relationship(back_populates="household")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    # Adresse WhatsApp au format Twilio, ex. "whatsapp:+33612345678".
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(80), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    household: Mapped[Household] = relationship(back_populates="members")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)
    # "in" : recu d'un membre. "out" : envoye par MUMU.
    direction: Mapped[str] = mapped_column(String(3))
    body: Mapped[str] = mapped_column(Text, default="")
    media_type: Mapped[str | None] = mapped_column(String(80), default=None)
    # "reply" : reponse a un message. "proactive" : MUMU ecrit de lui-meme.
    kind: Mapped[str] = mapped_column(String(12), default="reply")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Item(Base):
    """Une chose que MUMU suit : tache, echeance, paiement, rendez-vous, course."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    # Message WhatsApp d'origine, quand l'information vient d'une conversation.
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), default=None)
    # Origine : "whatsapp", "gmail" ou "calendar", avec la reference chez le
    # fournisseur et un libelle lisible ("Mail EDF du 12/11").
    source_kind: Mapped[str] = mapped_column(String(20), default="whatsapp")
    source_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    source_label: Mapped[str | None] = mapped_column(String(200), default=None)
    # Membre a qui appartient l'information. Ce qui vient d'une boite mail
    # reste "private" : les autres membres du foyer ne le voient pas.
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)
    visibility: Mapped[str] = mapped_column(String(12), default="household")
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    due_date: Mapped[date | None] = mapped_column(Date, default=None)
    amount_eur: Mapped[float | None] = mapped_column(Float, default=None)
    owner: Mapped[str | None] = mapped_column(String(80), default=None)
    # "open", "done" ou "cancelled".
    status: Mapped[str] = mapped_column(String(12), default="open")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Fact(Base):
    """Un fait durable sur le foyer, dit ou montre par un membre."""

    __tablename__ = "facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    # Message WhatsApp d'origine, quand l'information vient d'une conversation.
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), default=None)
    # Origine : "whatsapp", "gmail" ou "calendar", avec la reference chez le
    # fournisseur et un libelle lisible ("Mail EDF du 12/11").
    source_kind: Mapped[str] = mapped_column(String(20), default="whatsapp")
    source_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    source_label: Mapped[str | None] = mapped_column(String(200), default=None)
    # Membre a qui appartient l'information. Ce qui vient d'une boite mail
    # reste "private" : les autres membres du foyer ne le voient pas.
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)
    visibility: Mapped[str] = mapped_column(String(12), default="household")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Purchase(Base):
    """Un achat repere dans un mail de confirmation ou un recu."""

    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    merchant: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(300))
    amount_eur: Mapped[float | None] = mapped_column(Float, default=None)
    ordered_on: Mapped[date | None] = mapped_column(Date, default=None)
    delivery_expected: Mapped[date | None] = mapped_column(Date, default=None)
    order_ref: Mapped[str | None] = mapped_column(String(120), default=None)
    source_kind: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Connection(Base):
    """L'autorisation donnee par un membre d'acceder a un de ses services."""

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("member_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    scopes: Mapped[str] = mapped_column(Text, default="")
    # Jeton de rafraichissement, chiffre. Jamais stocke en clair.
    token_encrypted: Mapped[str] = mapped_column(Text)
    # "active", "revoked" (acces retire ou expire).
    status: Mapped[str] = mapped_column(String(12), default="active")
    # Instant (secondes Unix) jusqu'auquel les mails ont ete lus.
    cursor_ts: Mapped[int | None] = mapped_column(Integer, default=None)
    backfilled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class SeenDocument(Base):
    """Identifiants des documents deja traites, pour ne jamais les relire.

    Seul l'identifiant est garde : le contenu du mail n'est pas stocke.
    """

    __tablename__ = "seen_documents"
    __table_args__ = (UniqueConstraint("connection_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))


class CalendarEvent(Base):
    """Copie des evenements a venir d'un membre, rafraichie a chaque synchro."""

    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)


def make_session_factory(database_url: str) -> sessionmaker:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
