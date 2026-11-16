"""Schema de la base.

Principes :
- chaque information garde sa source (message WhatsApp, mail, agenda) ;
- ce qui vient d'un compte connecte est prive a son proprietaire
  (`visibility = "private"`) tant qu'il ne l'a pas explicitement partage ;
- un processus (Workflow) n'est termine que lorsque le probleme est regle
  ou que la personne l'a explicitement ferme.
"""

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
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


# --- Foyer et personnes -----------------------------------------------------


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "seul", "couple", "famille", "coloc" : adapte le ton et les priorites.
    kind: Mapped[str | None] = mapped_column(String(12), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    members: Mapped[list["Member"]] = relationship(back_populates="household")


class Member(Base):
    """Une personne qui parle a MUMU."""

    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    # Adresse WhatsApp au format Twilio, ex. "whatsapp:+33612345678".
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(80), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    household: Mapped[Household] = relationship(back_populates="members")


class Person(Base):
    """Quelqu'un du foyer qui ne parle pas a MUMU, typiquement un enfant.

    Visible par les adultes du foyer uniquement.
    """

    __tablename__ = "people"
    __table_args__ = (UniqueConstraint("household_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    relation: Mapped[str] = mapped_column(String(20), default="enfant")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Invite(Base):
    """Code pour rejoindre un foyer existant."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    code: Mapped[str] = mapped_column(String(12), unique=True)
    invited_name: Mapped[str | None] = mapped_column(String(80), default=None)
    created_by_member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)


# --- Conversation ------------------------------------------------------------


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None, index=True)
    # "in" : recu d'un membre. "out" : envoye par MUMU.
    direction: Mapped[str] = mapped_column(String(3))
    body: Mapped[str] = mapped_column(Text, default="")
    media_type: Mapped[str | None] = mapped_column(String(80), default=None)
    # "reply" : reponse a un message. "proactive" : MUMU ecrit de lui-meme.
    kind: Mapped[str] = mapped_column(String(12), default="reply")
    # Identifiant du message chez Twilio (MessageSid) : un message livre deux
    # fois n'est traite qu'une fois.
    external_id: Mapped[str | None] = mapped_column(String(64), default=None, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)


# --- Memoire simple -------------------------------------------------------


class Item(Base):
    """Une chose simple a suivre : tache, rendez-vous, course."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    due_date: Mapped[date | None] = mapped_column(Date, default=None)
    amount_eur: Mapped[float | None] = mapped_column(Float, default=None)
    owner: Mapped[str | None] = mapped_column(String(80), default=None)
    # "open", "done" ou "cancelled".
    status: Mapped[str] = mapped_column(String(12), default="open")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # Message WhatsApp d'origine, quand l'information vient d'une conversation.
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), default=None)
    # Origine : "whatsapp", "gmail", "calendar".
    source_kind: Mapped[str] = mapped_column(String(20), default="whatsapp")
    source_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    source_label: Mapped[str | None] = mapped_column(String(200), default=None)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)
    visibility: Mapped[str] = mapped_column(String(12), default="household")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Fact(Base):
    """Un fait durable sur le foyer, dit ou montre par un membre."""

    __tablename__ = "facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), default=None)
    source_kind: Mapped[str] = mapped_column(String(20), default="whatsapp")
    source_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    source_label: Mapped[str | None] = mapped_column(String(200), default=None)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)
    visibility: Mapped[str] = mapped_column(String(12), default="household")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


# --- Processus ----------------------------------------------------------------


class Workflow(Base):
    """Un processus suivi de bout en bout : payer une facture, repondre a la
    CAF, obtenir un remboursement, preparer une sortie scolaire..."""

    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("household_id", "dedupe_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    owner_member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    visibility: Mapped[str] = mapped_column(String(12), default="private")
    template: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    # Explication en langage simple de ce que c'est et de ce que ca demande.
    summary: Mapped[str] = mapped_column(Text, default="")
    organization: Mapped[str | None] = mapped_column(String(120), default=None)
    reference: Mapped[str | None] = mapped_column(String(120), default=None)
    amount_eur: Mapped[float | None] = mapped_column(Float, default=None)
    due_date: Mapped[date | None] = mapped_column(Date, default=None)
    event_date: Mapped[date | None] = mapped_column(Date, default=None)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), default=None)
    responsible_member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)
    # active, waiting_approval, waiting_other, blocked, resolved, closed
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    next_action: Mapped[str | None] = mapped_column(String(300), default=None)
    blocker: Mapped[str | None] = mapped_column(String(300), default=None)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resolution: Mapped[str | None] = mapped_column(String(300), default=None)

    steps: Mapped[list["WorkflowStep"]] = relationship(
        back_populates="workflow", order_by="WorkflowStep.position", cascade="all, delete-orphan"
    )
    sources: Mapped[list["WorkflowSource"]] = relationship(cascade="all, delete-orphan")


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("workflow_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(200))
    # "responsible" : une personne du foyer. "mumu" : MUMU s'en charge.
    # "evidence" : se valide quand la preuve arrive (mail de confirmation...).
    owner: Mapped[str] = mapped_column(String(12), default="responsible")
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    optional: Mapped[bool] = mapped_column(Boolean, default=False)
    # todo, awaiting_approval, approved, done, skipped
    status: Mapped[str] = mapped_column(String(20), default="todo")
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    done_by: Mapped[str | None] = mapped_column(String(12), default=None)
    done_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)
    evidence: Mapped[str | None] = mapped_column(String(300), default=None)

    workflow: Mapped[Workflow] = relationship(back_populates="steps")


