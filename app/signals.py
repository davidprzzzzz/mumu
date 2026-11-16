"""Du sens a l'action : ce que MUMU a compris devient un processus suivi.

Le modele lit un mail, une photo ou un message et en tire des « signaux »
(une facture, une lettre de la CAF, une sortie scolaire, un retour de colis).
Ce module, deterministe, decide quoi en faire : ouvrir un processus, en
completer un existant, valider une etape grace a une preuve, enregistrer un
achat ou un montant.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import log_action
from .models import Fact, Member, Person, Purchase, Workflow
from .money import record_charge
from .text import norm
from .workflows import engine
from .workflows.engine import SourceRef
from .workflows.templates import DOCUMENT_KINDS, default_refund_date

SIGNAL_TYPES = [
    "invoice",
    "payment_confirmation",
    "admin_letter",
    "document_request",
    "school_event",
    "order",
    "delivery",
    "return_started",
    "refund_received",
    "subscription_charge",
    "renewal",
    "life_event",
]
LIFE_EVENTS = ["emmenagement"]

# Delais legaux par defaut, utilises seulement si le marchand n'en donne pas :
# 14 jours de retractation pour un achat a distance, 2 ans de garantie legale
# de conformite pour un bien neuf.
WITHDRAWAL_DAYS = 14
LEGAL_WARRANTY_DAYS = 730


@dataclass
class Signal:
    type: str
    title: str
    summary: str = ""
    organization: str | None = None
    reference: str | None = None
    amount_eur: float | None = None
    due_date: date | None = None
    event_date: date | None = None
    person: str | None = None
    responsible: str | None = None
    actions: list[str] = field(default_factory=list)
    required_documents: list[dict] = field(default_factory=list)
    requested_document: str | None = None
    response_required: bool = True
    category: str | None = None
    product: str | None = None
    return_window_days: int | None = None
    warranty_months: int | None = None
    life_event: str | None = None
    workflow_id: int | None = None
    confidence: float = 0.8


@dataclass
class RouteResult:
    created: list[Workflow] = field(default_factory=list)
    updated: list[Workflow] = field(default_factory=list)
    resolved: list[Workflow] = field(default_factory=list)
    purchases: list[Purchase] = field(default_factory=list)
    # La personne a dit sur WhatsApp qu'un autre membre s'en charge :
    # la proposition lui est envoyee. (processus, membre)
    to_assign: list[tuple[Workflow, Member]] = field(default_factory=list)
    # Un mail prive designe un autre membre : MUMU le suggere au proprietaire
    # sans rien partager. (processus, membre)
    suggested: list[tuple[Workflow, Member]] = field(default_factory=list)


def _member_by_name(session: Session, household_id: int, name: str | None) -> Member | None:
    if not name:
        return None
    key = norm(name)
    for member in session.scalars(select(Member).where(Member.household_id == household_id)):
        if member.name and norm(member.name) == key:
            return member
    return None


def person_for(session: Session, household_id: int, name: str | None, create: bool = True) -> Person | None:
    if not name or not norm(name):
        return None
    key = norm(name)
    for person in session.scalars(select(Person).where(Person.household_id == household_id)):
        if norm(person.name) == key:
            return person
    if not create:
        return None
    person = Person(household_id=household_id, name=name.strip()[:80])
    session.add(person)
    session.flush()
    return person


def _open_workflows(session: Session, member: Member, template: str) -> list[Workflow]:
    return list(
        session.scalars(
            select(Workflow).where(
                Workflow.household_id == member.household_id,
                Workflow.template == template,
                Workflow.status.in_(engine.OPEN_STATUSES),
            ).order_by(Workflow.id.desc())
        )
    )


def _hinted(session: Session, member: Member, signal: Signal, template: str) -> Workflow | None:
    """Processus designe par le modele, accepte seulement s'il est coherent."""
    if not signal.workflow_id:
        return None
    wf = session.get(Workflow, signal.workflow_id)
    if wf is None or wf.household_id != member.household_id or wf.template != template:
        return None
    if wf.visibility == "private" and wf.owner_member_id != member.id:
        return None
    return wf if engine.is_open(wf) else None


def _match(session: Session, member: Member, signal: Signal, template: str) -> Workflow | None:
    """Retrouve le processus ouvert auquel se rattache une preuve."""
    hinted = _hinted(session, member, signal, template)
    if hinted:
        return hinted
    org, ref = norm(signal.organization), norm(signal.reference)
    candidates = _open_workflows(session, member, template)
    if ref:
        for wf in candidates:
            if norm(wf.reference) == ref:
                return wf
    if org:
        matching = [wf for wf in candidates if org in norm(wf.organization) or norm(wf.organization) in org]
        matching = [wf for wf in matching if norm(wf.organization)]
        if len(matching) == 1:
            return matching[0]
    return None


