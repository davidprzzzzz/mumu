"""Connecteur Google : autorisation OAuth, lecture Gmail et Google Agenda.

Lecture seule. MUMU ne modifie, n'envoie et ne supprime rien chez Google.
"""

import base64
import html
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from .base import AccessRevoked, AttachmentRef, Event, SourceDocument

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

# Les deux permissions demandees, les plus etroites possibles pour lire.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
]

# Mails que MUMU ne lit jamais : publicite, reseaux sociaux, indesirables,
# brouillons et messages envoyes.
SKIPPED_LABELS = {
    "SPAM",
    "TRASH",
    "DRAFT",
    "SENT",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_FORUMS",
}
GMAIL_QUERY_FILTER = "-category:promotions -category:social -category:forums -in:sent -in:draft"

MAX_TEXT_CHARS = 6000
# Pieces jointes lues : PDF et images, 3 au plus, 5 Mo au plus chacune.
ATTACHMENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def _decode_body(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")


def _strip_html(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _collect_parts(payload: dict, plain: list[str], rich: list[str], files: list[AttachmentRef]) -> None:
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    data = body.get("data")
    if payload.get("filename") and body.get("attachmentId"):
        if mime in ATTACHMENT_TYPES and int(body.get("size") or 0) <= MAX_ATTACHMENT_BYTES:
            files.append(
                AttachmentRef(body["attachmentId"], payload["filename"][:200], mime, int(body.get("size") or 0))
            )
    elif data and mime == "text/plain":
        plain.append(_decode_body(data))
    elif data and mime == "text/html":
        rich.append(_decode_body(data))
    for part in payload.get("parts") or []:
        _collect_parts(part, plain, rich, files)


def parse_gmail_message(raw: dict) -> SourceDocument | None:
    """Convertit un message Gmail en document, ou None s'il doit etre ignore."""
    if SKIPPED_LABELS & set(raw.get("labelIds") or []):
        return None
    payload = raw.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
    plain: list[str] = []
    rich: list[str] = []
    files: list[AttachmentRef] = []
    _collect_parts(payload, plain, rich, files)
    text = "\n".join(plain).strip() or _strip_html("\n".join(rich)) or raw.get("snippet", "")
    if not text and not files:
        return None
    received = datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, tz=timezone.utc)
    return SourceDocument(
        source_kind="gmail",
        external_id=raw["id"],
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        received_at=received,
        text=text[:MAX_TEXT_CHARS],
        attachments=files[:MAX_ATTACHMENTS],
    )


def parse_calendar_event(raw: dict) -> Event | None:
    if raw.get("status") == "cancelled":
        return None
    start, end = raw.get("start") or {}, raw.get("end") or {}
    try:
        if "dateTime" in start:
            starts_at = datetime.fromisoformat(start["dateTime"])
            ends_at = datetime.fromisoformat(end["dateTime"])
            all_day = False
        else:
            starts_at = datetime.combine(date.fromisoformat(start["date"]), datetime.min.time(), timezone.utc)
            ends_at = datetime.combine(date.fromisoformat(end["date"]), datetime.min.time(), timezone.utc)
            all_day = True
    except (KeyError, ValueError):
        return None
    return Event(
        external_id=raw.get("id", ""),
        title=(raw.get("summary") or "(sans titre)")[:200],
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
    )


class GoogleClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http: httpx.Client | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = http or httpx.Client(timeout=30)

    # --- Autorisation -----------------------------------------------------

    def auth_url(self, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # "offline" + "consent" : Google renvoie un jeton de rafraichissement.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict:
        response = self._http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        return response.json()

    def refresh(self, refresh_token: str) -> str:
        """Echange le jeton de rafraichissement contre un jeton d'acces court."""
        response = self._http.post(
            TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code in (400, 401):
            raise AccessRevoked(response.text[:200])
        response.raise_for_status()
        return response.json()["access_token"]

    def revoke(self, refresh_token: str) -> None:
        self._http.post(REVOKE_URL, data={"token": refresh_token})

    # --- Lecture ----------------------------------------------------------

    def _get(self, url: str, access_token: str, params: dict) -> dict:
        response = self._http.get(
            url, params=params, headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code == 401:
            raise AccessRevoked("jeton refuse")
        response.raise_for_status()
        return response.json()

    def list_message_ids(self, access_token: str, after_ts: int, limit: int = 50) -> list[str]:
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            params = {
                "q": f"after:{after_ts} {GMAIL_QUERY_FILTER}",
                "maxResults": min(50, limit - len(ids)),
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get(f"{GMAIL_URL}/messages", access_token, params)
            ids.extend(m["id"] for m in data.get("messages") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids[:limit]

    def get_message(self, access_token: str, message_id: str) -> SourceDocument | None:
        raw = self._get(f"{GMAIL_URL}/messages/{message_id}", access_token, {"format": "full"})
        return parse_gmail_message(raw)

    def get_attachment(self, access_token: str, message_id: str, attachment_id: str) -> bytes:
        data = self._get(
            f"{GMAIL_URL}/messages/{message_id}/attachments/{attachment_id}", access_token, {}
        )
        encoded = data.get("data", "")
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))

    def list_events(self, access_token: str, start: datetime, days: int = 14) -> list[Event]:
        data = self._get(
            CALENDAR_URL,
            access_token,
            {
                "timeMin": start.isoformat(),
                "timeMax": (start + timedelta(days=days)).isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 100,
            },
        )
        events = (parse_calendar_event(raw) for raw in data.get("items") or [])
        return [event for event in events if event is not None]