class WorkflowSource(Base):
    __tablename__ = "workflow_sources"
    __table_args__ = (UniqueConstraint("workflow_id", "source_kind", "source_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    source_kind: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(200))
    label: Mapped[str | None] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Approval(Base):
    """Une action preparee par MUMU qui attend l'accord d'une personne."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(60))
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    draft: Mapped[str] = mapped_column(Text)
    # pending, approved, rejected
    status: Mapped[str] = mapped_column(String(12), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Assignment(Base):
    """Une responsabilite proposee a un membre du foyer."""

    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    proposed_by_member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    # proposed, accepted, declined, done
    status: Mapped[str] = mapped_column(String(12), default="proposed")
    # Faux tant que la proposition n'a pas pu etre envoyee (fenetre WhatsApp).
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    nudged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# --- Documents, argent, achats ---------------------------------------------


class Document(Base):
    """Un document identifie (attestation, facture, contrat...).

    Le fichier n'est pas copie : MUMU garde une reference vers sa source
    (piece jointe Gmail, media WhatsApp) et le recupere a la demande.
    """

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("member_id", "sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    visibility: Mapped[str] = mapped_column(String(12), default="private")
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    issuer: Mapped[str | None] = mapped_column(String(120), default=None)
    issued_on: Mapped[date | None] = mapped_column(Date, default=None)
    valid_until: Mapped[date | None] = mapped_column(Date, default=None)
    summary: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(80))
    filename: Mapped[str | None] = mapped_column(String(200), default=None)
    source_kind: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(300))
    attachment_ref: Mapped[str | None] = mapped_column(String(600), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Charge(Base):
    """Un montant facture ou preleve, lu dans une facture ou un releve."""

    __tablename__ = "charges"
    __table_args__ = (UniqueConstraint("member_id", "source_kind", "source_ref", "merchant_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    merchant: Mapped[str] = mapped_column(String(120))
    merchant_key: Mapped[str] = mapped_column(String(120), index=True)
    amount_eur: Mapped[float] = mapped_column(Float)
    charged_on: Mapped[date] = mapped_column(Date)
    # abonnement, energie, telecom, loyer, assurance, autre
    category: Mapped[str] = mapped_column(String(20), default="autre")
    source_kind: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(200))
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
    delivered_on: Mapped[date | None] = mapped_column(Date, default=None)
    order_ref: Mapped[str | None] = mapped_column(String(120), default=None)
    # ordered, delivered, returned, refunded
    status: Mapped[str] = mapped_column(String(12), default="ordered")
    return_deadline: Mapped[date | None] = mapped_column(Date, default=None)
    warranty_until: Mapped[date | None] = mapped_column(Date, default=None)
    # Vrai quand les dates viennent des delais legaux par defaut, pas du marchand.
    dates_are_estimates: Mapped[bool] = mapped_column(Boolean, default=False)
    source_kind: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


# --- Connexions ---------------------------------------------------------------


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


# --- Confiance ---------------------------------------------------------------


class Notification(Base):
    """Chaque message que MUMU a envoye de lui-meme, pour ne jamais se repeter."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("member_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    key: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class AuditLog(Base):
    """Journal des actions importantes, lisible par la personne concernee."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int | None] = mapped_column(ForeignKey("households.id"), default=None, index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), default=None)
    # "member", "mumu", "system"
    actor: Mapped[str] = mapped_column(String(12))
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str | None] = mapped_column(String(120), default=None)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


def make_engine(database_url: str, **kwargs):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True, **kwargs)


def make_session_factory(database_url: str, create_tables: bool | None = None) -> sessionmaker:
    """Cree la fabrique de sessions.

    En local et dans les tests (SQLite), les tables sont creees directement.
    Ailleurs, le schema est gere par les migrations Alembic (`alembic upgrade head`).
    """
    engine = make_engine(database_url)
    if create_tables is None:
        create_tables = database_url.startswith("sqlite")
    if create_tables:
        Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
