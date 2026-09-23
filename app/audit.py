from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(session: Session, action: str, entity_type: str, entity_id: Any = None,
          actor: str = "system", **details: Any) -> None:
    """Add an audit row to the caller's transaction, so it commits (or rolls back)
    together with the change it describes."""
    session.add(AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        details=details,
    ))
