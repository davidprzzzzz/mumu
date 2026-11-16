"""Moteur de processus : creer, faire avancer, faire approuver, clore.

Regles :
- un processus n'est « resolu » que lorsque toutes ses etapes sont faites,
  ou « ferme » quand une personne le decide explicitement ;
- une etape qui demande un accord ne passe jamais seule : MUMU prepare un
  brouillon et attend ;
- MUMU n'envoie rien lui-meme a un organisme. Une etape « envoyer » approuvee
  reste a faire par la personne, qui recoit le texte pret, et n'est validee
  que lorsqu'elle confirme ou qu'une preuve arrive.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import log_action
from ..models import Approval, Member, Workflow, WorkflowSource, WorkflowStep, now_utc
from .templates import TEMPLATES, auto_checks_for

OPEN_STATUSES = ("active", "waiting_approval", "waiting_other", "blocked")
DONE_STEP = ("done", "skipped")


@dataclass
class SourceRef:
    kind: str  # "whatsapp", "gmail"
    ref: str
    label: str | None = None


def is_open(wf: Workflow) -> bool:
    return wf.status in OPEN_STATUSES


def open_steps(wf: Workflow) -> list[WorkflowStep]:
    return [s for s in wf.steps if s.status not in DONE_STEP]


def current_step(wf: Workflow) -> WorkflowStep | None:
    steps = open_steps(wf)
    return steps[0] if steps else None


def add_source(session: Session, wf: Workflow, source: SourceRef | None) -> None:
    if source is None or not source.ref:
        return
    if any(s.source_kind == source.kind and s.source_ref == source.ref for s in wf.sources):
        return
    wf.sources.append(
        WorkflowSource(source_kind=source.kind, source_ref=source.ref, label=source.label)
    )


def create(
    session: Session,
    *,
    owner: Member,
    template: str,
    title: str,
    dedupe_key: str,
    today: date,
    source: SourceRef | None = None,
    visibility: str = "private",
    **fields,
) -> tuple[Workflow, bool]:
    """Cree le processus, ou renvoie celui qui existe deja pour la meme cle.

    Renvoie (processus, cree).
    """
    existing = session.scalar(
        select(Workflow).where(
            Workflow.household_id == owner.household_id, Workflow.dedupe_key == dedupe_key
        )
    )
    if existing is not None:
        changed = _merge(existing, fields)
        add_source(session, existing, source)
        if changed:
            existing.updated_at = now_utc()
            log_action(
                session, "workflow_updated", household_id=owner.household_id,
                member_id=owner.id, target=f"workflow:{existing.id}", fields=sorted(changed),
            )
        refresh(session, existing, today)
        return existing, False

    if template not in TEMPLATES:
        raise ValueError(f"Modèle inconnu : {template}")
    data = fields.pop("data", None) or {}
    wf = Workflow(
        household_id=owner.household_id,
        owner_member_id=owner.id,
        visibility=visibility,
        template=template,
        title=title[:200],
        dedupe_key=dedupe_key[:200],
        data=data,
        **fields,
    )
    session.add(wf)
    for position, spec in enumerate(TEMPLATES[template].build_steps(wf)):
        wf.steps.append(
            WorkflowStep(
                position=position,
                key=spec.key,
                title=spec.title[:200],
                owner=spec.owner,
                requires_approval=spec.approval,
                optional=spec.optional,
                status="skipped" if spec.optional else "todo",
            )
        )
    add_source(session, wf, source)
    session.flush()
    log_action(
        session, "workflow_created", household_id=owner.household_id, member_id=owner.id,
        target=f"workflow:{wf.id}", template=template, source=source.kind if source else None,
    )
    refresh(session, wf, today)
    return wf, True


def _merge(wf: Workflow, fields: dict) -> set[str]:
    """Complete un processus existant avec ce qu'une nouvelle source apporte."""
    changed = set()
    for name in ("summary", "organization", "reference", "amount_eur", "due_date", "event_date", "person_id"):
        value = fields.get(name)
        if value and getattr(wf, name) != value:
            # Une nouvelle echeance remplace l'ancienne ; le reste se complete.
            if getattr(wf, name) in (None, "") or name == "due_date":
                setattr(wf, name, value)
                changed.add(name)
    data = fields.get("data") or {}
    merged = dict(wf.data or {})
    for key, value in data.items():
        if value and not merged.get(key):
            merged[key] = value
            changed.add(f"data.{key}")
    wf.data = merged
    return changed


