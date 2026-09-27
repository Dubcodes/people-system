from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import SessionLocal
from models import AuditEvent, Image, ManualAttendee, Person, Race, SystemSetting, TrackAlias
from services.auth_service import regional_pin_key, require_edit_pin, require_master_admin_pin
from services.audit_service import record_audit_event
from services.backup_service import backup_directory, create_backup_snapshot, delete_backup_snapshot, list_backup_snapshots, restore_backup_snapshot
from services.database_health_service import DatabaseHealthCleanupError, remove_safe_orphan_horses, scan_database_health
from services.instance_service import instance_id, instance_name
from services.region_service import allowed_regions, default_region, normalize_region
from services.system_settings_service import get_setting_value, set_setting_value
from services.track_alias_service import cleanup_track_aliases, snapshot_track_alias


router = APIRouter(prefix="/api/admin", tags=["admin"])


UNTAGGED_MODEL_MAP = {
    "people": {
        "model": Person,
        "entity_type": "person",
        "label": lambda row: row.name,
        "summary": lambda row: {
            "name": row.name,
            "notes": row.notes,
        },
    },
    "images": {
        "model": Image,
        "entity_type": "image",
        "label": lambda row: f"Image {row.id}",
        "summary": lambda row: {
            "person_id": row.person_id,
            "file_path": row.file_path,
            "location_taken": row.location_taken,
        },
    },
    "races": {
        "model": Race,
        "entity_type": "race",
        "label": lambda row: f"{row.track} R{row.race_number}",
        "summary": lambda row: {
            "track": row.track,
            "race_number": row.race_number,
            "date": row.date.isoformat() if row.date else None,
        },
    },
    "manual_attendees": {
        "model": ManualAttendee,
        "entity_type": "manual_attendee",
        "label": lambda row: f"Manual attendee {row.id}",
        "summary": lambda row: {
            "person_id": row.person_id,
            "track": row.track,
            "date": row.date.isoformat() if row.date else None,
            "race_id": row.race_id,
        },
    },
}

TABLE_EDITOR_CONFIG = {
    "people": {
        "model": Person,
        "entity_type": "person",
        "editable_fields": ["name", "notes", "source_region", "visible_regions", "deleted_at", "deleted_by_instance", "delete_reason"],
        "summary_fields": ["id", "name", "source_region", "visible_regions", "deleted_at"],
        "order_by": Person.id.desc(),
    },
    "images": {
        "model": Image,
        "entity_type": "image",
        "editable_fields": ["person_id", "file_path", "thumbnail_path", "source_region", "width", "height", "location_taken", "notes", "deleted_at", "deleted_by_instance", "delete_reason"],
        "summary_fields": ["id", "person_id", "source_region", "location_taken", "is_preferred", "deleted_at"],
        "order_by": Image.id.desc(),
    },
    "races": {
        "model": Race,
        "entity_type": "race",
        "editable_fields": ["track", "source_region", "race_number", "date"],
        "summary_fields": ["id", "track", "race_number", "date", "source_region"],
        "order_by": Race.date.desc(),
    },
    "manual_attendees": {
        "model": ManualAttendee,
        "entity_type": "manual_attendee",
        "editable_fields": ["person_id", "date", "track", "source_region", "race_id", "note"],
        "summary_fields": ["id", "person_id", "track", "date", "race_id", "source_region"],
        "order_by": ManualAttendee.date.desc(),
    },
    "track_aliases": {
        "model": TrackAlias,
        "entity_type": "track_alias",
        "editable_fields": ["alias", "canonical_track", "course_name", "club_name", "location_name", "source"],
        "summary_fields": ["id", "alias", "canonical_track", "location_name", "source"],
        "order_by": TrackAlias.id.desc(),
    },
}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _coerce_editor_value(column, value):
    if value == "":
        value = None
    if value is None:
        return None

    python_type = None
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        python_type = None

    if python_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y", "on"}:
                return True
            if lowered in {"false", "0", "no", "n", "off"}:
                return False
        raise HTTPException(status_code=422, detail=f"Field '{column.name}' must be true or false.")

    if python_type is int:
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=f"Field '{column.name}' must be a whole number.") from error

    if python_type is date and not isinstance(value, datetime):
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"Field '{column.name}' must use YYYY-MM-DD.") from error

    if python_type is datetime:
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"Field '{column.name}' must use an ISO datetime string.") from error

    if column.name == "source_region":
        normalized = normalize_region(str(value))
        if not normalized:
            raise HTTPException(status_code=422, detail=f"Field '{column.name}' must be one of: {', '.join(allowed_regions())}.")
        return normalized

    return str(value) if isinstance(value, str) else value


