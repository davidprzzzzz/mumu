"""Schema de la base : foyers, membres, messages, elements a suivre, memoire.

Chaque element et chaque fait garde un lien vers le message qui l'a cree :
MUMU doit toujours pouvoir dire d'ou il tient une information.
"""

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, Text, create_engine
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Item(Base):
    """Une chose que MUMU suit : tache, echeance, paiement, rendez-vous, course."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
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
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


def make_session_factory(database_url: str) -> sessionmaker:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
