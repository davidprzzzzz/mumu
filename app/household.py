"""Coordination entre les membres d'un foyer.

Principes :
- rien n'est partage sans un geste explicite du proprietaire de l'information ;
- une responsabilite se propose, elle ne s'impose pas : la personne accepte
  ou refuse, d'un mot ;
- une seule relance, jamais de rappel en boucle, jamais de reproche ;
- MUMU ne rapporte pas les oublis de l'un a l'autre : il dit ce qui est fait.
"""

import re
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .audit import log_action
from .messaging import Outbox, in_window
from .models import Assignment, Invite, Member, Person, Workflow, now_utc
from .text import fr_date
from .workflows import engine

INVITE_PATTERN = re.compile(r"\bREJOINDRE\s+([A-Z0-9]{6})\b", re.IGNORECASE)
INVITE_TTL = timedelta(days=7)
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def display_name(member: Member | None) -> str:
    return (member.name if member and member.name else "quelqu'un du foyer")


def can_see(session: Session, wf: Workflow, member: Member) -> bool:
    if wf.household_id != member.household_id:
        return False
    if wf.visibility == "household" or wf.owner_member_id == member.id:
        return True
    if wf.responsible_member_id == member.id:
        return True
    return session.scalar(
        select(Assignment.id).where(Assignment.workflow_id == wf.id, Assignment.member_id == member.id)
    ) is not None


def visible_workflows(session: Session, member: Member, open_only: bool = True) -> list[Workflow]:
    query = select(Workflow).where(
        Workflow.household_id == member.household_id,
        or_(
            Workflow.visibility == "household",
            Workflow.owner_member_id == member.id,
            Workflow.responsible_member_id == member.id,
            Workflow.id.in_(select(Assignment.workflow_id).where(Assignment.member_id == member.id)),
        ),
    )
    if open_only:
        query = query.where(Workflow.status.in_(engine.OPEN_STATUSES))
    return list(session.scalars(query.order_by(Workflow.due_date.is_(None), Workflow.due_date, Workflow.id)))


def other_members(session: Session, member: Member) -> list[Member]:
    return list(
        session.scalars(
            select(Member).where(Member.household_id == member.household_id, Member.id != member.id)
        )
    )


# --- Rejoindre un foyer ------------------------------------------------------


def create_invite(session: Session, member: Member, name: str | None, now: datetime | None = None) -> Invite:
    code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(6))
    invite = Invite(
        household_id=member.household_id,
        code=code,
        invited_name=(name or None),
        created_by_member_id=member.id,
        expires_at=(now or now_utc()) + INVITE_TTL,
    )
    session.add(invite)
    log_action(session, "invite_created", household_id=member.household_id, member_id=member.id, actor="member")
    return invite


def invite_text(invite: Invite, whatsapp_from: str) -> str:
    number = whatsapp_from.removeprefix("whatsapp:").lstrip("+")
    message = f"REJOINDRE {invite.code}"
    link = f"https://wa.me/{number}?text={message.replace(' ', '%20')}" if number else ""
    who = invite.invited_name or "la personne"
    lines = [f"Pour que {who} rejoigne le foyer, transfère-lui ce lien (valable 7 jours) :"]
    lines.append(link or f"écrire « {message} » à MUMU")
    return "\n".join(lines)


def try_join(session: Session, address: str, text: str, now: datetime | None = None) -> Member | None:
    """Une nouvelle adresse qui envoie un code valide rejoint le foyer."""
    match = INVITE_PATTERN.search(text or "")
    if not match:
        return None
    invite = session.scalar(select(Invite).where(Invite.code == match.group(1).upper()))
    now = now or now_utc()
    expires = invite.expires_at if invite and invite.expires_at.tzinfo else (
        invite.expires_at.replace(tzinfo=now.tzinfo) if invite else None
    )
    if invite is None or invite.used_by_member_id or expires < now:
        return None
    member = Member(household_id=invite.household_id, address=address, name=invite.invited_name)
    session.add(member)
    session.flush()
    invite.used_by_member_id = member.id
    log_action(session, "member_joined", household_id=invite.household_id, member_id=member.id, actor="member")
    return member


def welcome_text(session: Session, member: Member) -> str:
    inviter = session.scalar(
        select(Member).join(Invite, Invite.created_by_member_id == Member.id).where(
            Invite.used_by_member_id == member.id
        )
    )
    return (
        f"Bienvenue ☁️ Tu as rejoint le foyer de {display_name(inviter)}. "
        "Je te dirai seulement ce qui te concerne, et ce que tu m'écris reste entre nous. "
        "Comment tu t'appelles ?"
        if not member.name
        else f"Bienvenue {member.name} ☁️ Tu as rejoint le foyer de {display_name(inviter)}. "
        "Je te dirai seulement ce qui te concerne, et ce que tu m'écris reste entre nous."
    )


# --- Responsabilites ---------------------------------------------------------


