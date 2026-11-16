"""« Qu'est-ce que je dois faire cette semaine ? »

Construit une liste priorisee a partir de donnees reelles : processus en
cours, accords attendus, propositions a accepter, taches, agenda, hausses
de prix. Le modele s'en sert pour repondre ; il n'invente rien de plus.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .calendar_logic import _aware, conflict_line, find_tight_spots
from .household import display_name, pending_for, visible_workflows
from .models import Approval, CalendarEvent, Item, Member, Workflow
from .money import price_changes
from .text import fr_date
from .workflows import engine


@dataclass
class AgendaEntry:
    priority: int  # 0 = le plus urgent
    when: date | None
    text: str
    kind: str


def _acts(wf: Workflow, member: Member) -> bool:
    """Est-ce a ce membre d'agir ?"""
    if wf.responsible_member_id:
        return wf.responsible_member_id == member.id
    return wf.owner_member_id == member.id


def week_overview(
    session: Session, member: Member, today: date, days: int = 7, timezone: str = "Europe/Paris"
) -> list[AgendaEntry]:
    horizon = today + timedelta(days=days)
    entries: list[AgendaEntry] = []
    approvals = {
        a.workflow_id: a
        for a in session.scalars(
            select(Approval).where(Approval.member_id == member.id, Approval.status == "pending")
        )
    }

    for wf in visible_workflows(session, member):
        due = wf.due_date
        step = wf.next_action or wf.title
        label = f"{step} ({wf.title})" if step != wf.title else wf.title
        if wf.id in approvals:
            entries.append(AgendaEntry(1, due, f"Ton accord sur un texte préparé : {wf.title}", "approval"))
        elif not _acts(wf, member):
            other = session.get(Member, wf.responsible_member_id) if wf.responsible_member_id else None
            if other and other.id != member.id:
                entries.append(AgendaEntry(6, due, f"{display_name(other)} s'en occupe : {wf.title}", "delegated"))
        elif wf.status == "blocked" and wf.blocker:
            entries.append(AgendaEntry(1, due, f"Bloqué : {wf.title}. {wf.blocker}", "blocked"))
        elif (engine.current_step(wf) is not None and engine.current_step(wf).owner == "evidence"):
            if due and due < today:
                entries.append(AgendaEntry(1, due, f"En retard côté {wf.organization or 'organisme'} : {label}", "late_external"))
            else:
                entries.append(AgendaEntry(5, due, f"En attente : {label}", "waiting"))
        elif due and due < today:
            entries.append(AgendaEntry(0, due, f"En retard depuis le {fr_date(due)} : {label}", "overdue"))
        elif due and due <= horizon:
            priority = 2 if (due - today).days <= 2 else 3
            entries.append(AgendaEntry(priority, due, f"Avant le {fr_date(due)} : {label}", "due"))
        elif not due:
            entries.append(AgendaEntry(4, None, f"Sans date : {label}", "undated"))

    for assignment in pending_for(session, member):
        wf = session.get(Workflow, assignment.workflow_id)
        proposer = session.get(Member, assignment.proposed_by_member_id)
        entries.append(
            AgendaEntry(1, wf.due_date, f"{display_name(proposer)} te propose : {wf.title}", "assignment")
        )

    items = session.scalars(
        select(Item).where(
            Item.household_id == member.household_id,
            Item.status == "open",
            Item.due_date.is_not(None),
            Item.due_date <= horizon,
            (Item.visibility == "household") | (Item.member_id == member.id),
        )
    ).all()
    for item in items:
        priority = 0 if item.due_date < today else (2 if (item.due_date - today).days <= 2 else 3)
        entries.append(AgendaEntry(priority, item.due_date, f"{fr_date(item.due_date)} : {item.title}", "item"))

    zone = ZoneInfo(timezone)
    window_end = datetime.combine(horizon, datetime.max.time(), zone)
    events = [
        e for e in session.scalars(select(CalendarEvent).where(CalendarEvent.member_id == member.id))
        if _aware(e.starts_at).astimezone(zone).date() >= today and _aware(e.starts_at) <= window_end
    ]
    for first, second, kind in find_tight_spots(events):
        entries.append(
            AgendaEntry(2, _aware(second.starts_at).astimezone(zone).date(),
                        conflict_line(events, first, second, kind, timezone).lstrip("! "), "conflict")
        )
    for event in events:
        start = _aware(event.starts_at).astimezone(zone)
        hour = "" if event.all_day else f" à {start:%H:%M}"
        entries.append(AgendaEntry(4, start.date(), f"{fr_date(start.date())}{hour} : {event.title}", "event"))

    for change in price_changes(session, member, since=today - timedelta(days=30)):
        entries.append(AgendaEntry(3, change.new_date, f"Hausse de prix : {change.explain()}", "price"))

    return sorted(entries, key=lambda e: (e.priority, e.when or date.max))


def agenda_lines(entries: list[AgendaEntry], limit: int = 15) -> list[str]:
    return [f"{index + 1}. {entry.text}" for index, entry in enumerate(entries[:limit])]
