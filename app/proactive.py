"""Le moteur proactif : MUMU repere ce qui merite d'etre dit, et se tait le reste.

Chaque intervention possible est notee (importance x urgence x confiance).
Elle n'est envoyee que si :
- la note atteint le seuil ;
- elle n'a jamais ete envoyee (une cle par intervention et par etape :
  « bientot », puis « en retard », jamais deux fois la meme) ;
- la personne ne vient pas de nous le dire elle-meme ;
- on n'est pas entre 21 h et 8 h ;
- WhatsApp l'autorise (fenetre de 24 h) et MUMU n'a pas deja ecrit deux fois
  de lui-meme dans la journee.
Ce qui n'est pas envoye reste visible dans la vue de la semaine.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .calendar_logic import _aware, find_tight_spots, suggest_slot
from .household import display_name, visible_workflows
from .messaging import already_notified, can_write_first, record_proactive
from .models import Approval, Assignment, CalendarEvent, Member, Workflow, now_utc
from .money import price_changes
from .text import eur, fr_date
from .whatsapp import Sender
from .workflows import engine

log = logging.getLogger("mumu.proactive")

THRESHOLD = 4
MIN_CONFIDENCE = 0.6
QUIET_START, QUIET_END = 21, 8
NO_ANSWER_AFTER = timedelta(days=21)
JUST_TOLD = timedelta(hours=24)


@dataclass
class Candidate:
    key: str
    importance: int  # 1 a 3
    urgency: int  # 1 a 3
    confidence: float
    # Construit le message au moment de l'envoi (peut preparer un brouillon).
    build: Callable[[], str]

    @property
    def score(self) -> int:
        # Une information peu sure ne justifie jamais un message spontane.
        return self.importance * self.urgency if self.confidence >= MIN_CONFIDENCE else 0


def _just_told(wf: Workflow, now: datetime) -> bool:
    """La personne vient de nous en parler elle-meme : inutile de le lui redire."""
    return any(s.source_kind == "whatsapp" for s in wf.sources) and _aware(wf.created_at) > now - JUST_TOLD


def _acts(wf: Workflow, member: Member) -> bool:
    if wf.responsible_member_id:
        return wf.responsible_member_id == member.id
    return wf.owner_member_id == member.id


def _workflow_candidates(session: Session, member: Member, now: datetime, today, timezone: str) -> list[Candidate]:
    candidates = []
    for wf in visible_workflows(session, member):
        if wf.owner_member_id == member.id and (wf.data or {}).get("suggested_responsible_id"):
            other = session.get(Member, wf.data["suggested_responsible_id"])
            if other:
                candidates.append(Candidate(
                    f"wf:{wf.id}:suggest", 2, 2, wf.confidence,
                    lambda wf=wf, other=other: (
                        f"Pour « {wf.title} », je propose à {display_name(other)} de s'en occuper ? "
                        "Je ne lui transmets rien sans ton accord."
                    ),
                ))
        if not _acts(wf, member):
            continue

        if wf.template == "refund":
            refund = next((s for s in wf.steps if s.key == "refund"), None)
            if refund and refund.status == "todo" and wf.due_date and wf.due_date < today:
                def build(wf=wf) -> str:
                    engine.activate(session, wf, "followup", today)
                    approval = session.scalar(
                        select(Approval).where(Approval.workflow_id == wf.id, Approval.status == "pending")
                    )
                    estimate = " (date estimée)" if (wf.data or {}).get("refund_date_is_estimate") else ""
                    draft = f"\n\n« {approval.draft} »\n\nJe te le donne prêt à envoyer ? Réponds oui." if approval else ""
                    return (
                        f"Ton remboursement {wf.organization or ''} aurait dû arriver le "
                        f"{fr_date(wf.due_date)}{estimate}. J'ai préparé une relance :{draft}"
                    )
                candidates.append(Candidate(f"wf:{wf.id}:refund_overdue", 3, 3, wf.confidence, build))
            continue

        if wf.template == "admin":
            submit = next((s for s in wf.steps if s.key == "submit"), None)
            answer = next((s for s in wf.steps if s.key == "answer"), None)
            if submit and answer and submit.status == "done" and answer.status == "todo" \
                    and submit.done_at and _aware(submit.done_at) < now - NO_ANSWER_AFTER:
                candidates.append(Candidate(
                    f"wf:{wf.id}:no_answer", 2, 2, wf.confidence,
                    lambda wf=wf: (
                        f"Toujours pas de réponse de {wf.organization or 'l’organisme'} pour « {wf.title} », "
                        "trois semaines après ton envoi. Je prépare une relance ?"
                    ),
                ))

        step_now = engine.current_step(wf)
        if not wf.due_date or step_now is None or step_now.owner == "evidence":
            continue
        days = (wf.due_date - today).days
        money_at_stake = wf.template in ("payment", "admin", "document_request", "renewal")
        step = wf.next_action or wf.title
        if days < 0:
            candidates.append(Candidate(
                f"wf:{wf.id}:overdue", 3 if money_at_stake else 2, 3, wf.confidence,
                lambda wf=wf, step=step: f"« {wf.title} » était à faire avant le {fr_date(wf.due_date)}. Il reste : {step}. Je peux t'aider ?",
            ))
        elif days <= 3 and not _just_told(wf, now):
            when = "aujourd'hui" if days == 0 else ("demain" if days == 1 else f"le {fr_date(wf.due_date)}")
            amount = f" ({eur(wf.amount_eur)})" if wf.amount_eur and eur(wf.amount_eur) not in step else ""
            candidates.append(Candidate(
                f"wf:{wf.id}:due_soon", 3 if money_at_stake else 2, 3 if days <= 1 else 2, wf.confidence,
                lambda wf=wf, step=step, when=when, amount=amount: f"Petit rappel : {step}{amount}, c'est pour {when}.",
            ))
    return candidates


def _assignment_candidates(session: Session, member: Member, today) -> list[Candidate]:
    candidates = []
    for assignment in session.scalars(
        select(Assignment).where(
            Assignment.member_id == member.id, Assignment.status == "accepted", Assignment.nudged.is_(False)
        )
    ):
        wf = session.get(Workflow, assignment.workflow_id)
        if not engine.is_open(wf) or not wf.due_date or (wf.due_date - today).days > 1:
            continue

        def build(assignment=assignment, wf=wf) -> str:
            assignment.nudged = True
            when = "aujourd'hui" if wf.due_date <= today else "demain"
            return f"Pour {when} : {wf.next_action or wf.title}. Réponds « fait » quand c'est bon."

        candidates.append(Candidate(f"asg:{assignment.id}:nudge", 2, 3, 1.0, build))
    return candidates


def _money_candidates(session: Session, member: Member, today) -> list[Candidate]:
    return [
        Candidate(
            f"price:{change.charge_id}", 2, 2, 1.0,
            lambda change=change: f"{change.explain()} Si tu veux, je t'aide à vérifier s'il existe une formule moins chère.",
        )
        for change in price_changes(session, member, since=today - timedelta(days=30))
    ]


def _calendar_candidates(session: Session, member: Member, now: datetime, timezone: str) -> list[Candidate]:
    zone = ZoneInfo(timezone)
    soon = now + timedelta(days=2)
    events = [
        e for e in session.scalars(select(CalendarEvent).where(CalendarEvent.member_id == member.id))
        if now <= _aware(e.starts_at) <= soon
    ]
    candidates = []
    for first, second, kind in find_tight_spots(events):
        def build(first=first, second=second, kind=kind) -> str:
            start = _aware(second.starts_at).astimezone(zone)
            gap = "se chevauchent" if kind == "chevauchement" else "s'enchaînent sans marge"
            text = f"{fr_date(start.date()).capitalize()}, « {first.title} » et « {second.title} » {gap}."
            slot = suggest_slot(events, second, timezone)
            if slot:
                text += f" Un créneau est libre de {slot[0]:%H:%M} à {slot[1]:%H:%M} si tu veux déplacer « {second.title} »."
            return text

        candidates.append(Candidate(f"cal:{first.external_id}:{second.external_id}", 2, 2, 1.0, build))
    return candidates


def candidates_for(session: Session, member: Member, now: datetime, timezone: str) -> list[Candidate]:
    today = now.astimezone(ZoneInfo(timezone)).date()
    return (
        _workflow_candidates(session, member, now, today, timezone)
        + _assignment_candidates(session, member, today)
        + _money_candidates(session, member, today)
        + _calendar_candidates(session, member, now, timezone)
    )


def is_quiet_time(now: datetime, timezone: str) -> bool:
    hour = now.astimezone(ZoneInfo(timezone)).hour
    return hour >= QUIET_START or hour < QUIET_END


def tick(
    session_factory: sessionmaker,
    sender: Sender,
    timezone: str = "Europe/Paris",
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """Un passage du moteur proactif. Renvoie les messages envoyes."""
    now = now or now_utc()
    sent: list[tuple[str, str]] = []
    if is_quiet_time(now, timezone):
        return sent
    with session_factory() as session:
        for member in session.scalars(select(Member)).all():
            try:
                candidates = sorted(
                    (c for c in candidates_for(session, member, now, timezone)
                     if c.score >= THRESHOLD and not already_notified(session, member, c.key)),
                    key=lambda c: c.score,
                    reverse=True,
                )
                for candidate in candidates:
                    if not can_write_first(session, member, now):
                        break
                    body = candidate.build()
                    sender.send(member.address, body)
                    record_proactive(session, member, body, key=candidate.key)
                    session.flush()
                    sent.append((member.address, body))
                session.commit()
            except Exception:
                session.rollback()
                log.exception("Passage proactif impossible pour le membre %s", member.id)
    return sent
