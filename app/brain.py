"""Comprehension d'un message : appel au modele et sortie structuree."""

import base64
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from .connectors.base import SourceDocument
from .persona import INGEST_PROMPT, SYSTEM_PROMPT

ITEM_KINDS = ["tache", "echeance", "paiement", "rendez_vous", "course"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

# Limite de taille acceptee pour une image ou un PDF envoye au modele.
MAX_MEDIA_BYTES = 5 * 1024 * 1024


@dataclass
class Media:
    content_type: str
    data: bytes


@dataclass
class Context:
    today: date
    member_name: str | None
    facts: list[str]
    # (id, titre, date d'echeance ou None)
    open_items: list[tuple[int, str, date | None]]
    # (direction, texte), du plus ancien au plus recent
    recent_messages: list[tuple[str, str]]
    # Agenda des 14 prochains jours et achats recents, si un service est connecte.
    calendar_lines: list[str] = field(default_factory=list)
    purchase_lines: list[str] = field(default_factory=list)
    google_connected: bool = False


@dataclass
class Understanding:
    reply: str
    items: list[dict] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    done_item_ids: list[int] = field(default_factory=list)
    member_name: str | None = None
    # "connect_google", "disconnect_google" ou None.
    action: str | None = None


@dataclass
class Ingestion:
    """Ce que MUMU retient d'un document lu dans un service connecte."""

    relevant: bool
    label: str = ""
    items: list[dict] = field(default_factory=list)
    purchases: list[dict] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    # "maintenant", "recap" ou "silence".
    notify: str = "silence"
    message: str = ""


class Brain(Protocol):
    def understand(self, context: Context, text: str, media: Media | None) -> Understanding: ...

    def ingest(self, context: Context, document: SourceDocument) -> Ingestion: ...


ACTIONS = ["connect_google", "disconnect_google"]
NOTIFY_LEVELS = ["maintenant", "recap", "silence"]

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ITEM_KINDS},
        "title": {"type": "string"},
        "due_date": {"type": ["string", "null"], "description": "AAAA-MM-JJ, ou null si inconnue."},
        "amount_eur": {"type": ["number", "null"]},
        "owner": {"type": ["string", "null"], "description": "Prénom du responsable, ou MUMU."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["kind", "title", "confidence"],
}

INGEST_TOOL = {
    "name": "classer",
    "description": "Classe un document lu dans un service connecté et en tire ce qui compte.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevant": {
                "type": "boolean",
                "description": "Faux pour tout ce qui ne demande ni action ni mémoire.",
            },
            "label": {
                "type": "string",
                "description": "Libellé court de la source, ex. « Mail EDF du 12/11 ».",
            },
            "items": {"type": "array", "items": ITEM_SCHEMA},
            "purchases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "merchant": {"type": "string"},
                        "description": {"type": "string"},
                        "amount_eur": {"type": ["number", "null"]},
                        "ordered_on": {"type": ["string", "null"]},
                        "delivery_expected": {"type": ["string", "null"]},
                        "order_ref": {"type": ["string", "null"]},
                    },
                    "required": ["merchant", "description"],
                },
            },
            "facts": {"type": "array", "items": {"type": "string"}},
            "notify": {"type": "string", "enum": NOTIFY_LEVELS},
            "message": {
                "type": "string",
                "description": "Le message WhatsApp, seulement si notify vaut « maintenant ».",
            },
        },
        "required": ["relevant", "notify"],
    },
}

REPLY_TOOL = {
    "name": "repondre",
    "description": "Répond au membre du foyer et enregistre ce qui doit être suivi ou retenu.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "Le message WhatsApp à envoyer."},
            "items": {
                "type": "array",
                "description": "Nouveaux éléments à suivre tirés de ce message.",
                "items": ITEM_SCHEMA,
            },
            "facts": {
                "type": "array",
                "description": "Faits durables sur le foyer appris dans ce message.",
                "items": {"type": "string"},
            },
            "done_item_ids": {
                "type": "array",
                "description": "Identifiants des éléments en cours que ce message clôt.",
                "items": {"type": "integer"},
            },
            "member_name": {
                "type": ["string", "null"],
                "description": "Prénom de la personne, si elle vient de le donner.",
            },
            "action": {
                "type": ["string", "null"],
                "enum": ACTIONS + [None],
                "description": "connect_google si la personne veut connecter sa boîte mail ou son agenda, disconnect_google si elle veut retirer l'accès.",
            },
        },
        "required": ["reply"],
    },
}


def render_context(context: Context) -> str:
    """Met en texte ce que MUMU sait deja, pour le modele."""
    jour = JOURS[context.today.weekday()]
    lines = [f"Date du jour : {jour} {context.today.isoformat()} (heure de Paris)."]
    lines.append(f"Prénom de la personne : {context.member_name or 'inconnu'}.")

    lines.append("\nCe que tu sais du foyer :")
    lines.extend(f"- {fact}" for fact in context.facts)
    if not context.facts:
        lines.append("- rien pour l'instant")

    lines.append("\nÉléments en cours :")
    for item_id, title, due in context.open_items:
        echeance = f", pour le {due.isoformat()}" if due else ""
        lines.append(f"- [{item_id}] {title}{echeance}")
    if not context.open_items:
        lines.append("- aucun")

    if context.calendar_lines:
        lines.append("\nAgenda des 14 prochains jours (les lignes « ! » signalent un point serré) :")
        lines.extend(context.calendar_lines)
    if context.purchase_lines:
        lines.append("\nAchats récents :")
        lines.extend(context.purchase_lines)
    connecte = "oui" if context.google_connected else "non"
    lines.append(f"\nBoîte mail et agenda Google connectés : {connecte}.")

    lines.append("\nDerniers échanges :")
    for direction, body in context.recent_messages:
        qui = "MUMU" if direction == "out" else "Membre"
        lines.append(f"{qui} : {body}")
    if not context.recent_messages:
        lines.append("(premier message)")
    return "\n".join(lines)


