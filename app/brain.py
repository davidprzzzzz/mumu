"""Comprehension d'un message : appel au modele et sortie structuree."""

import base64
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from .persona import SYSTEM_PROMPT

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


@dataclass
class Understanding:
    reply: str
    items: list[dict] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    done_item_ids: list[int] = field(default_factory=list)
    member_name: str | None = None


class Brain(Protocol):
    def understand(self, context: Context, text: str, media: Media | None) -> Understanding: ...


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
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ITEM_KINDS},
                        "title": {"type": "string"},
                        "due_date": {
                            "type": ["string", "null"],
                            "description": "AAAA-MM-JJ, ou null si inconnue.",
                        },
                        "amount_eur": {"type": ["number", "null"]},
                        "owner": {
                            "type": ["string", "null"],
                            "description": "Prénom du responsable, ou MUMU.",
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["kind", "title", "confidence"],
                },
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


def parse_understanding(payload: dict) -> Understanding:
    """Valide la sortie du modele sans lui faire confiance aveuglement."""
    items = []
    for raw in payload.get("items") or []:
        if raw.get("kind") not in ITEM_KINDS or not raw.get("title"):
            continue
        due = raw.get("due_date")
        try:
            due_date = date.fromisoformat(due) if due else None
        except (TypeError, ValueError):
            due_date = None
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
    name = payload.get("member_name")
    return Understanding(
        reply=str(payload.get("reply") or "").strip(),
        items=items,
        facts=[str(f).strip() for f in payload.get("facts") or [] if str(f).strip()],
        done_item_ids=[i for i in payload.get("done_item_ids") or [] if isinstance(i, int)],
        member_name=str(name).strip()[:80] if name else None,
    )


class ClaudeBrain:
    def __init__(self, api_key: str, model: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

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
