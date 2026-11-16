"""Transcription des vocaux WhatsApp.

Claude ne lit pas l'audio : les vocaux passent par une API de transcription
(OpenAI par defaut, cle OPENAI_API_KEY). Sans cle, MUMU dit simplement qu'il
ne sait pas encore ecouter.
"""

import logging
from typing import Protocol

import httpx

log = logging.getLogger("mumu.voice")

TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_AUDIO_BYTES = 25 * 1024 * 1024
EXTENSIONS = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a",
    "audio/x-m4a": "m4a", "audio/wav": "wav", "audio/webm": "webm", "audio/amr": "amr",
}


class Transcriber(Protocol):
    def transcribe(self, data: bytes, content_type: str) -> str | None: ...


class OpenAITranscriber:
    def __init__(self, api_key: str, model: str = "gpt-4o-transcribe", http: httpx.Client | None = None):
        self._api_key = api_key
        self._model = model
        self._http = http or httpx.Client(timeout=60)

    def transcribe(self, data: bytes, content_type: str) -> str | None:
        base_type = content_type.split(";")[0].strip()
        extension = EXTENSIONS.get(base_type)
        if extension is None or extension == "amr" or len(data) > MAX_AUDIO_BYTES:
            return None
        try:
            response = self._http.post(
                TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                data={"model": self._model, "language": "fr", "response_format": "json"},
                files={"file": (f"vocal.{extension}", data, base_type)},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            log.exception("Transcription impossible")
            return None
        text = (response.json().get("text") or "").strip()
        return text or None
