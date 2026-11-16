"""Intelligence financiere en lecture seule.

Les montants viennent uniquement de documents reels (factures, avis de
prelevement, plus tard releves bancaires via un agregateur agree). MUMU ne
devine aucun montant, ne conseille pas d'investissement et ne paie rien.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Charge, Member
from .text import eur, norm

CHARGE_CATEGORIES = ["abonnement", "energie", "telecom", "loyer", "assurance", "autre"]
# Une hausse compte a partir de 2 % ET 0,50 € : en dessous, c'est du bruit
# (arrondis, prorata).
MIN_INCREASE_PCT = 0.02
MIN_INCREASE_EUR = 0.50


@dataclass
class PriceChange:
    merchant: str
    merchant_key: str
    old_amount: float
    new_amount: float
    old_date: date
    new_date: date
    charge_id: int

    @property
    def delta(self) -> float:
        return round(self.new_amount - self.old_amount, 2)

    @property
    def pct(self) -> float:
        return self.delta / self.old_amount if self.old_amount else 0.0

    def explain(self) -> str:
        return (
            f"{self.merchant} est passé de {eur(self.old_amount)} ({self.old_date.isoformat()}) "
            f"à {eur(self.new_amount)} ({self.new_date.isoformat()}), "
            f"soit {eur(self.delta)} de plus (+{self.pct:.0%})."
        )


@dataclass
class ExpectedCharge:
    merchant: str
    amount: float
    expected_on: date


def record_charge(
    session: Session,
    member: Member,
    *,
    merchant: str,
    amount: float,
    charged_on: date,
    category: str,
    source_kind: str,
    source_ref: str,
) -> Charge | None:
    """Enregistre un montant reel. Un meme document n'est compte qu'une fois."""
    key = norm(merchant)
    if not key or amount is None or amount <= 0:
        return None
    existing = session.scalar(
        select(Charge).where(
            Charge.member_id == member.id,
            Charge.source_kind == source_kind,
            Charge.source_ref == source_ref,
            Charge.merchant_key == key,
        )
    )
    if existing is not None:
        return existing
    charge = Charge(
        household_id=member.household_id,
        member_id=member.id,
        merchant=merchant[:120],
        merchant_key=key[:120],
        amount_eur=round(float(amount), 2),
        charged_on=charged_on,
        category=category if category in CHARGE_CATEGORIES else "autre",
        source_kind=source_kind,
        source_ref=source_ref[:200],
    )
    session.add(charge)
    session.flush()
    return charge


def _history(session: Session, member: Member) -> dict[str, list[Charge]]:
    charges = session.scalars(
        select(Charge).where(Charge.member_id == member.id).order_by(Charge.charged_on, Charge.id)
    ).all()
    by_merchant: dict[str, list[Charge]] = {}
    for charge in charges:
        by_merchant.setdefault(charge.merchant_key, []).append(charge)
    return by_merchant


def price_changes(session: Session, member: Member, since: date | None = None) -> list[PriceChange]:
    """Hausses entre deux montants successifs d'un meme marchand."""
    changes = []
    for history in _history(session, member).values():
        for previous, current in zip(history, history[1:]):
            if since and current.charged_on < since:
                continue
            delta = current.amount_eur - previous.amount_eur
            if delta >= MIN_INCREASE_EUR and delta / previous.amount_eur >= MIN_INCREASE_PCT:
                changes.append(
                    PriceChange(
                        merchant=current.merchant,
                        merchant_key=current.merchant_key,
                        old_amount=previous.amount_eur,
                        new_amount=current.amount_eur,
                        old_date=previous.charged_on,
                        new_date=current.charged_on,
                        charge_id=current.id,
                    )
                )
    return changes


def expected_charges(session: Session, member: Member, today: date, until: date) -> list[ExpectedCharge]:
    """Prelevements attendus d'ici `until`, deduits des montants reels deja vus.

    Un marchand compte comme recurrent apres au moins deux montants espaces
    de 25 a 35 jours. La date attendue est la derniere date plus l'ecart
    habituel ; le montant attendu est le dernier montant.
    """
    expected = []
    for history in _history(session, member).values():
        if len(history) < 2:
            continue
        gaps = [(b.charged_on - a.charged_on).days for a, b in zip(history, history[1:])]
        if not all(25 <= gap <= 35 for gap in gaps):
            continue
        last = history[-1]
        next_date = last.charged_on + timedelta(days=round(median(gaps)))
        while next_date < today:
            next_date += timedelta(days=round(median(gaps)))
        if next_date <= until:
            expected.append(ExpectedCharge(last.merchant, last.amount_eur, next_date))
    return sorted(expected, key=lambda e: e.expected_on)


def money_lines(session: Session, member: Member, today: date) -> list[str]:
    """Resume chiffre pour le contexte du modele, uniquement sur donnees reelles."""
    lines = []
    for change in price_changes(session, member, since=today - timedelta(days=60)):
        lines.append(f"- Hausse : {change.explain()}")
    month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    upcoming = expected_charges(session, member, today, month_end)
    if upcoming:
        total = sum(e.amount for e in upcoming)
        detail = ", ".join(f"{e.merchant} {eur(e.amount)} vers le {e.expected_on.isoformat()}" for e in upcoming)
        lines.append(
            f"- Prélèvements récurrents attendus d'ici la fin du mois : {eur(total)} ({detail}). "
            "Estimation à partir des montants déjà vus, pas d'un relevé bancaire."
        )
    return lines