def refresh(session: Session, wf: Workflow, today: date) -> None:
    """Recalcule l'etat : verifications automatiques, accords, etape suivante."""
    if not is_open(wf):
        return
    for key, check in auto_checks_for(wf).items():
        step = _step(wf, key)
        if step is not None and step.status == "todo":
            evidence = check(session, wf, today)
            if evidence:
                _mark_done(step, by="mumu", evidence=evidence)

    remaining = open_steps(wf)
    if not remaining:
        resolve(session, wf, "Toutes les étapes sont faites.")
        return

    step = remaining[0]
    if step.requires_approval and step.status == "todo":
        _request_approval(session, wf, step)

    if step.status == "awaiting_approval":
        wf.status = "waiting_approval"
    elif wf.blocker and step.owner != "responsible":
        wf.status = "blocked"
    elif step.owner == "evidence":
        wf.status = "waiting_other"
    elif wf.responsible_member_id and wf.responsible_member_id != wf.owner_member_id:
        wf.status = "waiting_other"
    else:
        wf.status = "active"
    wf.next_action = step.title
    wf.updated_at = now_utc()


def _step(wf: Workflow, key: str) -> WorkflowStep | None:
    return next((s for s in wf.steps if s.key == key), None)


def _mark_done(step: WorkflowStep, *, by: str, member_id: int | None = None, evidence: str | None = None) -> None:
    step.status = "done"
    step.done_at = now_utc()
    step.done_by = by
    step.done_by_member_id = member_id
    step.evidence = evidence[:300] if evidence else None


def _request_approval(session: Session, wf: Workflow, step: WorkflowStep) -> Approval:
    approver_id = wf.responsible_member_id or wf.owner_member_id
    approver = session.get(Member, approver_id)
    draft_fn = TEMPLATES[wf.template].drafts.get(step.key)
    draft = draft_fn(wf, (approver.name if approver else "") or "") if draft_fn else step.title
    approval = Approval(workflow_id=wf.id, step_key=step.key, member_id=approver_id, draft=draft)
    session.add(approval)
    step.status = "awaiting_approval"
    session.flush()
    log_action(
        session, "approval_requested", household_id=wf.household_id, member_id=approver_id,
        target=f"approval:{approval.id}", workflow=wf.id, step=step.key,
    )
    return approval


def activate(session: Session, wf: Workflow, key: str, today: date) -> bool:
    """Reveille une etape optionnelle (ex. relancer un marchand en retard)."""
    step = _step(wf, key)
    if step is None or step.status != "skipped" or not is_open(wf):
        return False
    step.status = "todo"
    # L'etape activee passe devant les etapes en attente d'une preuve.
    blocking = [s for s in open_steps(wf) if s is not step and s.owner == "evidence"]
    if blocking:
        step.position = min(s.position for s in blocking) - 1
        wf.steps.sort(key=lambda s: s.position)
    refresh(session, wf, today)
    return True


def complete_step(
    session: Session,
    wf: Workflow,
    key: str | None,
    *,
    by: str,
    today: date,
    member_id: int | None = None,
    evidence: str | None = None,
) -> WorkflowStep | None:
    """Valide une etape. Sans cle, valide la premiere etape ouverte qui
    revient a une personne du foyer. Une etape qui attend un accord ne peut
    pas etre validee ainsi."""
    if not is_open(wf):
        return None
    if key:
        step = _step(wf, key)
    else:
        step = next(
            (s for s in open_steps(wf) if s.owner in ("responsible", "evidence") and s.status != "awaiting_approval"),
            None,
        )
    if step is None or step.status in DONE_STEP or step.status == "awaiting_approval":
        return None
    _mark_done(step, by=by, member_id=member_id, evidence=evidence)
    log_action(
        session, "step_done", household_id=wf.household_id, member_id=member_id,
        actor="member" if by == "user" else by, target=f"workflow:{wf.id}", step=step.key,
        evidence=evidence,
    )
    # Une relance devenue inutile (le remboursement est arrive) est abandonnee.
    if step.owner == "evidence":
        for other in open_steps(wf):
            if other.optional and other.requires_approval:
                other.status = "skipped"
                for approval in _pending_approvals(session, wf, other.key):
                    approval.status = "rejected"
                    approval.decided_at = now_utc()
    refresh(session, wf, today)
    return step


