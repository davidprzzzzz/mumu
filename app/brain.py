"""Comprehension : appel au modele et validation stricte de sa sortie.

Le modele propose ; le code verifie. Aucun identifiant renvoye par le
modele n'est utilise sans avoir ete controle par le service (appartenance au
foyer, droit de voir).
"""

import base64
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from .connectors.base import SourceDocument
from .money import CHARGE_CATEGORIES
from .persona import INGEST_PROMPT, SYSTEM_PROMPT
from .signals import LIFE_EVENTS, SIGNAL_TYPES, Signal
from .workflows.templates import DOCUMENT_KINDS

ITEM_KINDS = ["tache", "echeance", "paiement", "rendez_vous", "course"]
HOUSEHOLD_KINDS = ["seul", "couple", "famille", "coloc"]
ACTIONS = ["connect_google", "disconnect_google"]
NOTIFY_LEVELS = ["maintenant", "recap", "silence"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

# Limite de taille acceptee pour une image ou un PDF envoye au modele.
MAX_MEDIA_BYTES = 5 * 1024 * 1024


@dataclass
class Media:
    content_type: str
    data: bytes
    filename: str | None = None


@dataclass
class Context:
    today: date
    member_name: str | None
    facts: list[str]
    # (id, titre, date d'echeance ou None)
    open_items: list[tuple[int, str, date | None]]
    # (direction, texte), du plus ancien au plus recent
    recent_messages: list[tuple[str, str]]
    calendar_lines: list[str] = field(default_factory=list)
    purchase_lines: list[str] = field(default_factory=list)
    google_connected: bool = False
    household_kind: str | None = None
    member_lines: list[str] = field(default_factory=list)
    people_lines: list[str] = field(default_factory=list)
    workflow_lines: list[str] = field(default_factory=list)
    approval_lines: list[str] = field(default_factory=list)
    assignment_lines: list[str] = field(default_factory=list)
    document_lines: list[str] = field(default_factory=list)
    money_lines: list[str] = field(default_factory=list)
    agenda_lines: list[str] = field(default_factory=list)


@dataclass
class DocumentMeta:
    index: int
    kind: str
    title: str
    issuer: str | None = None
    issued_on: date | None = None
    valid_until: date | None = None
    summary: str = ""


@dataclass
class Understanding:
    reply: str
    items: list[dict] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    done_item_ids: list[int] = field(default_factory=list)
    member_name: str | None = None
    action: str | None = None
    signals: list[Signal] = field(default_factory=list)
    documents: list[DocumentMeta] = field(default_factory=list)
    household_kind: str | None = None
    people: list[dict] = field(default_factory=list)
    approve_ids: list[int] = field(default_factory=list)
    reject_ids: list[int] = field(default_factory=list)
    # (processus, cle d'etape ou None)
    completed_steps: list[tuple[int, str | None]] = field(default_factory=list)
    close_workflow_ids: list[int] = field(default_factory=list)
    # (processus, prenom du membre)
    assign: list[tuple[int, str]] = field(default_factory=list)
    accept_ids: list[int] = field(default_factory=list)
    decline_ids: list[int] = field(default_factory=list)
    invite_name: str | None = None
    send_document_ids: list[int] = field(default_factory=list)


@dataclass
class Ingestion:
    """Ce que MUMU retient d'un document lu dans un service connecte."""

    relevant: bool
    label: str = ""
    items: list[dict] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    documents: list[DocumentMeta] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    # "maintenant", "recap" ou "silence".
    notify: str = "silence"
    message: str = ""


class Brain(Protocol):
    def understand(self, context: Context, text: str, media: Media | None) -> Understanding: ...

    def ingest(
        self, context: Context, document: SourceDocument, attachments: list[Media] | None = None
    ) -> Ingestion: ...


# --- Schemas des outils ------------------------------------------------------

NULLABLE_STRING = {"type": ["string", "null"]}
DATE = {"type": ["string", "null"], "description": "AAAA-MM-JJ, ou null si inconnue."}
INTS = {"type": "array", "items": {"type": "integer"}}

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ITEM_KINDS},
        "title": {"type": "string"},
        "due_date": DATE,
        "amount_eur": {"type": ["number", "null"]},
        "owner": {"type": ["string", "null"], "description": "Prénom du responsable, ou MUMU."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["kind", "title", "confidence"],
}

