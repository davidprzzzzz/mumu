"""Conservation limitee des conversations.

Passe le delai, le texte des messages est efface. La ligne reste, vide, pour
que les informations qui en ont ete tirees gardent leur reference.
"""

from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from .audit import log_action
from .models import Message, now_utc


def purge_old_messages(session_factory: sessionmaker, days: int) -> int:
    if days <= 0:
        return 0
    limit = now_utc() - timedelta(days=days)
    with session_factory() as session:
        result = session.execute(
            update(Message)
            .where(Message.created_at < limit, Message.body != "")
            .values(body="", media_type=None)
        )
        if result.rowcount:
            log_action(session, "messages_purged", actor="system", count=result.rowcount, days=days)
        session.commit()
        return result.rowcount or 0
