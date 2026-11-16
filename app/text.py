"""Petits utilitaires de texte."""

import re
import unicodedata
from datetime import date


def norm(value: str | None) -> str:
    """Forme comparable d'un nom : minuscules, sans accents ni ponctuation.

    « EDF Électricité » et « edf electricite » donnent la meme chose.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = decomposed.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def eur(amount: float | None) -> str:
    if amount is None:
        return ""
    text = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    return text.removesuffix(",00") + " €"


def fr_date(value: date | None) -> str:
    if value is None:
        return ""
    mois = [
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ]
    return f"{value.day} {mois[value.month - 1]}"
