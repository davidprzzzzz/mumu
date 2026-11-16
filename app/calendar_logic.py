"""Reperage des chevauchements et des enchainements serres dans un agenda."""

from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

TIGHT_GAP = timedelta(minutes=30)


class Timed(Protocol):
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool


def find_tight_spots(events: list[Timed], gap: timedelta = TIGHT_GAP) -> list[tuple[Timed, Timed, str]]:
    """Renvoie les paires d'evenements qui se chevauchent ou se suivent de trop pres.

    Chaque resultat est (premier, second, "chevauchement" ou "serre").
    Les evenements sur la journee entiere sont ignores.
    """
    timed = sorted((e for e in events if not e.all_day), key=lambda e: e.starts_at)
    spots = []
    for index, first in enumerate(timed):
        for second in timed[index + 1 :]:
            if second.starts_at >= first.ends_at + gap:
                break
            kind = "chevauchement" if second.starts_at < first.ends_at else "serre"
            spots.append((first, second, kind))
    return spots


def describe_calendar(events: list[Timed], timezone: str) -> list[str]:
    """Met l'agenda et ses points serres en lignes lisibles par le modele."""
    zone = ZoneInfo(timezone)

    def aware(moment: datetime) -> datetime:
        # SQLite rend des dates sans fuseau : elles sont stockees en UTC.
        return moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo("UTC"))

    def when(event: Timed) -> str:
        if event.all_day:
            return event.starts_at.strftime("%Y-%m-%d") + " (journée)"
        start, end = aware(event.starts_at).astimezone(zone), aware(event.ends_at).astimezone(zone)
        return f"{start:%Y-%m-%d %H:%M}-{end:%H:%M}"

    lines = [f"- {when(e)} : {e.title}" for e in sorted(events, key=lambda e: e.starts_at)]
    for first, second, kind in find_tight_spots(events):
        label = "se chevauchent" if kind == "chevauchement" else "s'enchaînent sans marge"
        lines.append(f"! « {first.title} » et « {second.title} » {label} ({when(second)})")
    return lines
