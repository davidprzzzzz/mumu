"""Envoi et reception WhatsApp via Twilio."""

import logging
from typing import Protocol

import httpx

from .brain import MAX_MEDIA_BYTES, Media

log = logging.getLogger("mumu.whatsapp")

# Twilio refuse les messages WhatsApp de plus de 1600 caracteres.
MAX_MESSAGE_CHARS = 1500


class Sender(Protocol):
    def send(self, to: str, body: str) -> None: ...


def split_message(body: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Coupe un message trop long sur des fins de ligne quand c'est possible."""
    parts: list[str] = []
    rest = body.strip()
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


class TwilioSender:
    def __init__(self, account_sid: str, auth_token: str, from_address: str):
        from twilio.rest import Client

        self._client = Client(account_sid, auth_token)
        self._from = from_address

    def send(self, to: str, body: str) -> None:
        for part in split_message(body):
            self._client.messages.create(from_=self._from, to=to, body=part)


def signature_is_valid(auth_token: str, url: str, params: dict, signature: str) -> bool:
    from twilio.request_validator import RequestValidator

    return RequestValidator(auth_token).validate(url, params, signature)


def download_media(url: str, content_type: str, account_sid: str, auth_token: str) -> Media | None:
    """Telecharge une piece jointe depuis Twilio. Renvoie None en cas d'echec."""
    try:
        response = httpx.get(
            url, auth=(account_sid, auth_token), follow_redirects=True, timeout=30
        )
        response.raise_for_status()
    except httpx.HTTPError:
        log.exception("Telechargement de la piece jointe impossible")
        return None
    if len(response.content) > MAX_MEDIA_BYTES:
        log.warning("Piece jointe trop lourde : %s octets", len(response.content))
    return Media(content_type=content_type, data=response.content)
