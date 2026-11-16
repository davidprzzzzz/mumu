"""Journal des actions importantes."""

from sqlalchemy.orm import Session

from .models import AuditLog


def log_action(
    session: Session,
    action: str,
    *,
    household_id: int | None = None,
    member_id: int | None = None,
    actor: str = "mumu",
    target: str | None = None,
    **detail,
) -> None:
    session.add(
        AuditLog(
            household_id=household_id,
            member_id=member_id,
            actor=actor,
            action=action,
            target=target,
            detail={k: v for k, v in detail.items() if v is not None},
        )
    )