SIGNAL_SCHEMA = {
    "type": "object",
    "description": "Une situation qui demande un suivi, ou qui en clôt un.",
    "properties": {
        "type": {"type": "string", "enum": SIGNAL_TYPES},
        "title": {"type": "string", "description": "Titre court, ex. « Facture EDF de novembre »."},
        "summary": {
            "type": "string",
            "description": "Ce que c'est et ce que ça demande, en langage simple, deux phrases au plus.",
        },
        "organization": NULLABLE_STRING,
        "reference": {"type": ["string", "null"], "description": "N° de commande, de facture ou de dossier."},
        "amount_eur": {"type": ["number", "null"]},
        "due_date": {
            "type": ["string", "null"],
            "description": "Date limite (AAAA-MM-JJ). Pour un retour : date de remboursement annoncée.",
        },
        "event_date": {
            "type": ["string", "null"],
            "description": "Date de l'événement, de la commande, de la facture ou du retour.",
        },
        "person": {"type": ["string", "null"], "description": "Prénom de l'enfant ou de la personne concernée."},
        "responsible": {
            "type": ["string", "null"],
            "description": "Prénom du membre du foyer désigné pour s'en occuper, seulement s'il est nommé.",
        },
        "actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Actions concrètes, ex. « Signer l'autorisation de sortie ».",
        },
        "required_documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "kind": {"type": "string", "enum": list(DOCUMENT_KINDS)}},
                "required": ["label"],
            },
        },
        "requested_document": {"type": ["string", "null"], "enum": list(DOCUMENT_KINDS) + [None]},
        "response_required": {"type": "boolean", "description": "Faux si le courrier est seulement informatif."},
        "category": {"type": ["string", "null"], "enum": CHARGE_CATEGORIES + [None]},
        "product": NULLABLE_STRING,
        "return_window_days": {"type": ["integer", "null"], "description": "Seulement si le marchand l'indique."},
        "warranty_months": {"type": ["integer", "null"], "description": "Seulement si le document l'indique."},
        "life_event": {"type": ["string", "null"], "enum": LIFE_EVENTS + [None]},
        "workflow_id": {
            "type": ["integer", "null"],
            "description": "Identifiant [W..] d'un processus en cours auquel ceci se rattache.",
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["type", "title", "confidence"],
}

DOCUMENT_SCHEMA = {
    "type": "object",
    "description": "Un document reçu en pièce jointe ou en photo, à garder.",
    "properties": {
        "index": {"type": "integer", "description": "Numéro de la pièce jointe, à partir de 0."},
        "kind": {"type": "string", "enum": list(DOCUMENT_KINDS)},
        "title": {"type": "string"},
        "issuer": NULLABLE_STRING,
        "issued_on": DATE,
        "valid_until": DATE,
        "summary": {"type": "string", "description": "Une phrase. Jamais de numéro d'identité, de compte ou de carte."},
    },
    "required": ["index", "kind", "title"],
}

REPLY_TOOL = {
    "name": "repondre",
    "description": "Répond au membre du foyer et enregistre ce qui doit être suivi, retenu ou décidé.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "Le message WhatsApp à envoyer."},
            "items": {"type": "array", "description": "Choses simples à suivre (tâche, rendez-vous, course).", "items": ITEM_SCHEMA},
            "signals": {"type": "array", "description": "Situations à suivre de bout en bout.", "items": SIGNAL_SCHEMA},
            "documents": {"type": "array", "description": "Documents à garder parmi les pièces envoyées.", "items": DOCUMENT_SCHEMA},
            "facts": {"type": "array", "description": "Faits durables sur le foyer.", "items": {"type": "string"}},
            "done_item_ids": INTS,
            "member_name": {"type": ["string", "null"], "description": "Prénom de la personne, si elle vient de le donner."},
            "household_kind": {"type": ["string", "null"], "enum": HOUSEHOLD_KINDS + [None]},
            "people": {
                "type": "array",
                "description": "Enfants ou proches du foyer qui ne parlent pas à MUMU, s'ils viennent d'être nommés.",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "relation": {"type": "string"}},
                    "required": ["name"],
                },
            },
            "approve_ids": {**INTS, "description": "Accords [A..] que la personne vient de donner explicitement."},
            "reject_ids": {**INTS, "description": "Accords [A..] que la personne refuse."},
            "completed_steps": {
                "type": "array",
                "description": "Étapes que la personne dit avoir faites.",
                "items": {
                    "type": "object",
                    "properties": {"workflow_id": {"type": "integer"}, "step_key": NULLABLE_STRING},
                    "required": ["workflow_id"],
                },
            },
            "close_workflow_ids": {**INTS, "description": "Processus que la personne demande d'arrêter ou dit réglés."},
            "assign": {
                "type": "array",
                "description": "La personne demande qu'un autre membre s'occupe d'un processus.",
                "items": {
                    "type": "object",
                    "properties": {"workflow_id": {"type": "integer"}, "member_name": {"type": "string"}},
                    "required": ["workflow_id", "member_name"],
                },
            },
            "accept_ids": {**INTS, "description": "Propositions [R..] que la personne accepte."},
            "decline_ids": {**INTS, "description": "Propositions [R..] que la personne refuse."},
            "invite_name": {"type": ["string", "null"], "description": "Prénom de la personne à inviter dans le foyer."},
            "send_document_ids": {**INTS, "description": "Documents [D..] que la personne veut récupérer."},
            "action": {
                "type": ["string", "null"],
                "enum": ACTIONS + [None],
                "description": "connect_google si la personne veut connecter sa boîte mail ou son agenda, disconnect_google si elle veut retirer l'accès.",
            },
        },
        "required": ["reply"],
    },
}