def _find_purchase(session: Session, member: Member, signal: Signal) -> Purchase | None:
    ref = norm(signal.reference)
    purchases = session.scalars(
        select(Purchase).where(Purchase.member_id == member.id).order_by(Purchase.id.desc())
    ).all()
    if ref:
        for purchase in purchases:
            if norm(purchase.order_ref) == ref:
                return purchase
    org = norm(signal.organization)
    if org:
        same_merchant = [p for p in purchases if norm(p.merchant) == org]
        if len(same_merchant) == 1:
            return same_merchant[0]
    return None


def _apply_purchase_dates(purchase: Purchase, signal: Signal) -> None:
    base = purchase.delivered_on or purchase.delivery_expected or purchase.ordered_on
    estimated = False
    if signal.return_window_days and base:
        purchase.return_deadline = base + timedelta(days=signal.return_window_days)
    elif base and purchase.return_deadline is None:
        purchase.return_deadline = base + timedelta(days=WITHDRAWAL_DAYS)
        estimated = True
    start = purchase.delivered_on or purchase.ordered_on
    if signal.warranty_months and start:
        purchase.warranty_until = start + timedelta(days=round(signal.warranty_months * 30.44))
    elif start and purchase.warranty_until is None:
        purchase.warranty_until = start + timedelta(days=LEGAL_WARRANTY_DAYS)
        estimated = True
    purchase.dates_are_estimates = purchase.dates_are_estimates or estimated