def _pending_approvals(session: Session, wf: Workflow, step_key: str) -> list[Approval]:
    return list(
        session.scalars(
            select(Approval).where(
                Approval.workflow_id == wf.id,
                Approval.step_key == step_key,
                Approval.status == "pending",
            )
        )
    )


def decide(session: Session, approval: Approval, approved: bool, member: Member, today: date) -> Workflow:
    """Enregistre la decision sur un brouillon.

    Approuve : l'etape passe a « approved ». Si c'est MUMU qui devait
    preparer quelque chose (un brouillon), l'etape est faite ; s'il faut
    l'envoyer, c'est l'etape suivante de la personne. Refuse : l'etape revient
    a faire et le brouillon est abandonne.
    """
    wf = session.get(Workflow, approval.workflow_id)
    step = _step(wf, approval.step_key)
    approval.status = "approved" if approved else "rejected"
    approval.decided_at = now_utc()
    log_action(
        session, "approval_decided", household_id=wf.household_id, member_id=member.id,
        actor="member", target=f"approval:{approval.id}", approved=approved,
    )
    if step is None or step.status != "awaiting_approval":
        return wf
    if not approved:
        # Sans accord, l'etape est confiee a la personne, sans nouveau brouillon.
        step.status = "todo"
        step.requires_approval = False
        if step.optional:
            step.status = "skipped"
        refresh(session, wf, today)
        return wf
    if step.owner == "mumu" and not step.optional:
        # Le brouillon etait le travail de MUMU : il est fait. L'envoi est
        # l'etape suivante de la personne (ex. « submit »).
        _mark_done(step, by="mumu", member_id=member.id, evidence=f"approval:{approval.id}")
    else:
        # Le texte est pret et accepte, mais rien n'est encore envoye :
        # l'etape reste ouverte jusqu'a ce que la personne confirme l'envoi.
        step.status = "approved"
        step.requires_approval = False
        if step.owner == "mumu":
            step.owner = "responsible"
            step.title = step.title.replace("Relancer", "Envoyer la relance à", 1)
    refresh(session, wf, today)
    return wf


def resolve(session: Session, wf: Workflow, resolution: str) -> None:
    if not is_open(wf):
        return
    wf.status = "resolved"
    wf.resolution = resolution[:300]
    wf.resolved_at = now_utc()
    wf.next_action = None
    wf.blocker = None
    log_action(
        session, "workflow_resolved", household_id=wf.household_id, target=f"workflow:{wf.id}",
        resolution=resolution,
    )


def close(session: Session, wf: Workflow, member: Member, note: str | None = None) -> None:
    """Fermeture explicite par une personne : « laisse tomber », « c'est reglé »."""
    if not is_open(wf):
        return
    wf.status = "closed"
    wf.resolution = (note or "Fermé à ta demande.")[:300]
    wf.resolved_at = now_utc()
    wf.next_action = None
    for approval in session.scalars(
        select(Approval).where(Approval.workflow_id == wf.id, Approval.status == "pending")
    ):
        approval.status = "rejected"
        approval.decided_at = now_utc()
    log_action(
        session, "workflow_closed", household_id=wf.household_id, member_id=member.id,
        actor="member", target=f"workflow:{wf.id}",
    )


def resolve_by_evidence(session: Session, wf: Workflow, step_key: str, evidence: str, today: date) -> bool:
    """Une preuve exterieure (mail de confirmation) valide une etape."""
    step = complete_step(session, wf, step_key, by="evidence", today=today, evidence=evidence)
    return step is not None