def media_block(media: Media) -> dict | None:
    """Convertit une piece jointe en bloc lisible par le modele, si possible."""
    if len(media.data) > MAX_MEDIA_BYTES:
        return None
    data = base64.standard_b64encode(media.data).decode()
    if media.content_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media.content_type, "data": data},
        }
    if media.content_type == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }
    return None


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _parse_items(raw_items) -> list[dict]:
    items = []
    for raw in raw_items or []:
        if raw.get("kind") not in ITEM_KINDS or not raw.get("title"):
            continue
        due_date = _parse_date(raw.get("due_date"))
        amount = raw.get("amount_eur")
        confidence = raw.get("confidence")
        items.append(
            {
                "kind": raw["kind"],
                "title": str(raw["title"])[:200],
                "due_date": due_date,
                "amount_eur": float(amount) if isinstance(amount, (int, float)) else None,
                "owner": (str(raw["owner"])[:80] if raw.get("owner") else None),
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else 0.5,
            }
        )
    return items


def parse_understanding(payload: dict) -> Understanding:
    """Valide la sortie du modele sans lui faire confiance aveuglement."""
    name = payload.get("member_name")
    action = payload.get("action")
    return Understanding(
        action=action if action in ACTIONS else None,
        reply=str(payload.get("reply") or "").strip(),
        items=_parse_items(payload.get("items")),
        facts=[str(f).strip() for f in payload.get("facts") or [] if str(f).strip()],
        done_item_ids=[i for i in payload.get("done_item_ids") or [] if isinstance(i, int)],
        member_name=str(name).strip()[:80] if name else None,
    )


def parse_ingestion(payload: dict) -> Ingestion:
    purchases = []
    for raw in payload.get("purchases") or []:
        if not raw.get("merchant") or not raw.get("description"):
            continue
        amount = raw.get("amount_eur")
        purchases.append(
            {
                "merchant": str(raw["merchant"])[:120],
                "description": str(raw["description"])[:300],
                "amount_eur": float(amount) if isinstance(amount, (int, float)) else None,
                "ordered_on": _parse_date(raw.get("ordered_on")),
                "delivery_expected": _parse_date(raw.get("delivery_expected")),
                "order_ref": str(raw["order_ref"])[:120] if raw.get("order_ref") else None,
            }
        )
    notify = payload.get("notify")
    message = str(payload.get("message") or "").strip()
    if notify not in NOTIFY_LEVELS or (notify == "maintenant" and not message):
        notify = "recap"
    return Ingestion(
        relevant=bool(payload.get("relevant")),
        label=str(payload.get("label") or "")[:200],
        items=_parse_items(payload.get("items")),
        purchases=purchases,
        facts=[str(f).strip() for f in payload.get("facts") or [] if str(f).strip()],
        notify=notify,
        message=message,
    )


class ClaudeBrain:
    def __init__(self, api_key: str, model: str, ingest_model: str | None = None):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._ingest_model = ingest_model or model

    def ingest(self, context: Context, document: SourceDocument) -> Ingestion:
        text = (
            f"{render_context(context)}\n\n"
            f"Document à classer ({document.source_kind}) :\n"
            f"De : {document.sender}\n"
            f"Objet : {document.subject}\n"
            f"Reçu le : {document.received_at.date().isoformat()}\n\n"
            f"{document.text}"
        )
        response = self._client.messages.create(
            model=self._ingest_model,
            max_tokens=1000,
            system=INGEST_PROMPT,
            tools=[INGEST_TOOL],
            tool_choice={"type": "tool", "name": "classer"},
            messages=[{"role": "user", "content": text}],
        )
        for block in response.content:
            if block.type == "tool_use":
                return parse_ingestion(block.input)
        raise RuntimeError("Le modèle n'a pas classé le document.")

    def understand(self, context: Context, text: str, media: Media | None) -> Understanding:
        content: list[dict] = []
        note = ""
        if media is not None:
            block = media_block(media)
            if block is not None:
                content.append(block)
            elif media.content_type.startswith("audio/"):
                note = "\n[La personne a envoyé un vocal. Tu ne sais pas encore les écouter : dis-le simplement et demande un message écrit.]"
            else:
                note = f"\n[Pièce jointe non lisible ({media.content_type}). Dis-le simplement.]"

        content.append(
            {
                "type": "text",
                "text": f"{render_context(context)}\n\nNouveau message :\n{text or '(sans texte)'}{note}",
            }
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=[REPLY_TOOL],
            tool_choice={"type": "tool", "name": "repondre"},
            messages=[{"role": "user", "content": content}],
        )
        for block in response.content:
            if block.type == "tool_use":
                understanding = parse_understanding(block.input)
                if understanding.reply:
                    return understanding
        raise RuntimeError("Le modèle n'a pas renvoyé de réponse exploitable.")
