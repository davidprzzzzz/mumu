"""Les types de processus que MUMU sait suivre.

Un modele decrit les etapes d'un processus, qui fait chacune d'elles, ce qui
demande un accord, comment MUMU peut verifier seul qu'une etape est deja
faite, et le brouillon qu'il prepare quand il faut ecrire a quelqu'un.

Ajouter un processus = ajouter un modele ici. Le moteur (engine.py) est commun.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Charge, Document, Workflow
from ..text import eur, fr_date, norm


@dataclass
class StepSpec:
    key: str
    title: str
    # "responsible" : une personne du foyer. "mumu" : MUMU s'en charge.
    # "evidence" : se valide quand la preuve arrive.
    owner: str = "responsible"
    approval: bool = False
    # Une etape optionnelle commence « skipped » et n'est activee que si besoin
    # (ex. relancer un marchand quand le remboursement est en retard).
    optional: bool = False


# Une verification renvoie une preuve (texte court) si l'etape est deja faite,
# ou None. Elle peut aussi poser un blocage sur le processus.
AutoCheck = Callable[[Session, Workflow, date], str | None]


@dataclass
class Template:
    key: str
    label: str
    build_steps: Callable[[Workflow], list[StepSpec]]
    drafts: dict[str, Callable[[Workflow, str], str]] = field(default_factory=dict)
    auto_checks: dict[str, AutoCheck] = field(default_factory=dict)


DOCUMENT_KINDS = {
    "attestation_assurance": "attestation d'assurance habitation",
    "facture": "facture",
    "contrat": "contrat",
    "avis_imposition": "avis d'imposition",
    "courrier_administratif": "courrier administratif",
    "justificatif_domicile": "justificatif de domicile",
    "bulletin_salaire": "bulletin de salaire",
    "certificat_scolarite": "certificat de scolarité",
    "etat_des_lieux": "état des lieux",
    "releve_bancaire": "relevé bancaire",
    "garantie": "garantie ou facture d'achat",
    "piece_identite": "pièce d'identité",
    "autre": "document",
}


def _org(wf: Workflow) -> str:
    return wf.organization or "l'organisme"


def _signature(name: str) -> str:
    return f"\n\nCordialement,\n{name}" if name else "\n\nCordialement,"


def find_document(session: Session, wf: Workflow, kind: str, today: date) -> tuple[Document | None, Document | None]:
    """Cherche le document le plus recent de ce type pour le proprietaire.

    Renvoie (document valide, document perime). L'un des deux au plus est rempli.
    """
    docs = session.scalars(
        select(Document)
        .where(Document.member_id == wf.owner_member_id, Document.kind == kind)
        .order_by(Document.valid_until.is_(None), Document.valid_until.desc(), Document.id.desc())
    ).all()
    for doc in docs:
        if doc.valid_until is None or doc.valid_until >= today:
            return doc, None
    return None, (docs[0] if docs else None)


# --- Facture a payer ----------------------------------------------------------


def _payment_steps(wf: Workflow) -> list[StepSpec]:
    what = f"Payer {eur(wf.amount_eur)} à {_org(wf)}" if wf.amount_eur else f"Payer {_org(wf)}"
    return [StepSpec("pay", what.strip())]


PAYMENT = Template("payment", "Facture à payer", _payment_steps)


# --- Courrier administratif ----------------------------------------------------


def _doc_step_key(label: str) -> str:
    return "doc_" + (norm(label)[:40] or "piece")


def _admin_steps(wf: Workflow) -> list[StepSpec]:
    steps = [StepSpec(_doc_step_key(doc), f"Réunir : {doc}") for doc in wf.data.get("required_documents", [])]
    for index, action in enumerate(wf.data.get("actions", [])):
        steps.append(StepSpec(f"action_{index + 1}", action))
    if wf.data.get("response_required", True):
        steps += [
            StepSpec("prepare", f"Préparer la réponse à {_org(wf)}", owner="mumu", approval=True),
            StepSpec("submit", f"Envoyer la réponse à {_org(wf)}"),
            StepSpec("answer", f"Réponse de {_org(wf)}", owner="evidence"),
        ]
    return steps


def _admin_draft(wf: Workflow, member_name: str) -> str:
    docs = wf.data.get("required_documents", [])
    ref = f" (référence {wf.reference})" if wf.reference else ""
    lines = [f"Madame, Monsieur,\n\nSuite à votre courrier{ref}, je vous adresse les éléments demandés."]
    if docs:
        lines.append("Vous trouverez ci-joint :\n" + "\n".join(f"- {d}" for d in docs))
    lines.append("Je reste à votre disposition pour toute information complémentaire.")
    return "\n\n".join(lines) + _signature(member_name)


def _admin_auto_checks(wf: Workflow) -> dict[str, AutoCheck]:
    checks: dict[str, AutoCheck] = {}
    kinds = wf.data.get("required_document_kinds", {})
    for label, kind in kinds.items():
        if kind not in DOCUMENT_KINDS or kind == "autre":
            continue

        def check(session: Session, workflow: Workflow, today: date, kind=kind) -> str | None:
            doc, _ = find_document(session, workflow, kind, today)
            return f"document:{doc.id}" if doc else None

        checks[_doc_step_key(label)] = check
    return checks


ADMIN = Template("admin", "Démarche administrative", _admin_steps, drafts={"prepare": _admin_draft})


# --- Document demande par un organisme -----------------------------------------


def _requested_label(wf: Workflow) -> str:
    return DOCUMENT_KINDS.get(wf.data.get("requested_document", "autre"), "document")


def _docreq_steps(wf: Workflow) -> list[StepSpec]:
    return [
        StepSpec("find", f"Retrouver ton {_requested_label(wf)}", owner="mumu"),
        StepSpec("send", f"Envoyer le document à {_org(wf)}", approval=True),
    ]


def _docreq_find(session: Session, wf: Workflow, today: date) -> str | None:
    kind = wf.data.get("requested_document", "autre")
    doc, expired = find_document(session, wf, kind, today)
    if doc:
        wf.blocker = None
        wf.data = {**wf.data, "document_id": doc.id}
        return f"document:{doc.id}"
    if expired:
        wf.blocker = (
            f"Ton {_requested_label(wf)} a expiré le {fr_date(expired.valid_until)} : "
            f"il en faut un nouveau{' auprès de ' + expired.issuer if expired.issuer else ''}."
        )
    else:
        wf.blocker = f"Je n'ai pas encore ton {_requested_label(wf)}. Envoie-le-moi en photo ou en PDF."
    return None


def _docreq_draft(wf: Workflow, member_name: str) -> str:
    ref = f" (dossier {wf.reference})" if wf.reference else ""
    return (
        f"Bonjour,\n\nVous trouverez ci-joint mon {_requested_label(wf)}{ref}, comme demandé."
        + _signature(member_name)
    )


DOCUMENT_REQUEST = Template(
    "document_request",
    "Document à fournir",
    _docreq_steps,
    drafts={"send": _docreq_draft},
    auto_checks={"find": _docreq_find},
)


# --- Evenement scolaire ----------------------------------------------------------


def _school_steps(wf: Workflow) -> list[StepSpec]:
    actions = list(wf.data.get("actions", []))
    if wf.amount_eur and not any("pay" in norm(a) for a in actions):
        actions.append(f"Payer {eur(wf.amount_eur)}")
    return [StepSpec(f"action_{i + 1}", action) for i, action in enumerate(actions)] or [
        StepSpec("action_1", "Préparer l'événement")
    ]


SCHOOL_EVENT = Template("school_event", "Événement scolaire", _school_steps)


# --- Retour et remboursement --------------------------------------------------


def _refund_steps(wf: Workflow) -> list[StepSpec]:
    amount = f" de {eur(wf.amount_eur)}" if wf.amount_eur else ""
    return [
        StepSpec("return", "Colis renvoyé", owner="evidence"),
        StepSpec("refund", f"Remboursement{amount} reçu", owner="evidence"),
        StepSpec("followup", f"Relancer {_org(wf)}", owner="mumu", approval=True, optional=True),
    ]


def _refund_draft(wf: Workflow, member_name: str) -> str:
    ref = f"n° {wf.reference} " if wf.reference else ""
    returned = wf.data.get("returned_on")
    sent = f" le {fr_date(date.fromisoformat(returned))}" if returned else ""
    amount = f" de {eur(wf.amount_eur)}" if wf.amount_eur else ""
    return (
        f"Bonjour,\n\nJ'ai renvoyé ma commande {ref}{sent}. Je n'ai pas encore reçu le "
        f"remboursement{amount}. Pouvez-vous me confirmer la date à laquelle il sera effectué ?"
        + _signature(member_name)
    )


REFUND = Template("refund", "Retour et remboursement", _refund_steps, drafts={"followup": _refund_draft})


# --- Renouvellement de contrat ---------------------------------------------------


def _renewal_steps(wf: Workflow) -> list[StepSpec]:
    before = f" avant le {fr_date(wf.due_date)}" if wf.due_date else ""
    return [
        StepSpec("review", "Vérifier la couverture et le prix"),
        StepSpec("decide", f"Garder ou changer{before}"),
    ]


RENEWAL = Template("renewal", "Renouvellement de contrat", _renewal_steps)


# --- Playbook : premier logement --------------------------------------------------


def _move_in_steps(wf: Workflow) -> list[StepSpec]:
    return [
        StepSpec("insurance", "Attestation d'assurance habitation"),
        StepSpec("energy", "Contrat d'électricité ouvert"),
        StepSpec("caf", "Demande d'aide au logement à la CAF"),
        StepSpec("inventory", "État des lieux d'entrée gardé"),
        StepSpec("address", "Nouvelle adresse donnée (banque, employeur ou école, impôts)"),
    ]


def _has_document(kind: str) -> AutoCheck:
    def check(session: Session, wf: Workflow, today: date) -> str | None:
        doc, _ = find_document(session, wf, kind, today)
        return f"document:{doc.id}" if doc else None

    return check


def _has_energy_contract(session: Session, wf: Workflow, today: date) -> str | None:
    charge = session.scalar(
        select(Charge).where(Charge.member_id == wf.owner_member_id, Charge.category == "energie").limit(1)
    )
    return f"facture {charge.merchant}" if charge else None


def _has_caf_process(session: Session, wf: Workflow, today: date) -> str | None:
    others = session.scalars(
        select(Workflow).where(
            Workflow.owner_member_id == wf.owner_member_id,
            Workflow.id != wf.id,
            Workflow.template.in_(["admin", "document_request"]),
        )
    ).all()
    for other in others:
        if "caf" in norm(other.organization):
            return f"processus {other.id}"
    return None


MOVE_IN = Template(
    "move_in",
    "Premier logement",
    _move_in_steps,
    auto_checks={
        "insurance": _has_document("attestation_assurance"),
        "energy": _has_energy_contract,
        "caf": _has_caf_process,
        "inventory": _has_document("etat_des_lieux"),
    },
)


TEMPLATES: dict[str, Template] = {
    t.key: t for t in (PAYMENT, ADMIN, DOCUMENT_REQUEST, SCHOOL_EVENT, REFUND, RENEWAL, MOVE_IN)
}


def auto_checks_for(wf: Workflow) -> dict[str, AutoCheck]:
    template = TEMPLATES[wf.template]
    if wf.template == "admin":
        return _admin_auto_checks(wf)
    return template.auto_checks


def default_refund_date(returned_on: date) -> date:
    """Delai legal : le marchand rembourse au plus tard 14 jours apres avoir
    ete informe de la retractation. Utilise seulement faute de date donnee."""
    return returned_on + timedelta(days=14)