INGEST_TOOL = {
    "name": "classer",
    "description": "Classe un document lu dans un service connecté et en tire ce qui compte.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean", "description": "Faux pour tout ce qui ne demande ni action ni mémoire."},
            "label": {"type": "string", "description": "Libellé court de la source, ex. « Mail EDF du 12/11 »."},
            "signals": {"type": "array", "items": SIGNAL_SCHEMA},
            "documents": {"type": "array", "items": DOCUMENT_SCHEMA},
            "items": {"type": "array", "items": ITEM_SCHEMA},
            "facts": {"type": "array", "items": {"type": "string"}},
            "notify": {"type": "string", "enum": NOTIFY_LEVELS},
            "message": {"type": "string", "description": "Le message WhatsApp, seulement si notify vaut « maintenant »."},
        },
        "required": ["relevant", "notify"],
    },
}


# --- Contexte ---------------------------------------------------------------


def _section(lines: list[str], title: str, items: list[str], empty: str | None = None) -> None:
    if not items and empty is None:
        return
    lines.append(f"\n{title}")
    lines.extend(items or [empty])


def render_context(context: Context) -> str:
    """Met en texte ce que MUMU sait deja, pour le modele."""
    jour = JOURS[context.today.weekday()]
    lines = [f"Date du jour : {jour} {context.today.isoformat()} (heure de Paris)."]
    lines.append(f"Prénom de la personne : {context.member_name or 'inconnu'}.")
    lines.append(f"Type de foyer : {context.household_kind or 'inconnu'}.")
    _section(lines, "Autres membres du foyer :", context.member_lines)
    _section(lines, "Enfants et proches du foyer :", context.people_lines)
    _section(lines, "Ce que tu sais du foyer :", [f"- {f}" for f in context.facts], "- rien pour l'instant")
    _section(lines, "Sa semaine, calculée à partir de données réelles (par priorité) :", context.agenda_lines)
    _section(lines, "Processus en cours :", context.workflow_lines, "- aucun")
    _section(lines, "Accords attendus de cette personne :", context.approval_lines)
    _section(lines, "Propositions faites à cette personne :", context.assignment_lines)

    open_items = []
    for item_id, title, due in context.open_items:
        echeance = f", pour le {due.isoformat()}" if due else ""
        open_items.append(f"- [{item_id}] {title}{echeance}")
    _section(lines, "Tâches simples en cours :", open_items, "- aucune")
    _section(lines, "Ses documents :", context.document_lines)
    _section(lines, "Argent (montants réels déjà vus) :", context.money_lines)
    _section(lines, "Agenda des 14 prochains jours (les lignes « ! » signalent un point serré) :", context.calendar_lines)
    _section(lines, "Achats récents :", context.purchase_lines)
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
    """Convertit une piece jointe en bloc lisible par le modele, si possible.

    Les PDF sont lus directement par le modele, y compris les scans.
    """
    if len(media.data) > MAX_MEDIA_BYTES:
        return None
    data = base64.standard_b64encode(media.data).decode()
    content_type = media.content_type.split(";")[0].strip()
    if content_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": data}}
    if content_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    return None


