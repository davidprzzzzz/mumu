"""Formes communes a toutes les sources connectees.

Chaque connecteur (Gmail aujourd'hui, Outlook ou Drive demain) transforme ce
qu'il lit en `SourceDocument` et en `Event`. Le reste de MUMU ne connait que
ces deux formes, jamais l'API du fournisseur.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class AttachmentRef:
    """Une piece jointe, pas encore telechargee."""

    attachment_id: str
    filename: str
    mime_type: str
    size: int


@dataclass
class SourceDocument:
    """Un document lu chez un fournisseur. Jamais enregistre tel quel."""

    source_kind: str  # "gmail", plus tard "outlook", "drive"
    external_id: str
    sender: str
    subject: str
    received_at: datetime
    text: str
    attachments: list[AttachmentRef] = field(default_factory=list)


@dataclass
class Event:
    external_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False


class AccessRevoked(Exception):
    """Le membre a retire l'acces, ou le jeton a expire : il faut reconnecter."""


class Provider(Protocol):
    """Ce qu'un fournisseur de mails et d'agenda doit savoir faire.

    `GoogleClient` le fait. Un connecteur Outlook (Microsoft Graph) n'aurait
    qu'a implementer ces methodes : la synchronisation, le classement, la
    confidentialite et les notifications sont communs.
    """

    def refresh(self, refresh_token: str) -> str: ...

    def revoke(self, refresh_token: str) -> None: ...

    def list_message_ids(self, access_token: str, after_ts: int, limit: int = 50) -> list[str]: ...

    def get_message(self, access_token: str, message_id: str) -> SourceDocument | None: ...

    def get_attachment(self, access_token: str, message_id: str, attachment_id: str) -> bytes: ...

    def list_events(self, access_token: str, start: datetime, days: int = 14) -> list[Event]: ...
