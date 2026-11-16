"""Reperage des chevauchements et des enchainements serres dans un agenda,
et proposition d'un creneau libre."""

from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

TIGHT_GAP = timedelta(minutes=30)
DAY_START = time(8, 0)
DAY_END = time(20, 0)
UTC = ZoneInfo("UTC")


class Timed(Protocol):
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool


def _aware(moment: datetime) -> datetime:
    # SQLite rend des dates sans fuseau : elles sont stockees en UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def find_tight_spots(events: list[Timed], gap: timedelta = TIGHT_GAP) -> list[tuple[Timed, Timed, str]]:
    """Renvoie les paires d'evenements qui se chevauchent ou se suivent de trop pres.

    Chaque resultat est (premier, second, "chevauchement" ou "serre").
    Les evenements sur la journee entiere sont ignores.
    """
    timed = sorted((e for e in events if not e.all_day), key=lambda e: _aware(e.starts_at))
    spots = []
    for index, first in enumerate(timed):
        for second in timed[index + 1 :]:
            if _aware(second.starts_at) >= _aware(first.ends_at) + gap:
                break
            kind = "chevauchement" if _aware(second.starts_at) < _aware(first.ends_at) else "serre"
            spots.append((first, second, kind))
    return spots


def suggest_slot(
    events: list[Timed], moving: Timed, timezone: str, gap: timedelta = TIGHT_GAP
) -> tuple[datetime, datetime] | None:
    """Premier creneau libre le meme jour, de la meme duree, avec une marge
    de 30 minutes autour des autres evenements. None s'il n'y en a pas."""
    zone = ZoneInfo(timezone)
    start = _aware(moving.starts_at).astimezone(zone)
    duration = _aware(moving.ends_at) - _aware(moving.starts_at)
    day_start = datetime.combine(start.date(), DAY_START, zone)
    day_end = datetime.combine(start.date(), DAY_END, zone)
    busy = sorted(
        (
            (_aware(e.starts_at).astimezone(zone) - gap, _aware(e.ends_at).astimezone(zone) + gap)
            for e in events
            if e is not moving and not e.all_day
        ),
        key=lambda span: span[0],
    )
    candidate = day_start
    for busy_start, busy_end in busy:
        if candidate + duration <= busy_start:
            break
        candidate = max(candidate, busy_end)
    if candidate + duration <= day_end:
        return candidate, candidate + duration
    return None


def conflict_line(events: list[Timed], first: Timed, second: Timed, kind: str, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    label = "se chevauchent" if kind == "chevauchement" else "s'enchaînent sans marge"
    start = _aware(second.starts_at).astimezone(zone)
    line = f"! « {first.title} » et « {second.title} » {label} ({start:%Y-%m-%d %H:%M})"
    slot = suggest_slot(events, second, timezone)
    if slot:
        line += f" — créneau libre possible pour « {second.title} » : {slot[0]:%H:%M}-{slot[1]:%H:%M}"
    return line


def describe_calendar(events: list[Timed], timezone: str) -> list[str]:
    """Met l'agenda et ses points serres en lignes lisibles par le modele."""
    zone = ZoneInfo(timezone)

    def when(event: Timed) -> str:
        if event.all_day:
            return _aware(event.starts_at).strftime("%Y-%m-%d") + " (journée)"
        start, end = _aware(event.starts_at).astimezone(zone), _aware(event.ends_at).astimezone(zone)
        return f"{start:%Y-%m-%d %H:%M}-{end:%H:%M}"

    lines = [f"- {when(e)} : {e.title}" for e in sorted(events, key=lambda e: _aware(e.starts_at))]
    for first, second, kind in find_tight_spots(events):
        lines.append(conflict_line(events, first, second, kind, timezone))
    return lines