# --- Validation ----------------------------------------------------------


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ints(values) -> list[int]:
    return [v for v in values or [] if _int(v) is not None]


def _str(value, limit: int = 200) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:limit] if text else None


def _parse_items(raw_items) -> list[dict]:
    items = []
    for raw in raw_items or []:
        if not isinstance(raw, dict) or raw.get("kind") not in ITEM_KINDS or not raw.get("title"):
            continue
        confidence = _num(raw.get("confidence"))
        items.append(
            {
                "kind": raw["kind"],
                "title": str(raw["title"])[:200],
                "due_date": _parse_date(raw.get("due_date")),
                "amount_eur": _num(raw.get("amount_eur")),
                "owner": _str(raw.get("owner"), 80),
                "confidence": confidence if confidence is not None else 0.5,
            }
        )
    return items


def parse_signals(raw_signals) -> list[Signal]:
    signals = []
    for raw in raw_signals or []:
        if not isinstance(raw, dict) or raw.get("type") not in SIGNAL_TYPES:
            continue
        docs = []
        for doc in raw.get("required_documents") or []:
            if isinstance(doc, dict) and doc.get("label"):
                kind = doc.get("kind") if doc.get("kind") in DOCUMENT_KINDS else "autre"
                docs.append({"label": str(doc["label"])[:120], "kind": kind})
            elif isinstance(doc, str) and doc.strip():
                docs.append({"label": doc.strip()[:120], "kind": "autre"})
        actions = [str(a).strip()[:200] for a in raw.get("actions") or [] if str(a).strip()][:8]
        confidence = _num(raw.get("confidence"))
        requested = raw.get("requested_document")
        category = raw.get("category")
        life_event = raw.get("life_event")
        response_required = raw.get("response_required")
        signals.append(
            Signal(
                type=raw["type"],
                title=_str(raw.get("title")) or "",
                summary=_str(raw.get("summary"), 600) or "",
                organization=_str(raw.get("organization"), 120),
                reference=_str(raw.get("reference"), 120),
                amount_eur=_num(raw.get("amount_eur")),
                due_date=_parse_date(raw.get("due_date")),
                event_date=_parse_date(raw.get("event_date")),
                person=_str(raw.get("person"), 80),
                responsible=_str(raw.get("responsible"), 80),
                actions=actions,
                required_documents=docs[:10],
                requested_document=requested if requested in DOCUMENT_KINDS else None,
                response_required=response_required if isinstance(response_required, bool) else bool(docs or actions),
                category=category if category in CHARGE_CATEGORIES else None,
                product=_str(raw.get("product"), 300),
                return_window_days=_int(raw.get("return_window_days")),
                warranty_months=_int(raw.get("warranty_months")),
                life_event=life_event if life_event in LIFE_EVENTS else None,
                workflow_id=_int(raw.get("workflow_id")),
                confidence=confidence if confidence is not None else 0.5,
            )
        )
    return signals


def parse_documents(raw_documents) -> list[DocumentMeta]:
    documents = []
    for raw in raw_documents or []:
        if not isinstance(raw, dict) or _int(raw.get("index")) is None or not raw.get("title"):
            continue
        kind = raw.get("kind") if raw.get("kind") in DOCUMENT_KINDS else "autre"
        documents.append(
            DocumentMeta(
                index=raw["index"],
                kind=kind,
                title=str(raw["title"])[:200],
                issuer=_str(raw.get("issuer"), 120),
                issued_on=_parse_date(raw.get("issued_on")),
                valid_until=_parse_date(raw.get("valid_until")),
                summary=_str(raw.get("summary"), 2000) or "",
            )
        )
    return documents