def route_signals(
    session: Session,
    member: Member,
    signals: list[Signal],
    source: SourceRef,
    today: date,
    received_on: date | None = None,
) -> RouteResult:
    """Transforme les signaux en processus, achats et montants."""
    result = RouteResult()
    # Ce qui vient d'une boite mail reste prive ; ce qu'une personne dit a
    # MUMU sur WhatsApp aussi, tant qu'elle n'implique pas quelqu'un d'autre.
    received_on = received_on or today

    def opened(wf: Workflow, created: bool) -> None:
        (result.created if created else result.updated).append(wf)

    for signal in signals:
        org = signal.organization or ""
        person = person_for(session, member.household_id, signal.person) if signal.person else None
        responsible = _member_by_name(session, member.household_id, signal.responsible)
        if responsible and responsible.id == member.id:
            responsible = None

        if signal.type in ("invoice", "subscription_charge") and signal.amount_eur:
            record_charge(
                session, member,
                merchant=org or signal.title,
                amount=signal.amount_eur,
                charged_on=signal.event_date or received_on,
                category=signal.category or ("abonnement" if signal.type == "subscription_charge" else "autre"),
                source_kind=source.kind,
                source_ref=source.ref,
            )

        if signal.type == "invoice":
            if not (signal.due_date or signal.amount_eur):
                continue
            wf, created = engine.create(
                session, owner=member, template="payment",
                title=signal.title or f"Facture {org}",
                dedupe_key=f"payment:{norm(org)}:{norm(signal.reference) or signal.due_date or signal.amount_eur}",
                today=today, source=source,
                summary=signal.summary, organization=org or None, reference=signal.reference,
                amount_eur=signal.amount_eur, due_date=signal.due_date, confidence=signal.confidence,
            )
            opened(wf, created)

        elif signal.type == "payment_confirmation":
            wf = _match(session, member, signal, "payment")
            if wf and engine.resolve_by_evidence(session, wf, "pay", f"{source.kind}:{source.ref}", today):
                engine.add_source(session, wf, source)
                result.resolved.append(wf)

        elif signal.type == "admin_letter":
            docs = [d for d in signal.required_documents if d.get("label")]
            if not (signal.actions or docs or signal.due_date or signal.response_required):
                # Courrier pour information : on le retient, rien a suivre.
                session.add(
                    Fact(
                        household_id=member.household_id, member_id=member.id,
                        text=f"{org} : {signal.summary or signal.title}"[:1000],
                        source_kind=source.kind, source_ref=source.ref, source_label=source.label,
                        visibility="private",
                    )
                )
                continue
            wf, created = engine.create(
                session, owner=member, template="admin",
                title=signal.title or f"Courrier {org}",
                dedupe_key=f"admin:{norm(org)}:{norm(signal.reference) or norm(signal.title)[:40]}",
                today=today, source=source,
                summary=signal.summary, organization=org or None, reference=signal.reference,
                amount_eur=signal.amount_eur, due_date=signal.due_date, confidence=signal.confidence,
                data={
                    "required_documents": [d["label"] for d in docs],
                    "required_document_kinds": {d["label"]: d.get("kind") or "autre" for d in docs},
                    "actions": signal.actions,
                    "response_required": signal.response_required,
                },
            )
            opened(wf, created)

        elif signal.type == "document_request":
            kind = signal.requested_document if signal.requested_document in DOCUMENT_KINDS else "autre"
            wf, created = engine.create(
                session, owner=member, template="document_request",
                title=signal.title or f"{DOCUMENT_KINDS[kind].capitalize()} demandé par {org}",
                dedupe_key=f"docreq:{norm(org)}:{kind}:{norm(signal.reference)}",
                today=today, source=source,
                summary=signal.summary, organization=org or None, reference=signal.reference,
                due_date=signal.due_date, confidence=signal.confidence,
                data={"requested_document": kind},
            )
            opened(wf, created)

        elif signal.type == "school_event":
            when = signal.event_date or signal.due_date
            wf, created = engine.create(
                session, owner=member, template="school_event",
                title=signal.title or "Événement scolaire",
                dedupe_key=f"school:{norm(signal.person)}:{when}:{norm(signal.title)[:30]}",
                today=today, source=source,
                summary=signal.summary, organization=org or None, amount_eur=signal.amount_eur,
                due_date=signal.due_date, event_date=signal.event_date,
                person_id=person.id if person else None, confidence=signal.confidence,
                data={"actions": signal.actions},
            )
            opened(wf, created)
            if created and responsible:
                if source.kind == "whatsapp":
                    # La personne a dit elle-meme qui s'en charge.
                    result.to_assign.append((wf, responsible))
                else:
                    # Depuis un mail prive, on suggere sans rien partager.
                    wf.data = {**wf.data, "suggested_responsible_id": responsible.id}
                    result.suggested.append((wf, responsible))

        elif signal.type == "order":
            purchase = _find_purchase(session, member, signal) if signal.reference else None
            if purchase is None:
                purchase = Purchase(
                    household_id=member.household_id, member_id=member.id,
                    merchant=(org or "Marchand")[:120],
                    description=(signal.product or signal.title)[:300],
                    amount_eur=signal.amount_eur, ordered_on=signal.event_date or received_on,
                    delivery_expected=signal.due_date, order_ref=signal.reference,
                    source_kind=source.kind, source_ref=source.ref,
                )
                session.add(purchase)
            _apply_purchase_dates(purchase, signal)
            session.flush()
            result.purchases.append(purchase)

        elif signal.type == "delivery":
            purchase = _find_purchase(session, member, signal)
            if purchase:
                purchase.status = "delivered"
                purchase.delivered_on = signal.event_date or received_on
                if purchase.dates_are_estimates:
                    purchase.return_deadline = None
                    purchase.warranty_until = None
                    purchase.dates_are_estimates = False
                _apply_purchase_dates(purchase, signal)

        elif signal.type == "return_started":
            purchase = _find_purchase(session, member, signal)
            returned_on = signal.event_date or received_on
            expected = signal.due_date or default_refund_date(returned_on)
            if purchase:
                purchase.status = "returned"
            reference = signal.reference or (purchase.order_ref if purchase else None)
            wf, created = engine.create(
                session, owner=member, template="refund",
                title=signal.title or f"Remboursement {org}",
                dedupe_key=f"refund:{norm(org)}:{norm(reference) or norm(signal.title)[:30]}",
                today=today, source=source,
                summary=signal.summary, organization=org or (purchase.merchant if purchase else None),
                reference=reference,
                amount_eur=signal.amount_eur or (purchase.amount_eur if purchase else None),
                due_date=expected, confidence=signal.confidence,
                data={
                    "purchase_id": purchase.id if purchase else None,
                    "returned_on": returned_on.isoformat(),
                    "refund_date_is_estimate": signal.due_date is None,
                },
            )
            if created:
                engine.complete_step(session, wf, "return", by="evidence", today=today,
                                     evidence=f"{source.kind}:{source.ref}")
            opened(wf, created)

        elif signal.type == "refund_received":
            wf = _match(session, member, signal, "refund")
            if wf and engine.resolve_by_evidence(session, wf, "refund", f"{source.kind}:{source.ref}", today):
                engine.add_source(session, wf, source)
                result.resolved.append(wf)
                purchase_id = (wf.data or {}).get("purchase_id")
                purchase = session.get(Purchase, purchase_id) if purchase_id else None
                if purchase:
                    purchase.status = "refunded"

        elif signal.type == "renewal":
            wf, created = engine.create(
                session, owner=member, template="renewal",
                title=signal.title or f"Renouvellement {org}",
                dedupe_key=f"renewal:{norm(org)}:{signal.due_date}",
                today=today, source=source,
                summary=signal.summary, organization=org or None, reference=signal.reference,
                amount_eur=signal.amount_eur, due_date=signal.due_date, confidence=signal.confidence,
            )
            opened(wf, created)

        elif signal.type == "life_event" and signal.life_event == "emmenagement":
            wf, created = engine.create(
                session, owner=member, template="move_in",
                title=signal.title or "Installation dans le nouveau logement",
                dedupe_key=f"playbook:move_in:{member.id}",
                today=today, source=source, summary=signal.summary,
                due_date=signal.due_date, event_date=signal.event_date,
            )
            opened(wf, created)

    if result.created or result.resolved:
        log_action(
            session, "signals_routed", household_id=member.household_id, member_id=member.id,
            source=source.kind, created=[w.id for w in result.created],
            resolved=[w.id for w in result.resolved],
        )
    return result
