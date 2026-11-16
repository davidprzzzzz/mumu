"""Formes communes a toutes les sources connectees.

Chaque connecteur (Gmail aujourd'hui, Outlook ou Drive demain) transforme ce
qu'il lit en `SourceDocument` et en `Event`. Le reste de MUMU ne connait que
ces deux formes, jamais l'API du fournisseur.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SourceDocument:
    """Un document lu chez un fournisseur. Jamais enregistre tel quel."""

    source_kind: str  # "gmail", plus tard "outlook", "drive"
    external_id: str
    sender: str
    subject: str
    received_at: datetime
    text: str


@dataclass
class Event:
    external_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False


class AccessRevoked(Exception):
    """Le membre a retire l'acces, ou le jeton a expire : il faut reconnecter."""