def parse_understanding(payload: dict) -> Understanding:
    """Valide la forme de la sortie du modele. Les identifiants seront
    controles par le service."""
    action = payload.get("action")
    kind = payload.get("household_kind")
    steps = []
    for raw in payload.get("completed_steps") or []:
        if isinstance(raw, dict) and _int(raw.get("workflow_id")) is not None:
            steps.append((raw["workflow_id"], _str(raw.get("step_key"), 60)))
    assign = []
    for raw in payload.get("assign") or []:
        if isinstance(raw, dict) and _int(raw.get("workflow_id")) is not None and _str(raw.get("member_name")):
            assign.append((raw["workflow_id"], _str(raw["member_name"], 80)))
    people = [
        {"name": _str(p["name"], 80), "relation": _str(p.get("relation"), 20) or "enfant"}
        for p in payload.get("people") or []
        if isinstance(p, dict) and _str(p.get("name"))
    ]
    return Understanding(
        reply=str(payload.get("reply") or "").strip(),
        items=_parse_items(payload.get("items")),
        facts=[str(f).strip() for f in payload.get("facts") or [] if str(f).strip()],
        done_item_ids=_ints(payload.get("done_item_ids")),
        member_name=_str(payload.get("member_name"), 80),
        action=action if action in ACTIONS else None,
        signals=parse_signals(payload.get("signals")),
        documents=parse_documents(payload.get("documents")),
        household_kind=kind if kind in HOUSEHOLD_KINDS else None,
        people=people,
        approve_ids=_ints(payload.get("approve_ids")),
        reject_ids=_ints(payload.get("reject_ids")),
        completed_steps=steps,
        close_workflow_ids=_ints(payload.get("close_workflow_ids")),
        assign=assign,
        accept_ids=_ints(payload.get("accept_ids")),
        decline_ids=_ints(payload.get("decline_ids")),
        invite_name=_str(payload.get("invite_name"), 80),
        send_document_ids=_ints(payload.get("send_document_ids")),
    )


def parse_ingestion(payload: dict) -> Ingestion:
    notify = payload.get("notify")
    message = str(payload.get("message") or "").strip()
    if notify not in NOTIFY_LEVELS or (notify == "maintenant" and not message):
        notify = "recap"
    return Ingestion(
        relevant=bool(payload.get("relevant")),
        label=str(payload.get("label") or "")[:200],
        items=_parse_items(payload.get("items")),
        signals=parse_signals(payload.get("signals")),
        documents=parse_documents(payload.get("documents")),
        facts=[str(f).strip() for f in payload.get("facts") or [] if str(f).strip()],
        notify=notify,
        message=message,
    )


# --- Appel au modele -------------------------------------------------------


class ClaudeBrain:
    def __init__(self, api_key: str, model: str, ingest_model: str | None = None):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._ingest_model = ingest_model or model

    def _call(self, model: str, system: str, tool: dict, content: list[dict], max_tokens: int) -> dict:
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": content}],
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("Le modèle n'a pas renvoyé de réponse exploitable.")

    def understand(self, context: Context, text: str, media: Media | None) -> Understanding:
        content: list[dict] = []
        note = ""
        if media is not None:
            block = media_block(media)
            if block is not None:
                content.append(block)
                note = "\n[Pièce jointe n° 0 ci-dessus.]"
            elif media.content_type.startswith("audio/"):
                note = "\n[La personne a envoyé un vocal que tu n'as pas pu écouter : dis-le simplement et demande un message écrit.]"
            else:
                note = f"\n[Pièce jointe non lisible ({media.content_type}). Dis-le simplement.]"
        content.append(
            {"type": "text", "text": f"{render_context(context)}\n\nNouveau message :\n{text or '(sans texte)'}{note}"}
        )
        understanding = parse_understanding(self._call(self._model, SYSTEM_PROMPT, REPLY_TOOL, content, 2500))
        if not understanding.reply:
            raise RuntimeError("Réponse vide.")
        return understanding

    def ingest(
        self, context: Context, document: SourceDocument, attachments: list[Media] | None = None
    ) -> Ingestion:
        content: list[dict] = []
        listed = []
        for index, attachment in enumerate(attachments or []):
            block = media_block(attachment)
            if block is not None:
                content.append(block)
                listed.append(f"n° {index} : {attachment.filename or attachment.content_type}")
        attachments_note = f"\nPièces jointes ci-dessus : {', '.join(listed)}." if listed else ""
        content.append(
            {
                "type": "text",
                "text": (
                    f"{render_context(context)}\n\n"
                    f"Document à classer ({document.source_kind}) :\n"
                    f"De : {document.sender}\n"
                    f"Objet : {document.subject}\n"
                    f"Reçu le : {document.received_at.date().isoformat()}{attachments_note}\n\n"
                    f"{document.text}"
                ),
            }
        )
        return parse_ingestion(self._call(self._ingest_model, INGEST_PROMPT, INGEST_TOOL, content, 2000))