def _ask_text(session: Session, wf: Workflow, proposer: Member) -> str:
    person = session.get(Person, wf.person_id) if wf.person_id else None
    about = f" pour {person.name}" if person else ""
    when = f", avant le {fr_date(wf.due_date)}" if wf.due_date else ""
    step = engine.current_step(wf)
    what = step.title if step else wf.title
    return (
        f"{display_name(proposer)} te propose de t'occuper de ceci{about} : {what}{when} "
        f"({wf.title}). Tu t'en charges ? Réponds oui ou non."
    )


def propose(
    session: Session, wf: Workflow, assignee: Member, proposer: Member, outbox: Outbox,
    now: datetime | None = None,
) -> tuple[Assignment, bool]:
    """Propose une responsabilite. Renvoie (proposition, envoyee maintenant).

    Proposer revient a partager ce processus avec le foyer : seul son
    proprietaire peut le faire.
    """
    if wf.owner_member_id != proposer.id and wf.visibility != "household":
        raise PermissionError("Seul le propriétaire peut partager ce processus.")
    existing = session.scalar(
        select(Assignment).where(
            Assignment.workflow_id == wf.id,
            Assignment.member_id == assignee.id,
            Assignment.status.in_(["proposed", "accepted"]),
        )
    )
    if existing:
        return existing, existing.delivered
    if wf.visibility != "household":
        wf.visibility = "household"
        log_action(
            session, "workflow_shared", household_id=wf.household_id, member_id=proposer.id,
            actor="member", target=f"workflow:{wf.id}", with_member=assignee.id,
        )
    data = dict(wf.data or {})
    data.pop("suggested_responsible_id", None)
    wf.data = data
    assignment = Assignment(workflow_id=wf.id, member_id=assignee.id, proposed_by_member_id=proposer.id)
    session.add(assignment)
    session.flush()
    delivered = in_window(session, assignee, now)
    if delivered:
        outbox.add(assignee.address, _ask_text(session, wf, proposer))
        assignment.delivered = True
    wf.status = "waiting_other"
    log_action(
        session, "assignment_proposed", household_id=wf.household_id, member_id=proposer.id,
        actor="member", target=f"assignment:{assignment.id}", delivered=delivered,
    )
    return assignment, delivered


def pending_for(session: Session, member: Member) -> list[Assignment]:
    return list(
        session.scalars(
            select(Assignment).where(Assignment.member_id == member.id, Assignment.status == "proposed")
        )
    )


def assignment_line(session: Session, assignment: Assignment) -> str:
    wf = session.get(Workflow, assignment.workflow_id)
    proposer = session.get(Member, assignment.proposed_by_member_id)
    step = engine.current_step(wf)
    when = f", avant le {wf.due_date.isoformat()}" if wf.due_date else ""
    return (
        f"- [R{assignment.id}] {display_name(proposer)} te propose : "
        f"{step.title if step else wf.title} ({wf.title}){when}"
    )


def respond(
    session: Session, assignment: Assignment, accepted: bool, assignee: Member, outbox: Outbox,
    today: date, now: datetime | None = None,
) -> Workflow:
    wf = session.get(Workflow, assignment.workflow_id)
    proposer = session.get(Member, assignment.proposed_by_member_id)
    assignment.status = "accepted" if accepted else "declined"
    assignment.responded_at = now or now_utc()
    if accepted:
        wf.responsible_member_id = assignee.id
        message = f"{display_name(assignee)} s'occupe de : {wf.title}. Tu n'as plus à y penser."
    else:
        wf.responsible_member_id = proposer.id
        message = (
            f"{display_name(assignee)} ne peut pas s'occuper de : {wf.title}. "
            "Je le garde pour toi, ou tu veux que je demande à quelqu'un d'autre ?"
        )
    log_action(
        session, "assignment_answered", household_id=wf.household_id, member_id=assignee.id,
        actor="member", target=f"assignment:{assignment.id}", accepted=accepted,
    )
    engine.refresh(session, wf, today)
    if in_window(session, proposer, now):
        outbox.add(proposer.address, message)
    return wf


def after_step_done(session: Session, wf: Workflow, by: Member, outbox: Outbox, now: datetime | None = None) -> None:
    """Quand quelqu'un d'autre que le proprietaire avance, le proprietaire le
    sait, une fois, sans detail superflu."""
    accepted = session.scalars(
        select(Assignment).where(Assignment.workflow_id == wf.id, Assignment.status == "accepted")
    ).all()
    if not engine.is_open(wf):
        for assignment in accepted:
            assignment.status = "done"
    if by.id == wf.owner_member_id:
        return
    owner = session.get(Member, wf.owner_member_id)
    if owner is None or not in_window(session, owner, now):
        return
    if engine.is_open(wf):
        step = engine.current_step(wf)
        outbox.add(owner.address, f"{display_name(by)} a avancé sur « {wf.title} ». Reste : {step.title if step else '—'}.")
    else:
        outbox.add(owner.address, f"C'est réglé : {wf.title} ({display_name(by)} s'en est occupé).")
