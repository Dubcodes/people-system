import json
from typing import Any

from sqlalchemy.orm import Session

from models import AuditEvent
from services.instance_service import instance_id, instance_name


def record_audit_event(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    notes: str | None = None,
    actor: str = "admin",
) -> None:
    db.add(
        AuditEvent(
            instance_id=instance_id(),
            instance_name=instance_name(),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_json=json.dumps(before, default=str) if before is not None else None,
            after_json=json.dumps(after, default=str) if after is not None else None,
            notes=notes,
        )
    )