def _serialize_editor_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _table_row_snapshot(row, fields=None):
    column_names = fields or [column.name for column in row.__table__.columns]
    return {name: _serialize_editor_value(getattr(row, name)) for name in column_names}


def _get_table_config(table_name: str):
    config = TABLE_EDITOR_CONFIG.get(table_name)
    if not config:
        raise HTTPException(status_code=404, detail="That table is not available in the safe editor.")
    return config


def _load_editor_row(db: Session, table_name: str, row_id: int):
    config = _get_table_config(table_name)
    row = db.query(config["model"]).filter(config["model"].id == row_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="That row was not found.")
    return config, row


@router.post("/check")
def check_admin_pin(_: None = Depends(require_edit_pin)):
    return {"status": "ok", "level": "edit"}


@router.post("/edit-check")
def check_edit_pin(_: None = Depends(require_edit_pin)):
    return {"status": "ok", "level": "edit"}


@router.post("/master-check")
def check_master_admin_pin(_: None = Depends(require_master_admin_pin)):
    return {"status": "ok"}


@router.get("/instance")
def get_instance_info():
    return {"instance_id": instance_id(), "instance_name": instance_name()}


@router.get("/master/overview")
def master_overview(db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    settings_keys = {item.key for item in db.query(SystemSetting).all()}
    total_audit_events = db.query(AuditEvent).count()
    recent_audit = (
        db.query(AuditEvent)
        .order_by(AuditEvent.created_at.desc())
        .limit(10)
        .all()
    )
    recent_instances = []
    seen = set()
    for item in recent_audit:
        key = (item.instance_id or "", item.instance_name or "")
        if key in seen:
            continue
        seen.add(key)
        recent_instances.append(
            {
                "instance_id": item.instance_id,
                "instance_name": item.instance_name,
                "last_event_at": item.created_at.isoformat() if item.created_at else None,
                "last_action": item.action,
                "last_entity_type": item.entity_type,
            }
        )

    return {
        "instance_id": instance_id(),
        "instance_name": instance_name(),
        "db_overrides": {
            "edit_pin": "edit_pin" in settings_keys,
            "admin_pin": "admin_pin" in settings_keys,
            "master_admin_pin": "master_admin_pin" in settings_keys,
        },
        "recent_instances": recent_instances,
        "backup_count": len(list_backup_snapshots()),
        "backup_directory": backup_directory(),
        "audit_event_count": total_audit_events,
    }


@router.get("/master/database-health")
def master_database_health_scan(db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    """Return a read-only, server-generated maintenance report."""
    return scan_database_health(db)


@router.post("/master/database-health/remove-safe-orphan-junk")
def master_remove_safe_orphan_junk(
    payload: dict,
    db: Session = Depends(get_db),
    _: None = Depends(require_master_admin_pin),
):
    try:
        return remove_safe_orphan_horses(db, payload.get("horse_ids"), bool(payload.get("confirm")))
    except DatabaseHealthCleanupError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error


@router.get("/master/untagged")
def master_untagged_records(limit: int = Query(default=50, ge=1, le=500), db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    sections = []
    total_rows = 0
    for key, config in UNTAGGED_MODEL_MAP.items():
        rows = []
        for row in db.query(config["model"]).order_by(config["model"].id.asc()).all():
            if normalize_region(getattr(row, "source_region", None)):
                continue
            rows.append(
                {
                    "id": row.id,
                    "table": key,
                    "label": config["label"](row),
                    "source_region": getattr(row, "source_region", None),
                    "summary": config["summary"](row),
                }
            )
            if len(rows) >= limit:
                break
        total_rows += len(rows)
        sections.append({"table": key, "count": len(rows), "rows": rows})
    return {"total_rows": total_rows, "allowed_regions": allowed_regions(), "sections": sections}


@router.put("/master/untagged/{table_name}/{row_id}/region")
def master_set_untagged_region(table_name: str, row_id: int, payload: dict, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    config = UNTAGGED_MODEL_MAP.get(table_name)
    if not config:
        raise HTTPException(status_code=404, detail="That untagged section was not found.")
    row = db.query(config["model"]).filter(config["model"].id == row_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="That row was not found.")
    new_region = normalize_region(payload.get("source_region"))
    if not new_region:
        raise HTTPException(status_code=422, detail=f"Region must be one of: {', '.join(allowed_regions())}.")

    before = {"source_region": getattr(row, "source_region", None)}
    row.source_region = new_region
    after = {"source_region": row.source_region}
    record_audit_event(
        db,
        action="retag_region",
        entity_type=config["entity_type"],
        entity_id=row.id,
        before=before,
        after=after,
        notes=f"Master admin set source_region on {table_name} row {row.id}.",
        actor="master_admin",
    )
    db.commit()
    return {"status": "ok", "id": row.id, "table": table_name, "source_region": row.source_region}


@router.get("/master/table-editor")
def master_table_editor_list(
    table: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: None = Depends(require_master_admin_pin),
):
    config = _get_table_config(table)
    rows = (
        db.query(config["model"])
        .order_by(config["order_by"])
        .limit(limit)
        .all()
    )
    return {
        "table": table,
        "editable_fields": config["editable_fields"],
        "summary_fields": config["summary_fields"],
        "rows": [_table_row_snapshot(row, config["summary_fields"]) for row in rows],
    }


@router.get("/master/table-editor/config")
def master_table_editor_config(_: None = Depends(require_master_admin_pin)):
    return {
        "tables": [
            {
                "name": table_name,
                "editable_fields": config["editable_fields"],
                "summary_fields": config["summary_fields"],
            }
            for table_name, config in TABLE_EDITOR_CONFIG.items()
        ]
    }


@router.get("/master/table-editor/{table_name}/{row_id}")
def master_table_editor_row(table_name: str, row_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    config, row = _load_editor_row(db, table_name, row_id)
    return {
        "table": table_name,
        "editable_fields": config["editable_fields"],
        "row": _table_row_snapshot(row),
    }


@router.put("/master/table-editor/{table_name}/{row_id}")
def master_table_editor_update(table_name: str, row_id: int, payload: dict, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    config, row = _load_editor_row(db, table_name, row_id)
    before = _table_row_snapshot(row, config["editable_fields"])
    allowed_field_names = set(config["editable_fields"])

    for field_name, value in payload.items():
        if field_name not in allowed_field_names:
            raise HTTPException(status_code=422, detail=f"Field '{field_name}' is not editable for table '{table_name}'.")
        column = row.__table__.columns.get(field_name)
        setattr(row, field_name, _coerce_editor_value(column, value))

    after = _table_row_snapshot(row, config["editable_fields"])
    record_audit_event(
        db,
        action="table_editor_update",
        entity_type=config["entity_type"],
        entity_id=row.id,
        before=before,
        after=after,
        notes=f"Master admin updated {table_name} row {row.id} in the safe table editor.",
        actor="master_admin",
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="That update conflicts with an existing row or database rule.") from error
    return {"status": "ok", "table": table_name, "row": _table_row_snapshot(row)}


@router.get("/master/track-aliases")
def master_track_aliases(
    search: str | None = Query(default=None),
    limit: int = Query(default=300, ge=1, le=2000),
    db: Session = Depends(get_db),
    _: None = Depends(require_master_admin_pin),
):
    query = db.query(TrackAlias)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.filter(
            TrackAlias.alias.ilike(pattern)
            | TrackAlias.canonical_track.ilike(pattern)
            | TrackAlias.course_name.ilike(pattern)
            | TrackAlias.club_name.ilike(pattern)
            | TrackAlias.location_name.ilike(pattern)
            | TrackAlias.source.ilike(pattern)
        )
    aliases = (
        query.order_by(TrackAlias.canonical_track.asc(), TrackAlias.alias.asc(), TrackAlias.id.asc())
        .limit(limit)
        .all()
    )
    return {
        "count": len(aliases),
        "limit": limit,
        "search": search or "",
        "aliases": [
            {
                "id": item.id,
                "alias": item.alias,
                "canonical_track": item.canonical_track,
                "course_name": item.course_name,
                "club_name": item.club_name,
                "location_name": item.location_name,
                "source": item.source,
            }
            for item in aliases
        ],
    }


def _clean_track_alias_payload(payload: dict) -> dict:
    alias = (payload.get("alias") or "").strip()
    canonical_track = (payload.get("canonical_track") or "").strip()
    if not alias:
        raise HTTPException(status_code=422, detail="Alias is required.")
    if not canonical_track:
        raise HTTPException(status_code=422, detail="Canonical track is required.")
    return {
        "alias": alias,
        "canonical_track": canonical_track,
        "course_name": (payload.get("course_name") or "").strip() or canonical_track,
        "club_name": (payload.get("club_name") or "").strip() or None,
        "location_name": (payload.get("location_name") or "").strip() or None,
        "source": (payload.get("source") or "").strip() or "manual",
    }


@router.post("/master/track-aliases")
def master_create_track_alias(payload: dict, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    cleaned = _clean_track_alias_payload(payload)
    existing = db.query(TrackAlias).filter(TrackAlias.alias == cleaned["alias"]).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Track alias '{cleaned['alias']}' already exists.")
    row = TrackAlias(**cleaned)
    db.add(row)
    db.flush()
    after = snapshot_track_alias(row)
    record_audit_event(
        db,
        action="create",
        entity_type="track_alias",
        entity_id=row.id,
        after=after,
        notes="Master admin created a track alias row.",
        actor="master_admin",
    )
    db.commit()
    return after


@router.put("/master/track-aliases/{alias_id}")
def master_update_track_alias(alias_id: int, payload: dict, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    row = db.query(TrackAlias).filter(TrackAlias.id == alias_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Track alias row was not found.")
    cleaned = _clean_track_alias_payload(payload)
    duplicate = db.query(TrackAlias).filter(TrackAlias.alias == cleaned["alias"], TrackAlias.id != alias_id).first()
    if duplicate:
        raise HTTPException(status_code=409, detail=f"Track alias '{cleaned['alias']}' already exists.")

    before = snapshot_track_alias(row)
    row.alias = cleaned["alias"]
    row.canonical_track = cleaned["canonical_track"]
    row.course_name = cleaned["course_name"]
    row.club_name = cleaned["club_name"]
    row.location_name = cleaned["location_name"]
    row.source = cleaned["source"]
    after = snapshot_track_alias(row)
    record_audit_event(
        db,
        action="update",
        entity_type="track_alias",
        entity_id=row.id,
        before=before,
        after=after,
        notes="Master admin updated a track alias row.",
        actor="master_admin",
    )
    db.commit()
    return after


@router.delete("/master/track-aliases/{alias_id}")
def master_delete_track_alias(alias_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    row = db.query(TrackAlias).filter(TrackAlias.id == alias_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Track alias row was not found.")
    before = snapshot_track_alias(row)
    record_audit_event(
        db,
        action="delete",
        entity_type="track_alias",
        entity_id=row.id,
        before=before,
        notes="Master admin deleted a track alias row.",
        actor="master_admin",
    )
    db.delete(row)
    db.commit()
    return {"status": "ok", "deleted": before}


@router.post("/master/track-aliases/cleanup")
def master_cleanup_track_aliases(db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    result = cleanup_track_aliases(db)
    record_audit_event(
        db,
        action="cleanup",
        entity_type="track_aliases",
        after=result,
        notes="Ran the full track alias cleanup pass: obvious junk, address duplicates, URL/email/contact junk, and location normalization.",
        actor="master_admin",
    )
    db.commit()
    return result


@router.get("/master/audit-events")
def master_audit_events(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    events = (
        db.query(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "events": [
            {
                "id": event.id,
                "created_at": event.created_at.isoformat() if event.created_at else None,
                "instance_id": event.instance_id,
                "instance_name": event.instance_name,
                "actor": event.actor,
                "action": event.action,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "before_json": event.before_json,
                "after_json": event.after_json,
                "notes": event.notes,
            }
            for event in events
        ]
    }


@router.get("/master/password-status")
def master_password_status(db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    region = default_region()
    edit_key = regional_pin_key("edit_pin", region)
    admin_key = regional_pin_key("admin_pin", region)
    master_key = regional_pin_key("master_admin_pin", region)
    return {
        "current_region": region,
        "edit_pin_overridden": bool(get_setting_value(db, edit_key) or get_setting_value(db, admin_key)),
        "master_admin_pin_overridden": bool(get_setting_value(db, master_key)),
        "legacy_global_edit_pin_overridden": bool(get_setting_value(db, "edit_pin") or get_setting_value(db, "admin_pin")),
        "legacy_admin_pin_overridden": bool(get_setting_value(db, "admin_pin")),
        "legacy_global_master_admin_pin_overridden": bool(get_setting_value(db, "master_admin_pin")),
        "edit_pin_key": edit_key,
        "master_admin_pin_key": master_key,
        "fallback_note": "Environment EDIT_PIN/ADMIN_PIN and MASTER_ADMIN_PIN remain fallbacks for recovery. Defaults are also accepted unless changed in the app environment.",
    }


@router.put("/master/passwords")
def master_update_passwords(payload: dict, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    region = normalize_region(payload.get("region")) or default_region()
    edit_key = regional_pin_key("edit_pin", region)
    admin_key = regional_pin_key("admin_pin", region)
    master_key = regional_pin_key("master_admin_pin", region)
    edit_value = (payload.get("edit_pin") or payload.get("admin_pin") or "").strip() or None
    master_value = (payload.get("master_admin_pin") or "").strip() or None
    clear_edit = bool(payload.get("clear_edit_override") or payload.get("clear_admin_override"))
    clear_master = bool(payload.get("clear_master_override"))
    clear_legacy_global = bool(payload.get("clear_legacy_global_overrides"))

    before = {
        "region": region,
        "edit_pin_overridden": bool(get_setting_value(db, edit_key) or get_setting_value(db, admin_key)),
        "master_admin_pin_overridden": bool(get_setting_value(db, master_key)),
        "legacy_global_edit_pin_overridden": bool(get_setting_value(db, "edit_pin") or get_setting_value(db, "admin_pin")),
        "legacy_global_master_admin_pin_overridden": bool(get_setting_value(db, "master_admin_pin")),
    }

    if edit_value is not None and (not edit_value.isdigit() or len(edit_value) < 4):
        raise HTTPException(status_code=422, detail="Edit PIN must be at least 4 digits.")
    if master_value is not None and (not master_value.isdigit() or len(master_value) < 4):
        raise HTTPException(status_code=422, detail="Master PIN must be at least 4 digits.")

    if clear_edit:
        set_setting_value(db, edit_key, None)
        set_setting_value(db, admin_key, None)
    elif edit_value is not None:
        set_setting_value(db, edit_key, edit_value)

    if clear_master:
        set_setting_value(db, master_key, None)
    elif master_value is not None:
        set_setting_value(db, master_key, master_value)

    if clear_legacy_global:
        set_setting_value(db, "edit_pin", None)
        set_setting_value(db, "admin_pin", None)
        set_setting_value(db, "master_admin_pin", None)

    record_audit_event(
        db,
        action="update_passwords",
        entity_type="system_settings",
        before=before,
        after={
            "region": region,
            "edit_pin_overridden": bool(get_setting_value(db, edit_key) or get_setting_value(db, admin_key)),
            "master_admin_pin_overridden": bool(get_setting_value(db, master_key)),
            "legacy_global_edit_pin_overridden": bool(get_setting_value(db, "edit_pin") or get_setting_value(db, "admin_pin")),
            "legacy_global_master_admin_pin_overridden": bool(get_setting_value(db, "master_admin_pin")),
        },
        notes=f"Master admin updated {region} Edit/Master PIN overrides. Raw PIN values were not stored in the audit log.",
        actor="master_admin",
    )
    db.commit()
    return {"status": "ok"}


@router.get("/master/backups")
def master_backups(_: None = Depends(require_master_admin_pin)):
    return {"backups": list_backup_snapshots(), "backup_directory": backup_directory()}


@router.post("/master/backups/create")
def master_create_backup(db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    snapshot = create_backup_snapshot(db, reason="manual_master_admin")
    record_audit_event(
        db,
        action="create_backup",
        entity_type="backup",
        notes=f"Created backup {snapshot['filename']}",
        after={"filename": snapshot["filename"], "table_counts": snapshot["table_counts"]},
        actor="master_admin",
    )
    db.commit()
    return snapshot


@router.delete("/master/backups")
def master_delete_backup(filename: str = Query(...), db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    try:
        result = delete_backup_snapshot(filename)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Backup {filename} was not found.") from error
    record_audit_event(
        db,
        action="delete_backup",
        entity_type="backup",
        notes=f"Deleted backup {filename}",
        after={"filename": filename},
        actor="master_admin",
    )
    db.commit()
    return result


@router.post("/master/thumbnails/generate")
def master_generate_thumbnails(db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    from services.ingest_service import generate_missing_thumbnails

    result = generate_missing_thumbnails(db)
    record_audit_event(
        db,
        action="maintenance",
        entity_type="thumbnails",
        after=result,
        notes="Generated missing thumbnails from the admin page.",
        actor="master_admin",
    )
    db.commit()
    return result


@router.post("/master/backups/restore")
def master_restore_backup(payload: dict, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    filename = (payload.get("filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=422, detail="Backup filename is required.")

    safety_backup = create_backup_snapshot(db, reason=f"pre_restore_{filename}")
    try:
        result = restore_backup_snapshot(filename)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Backup {filename} was not found.") from error

    record_audit_event(
        db,
        action="restore_backup",
        entity_type="backup",
        notes=f"Restored backup {filename}. Safety backup created first as {safety_backup['filename']}.",
        before={"restore_source": filename},
        after={"safety_backup": safety_backup["filename"]},
        actor="master_admin",
    )
    db.commit()
    return {**result, "safety_backup": safety_backup["filename"]}
