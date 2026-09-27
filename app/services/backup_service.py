import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import engine
from models import (
    Absence,
    Alias,
    AuditEvent,
    Horse,
    HorsePerson,
    Image,
    LoveracingImportRun,
    ManualAttendee,
    PendingImage,
    Person,
    PersonRole,
    PersonStable,
    Race,
    RaceCourse,
    RaceEntry,
    RaceMeeting,
    Role,
    Stable,
    SystemSetting,
    TrackAlias,
)
from services.instance_service import instance_id, instance_name


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


BACKUP_DIR = Path(os.getenv("PEOPLE_BACKUP_DIR", Path(__file__).resolve().parent.parent / "backups"))
MANUAL_BACKUP_PREFIX = "people-system-backup-"
AUTO_BACKUP_PREFIX = "people-system-auto-backup-"
AUTO_BACKUP_SETTING_KEY = "auto_backup_last_created_at"
AUTO_BACKUP_KEEP = _env_int("PEOPLE_AUTO_BACKUP_KEEP", 4)
AUTO_BACKUP_DAYS = _env_int("PEOPLE_AUTO_BACKUP_DAYS", 7)
AUTO_BACKUP_PRIMARY_INSTANCE = os.getenv("PEOPLE_AUTO_BACKUP_PRIMARY_INSTANCE", "northern")
BACKUP_TABLES = [
    SystemSetting,
    RaceCourse,
    Role,
    Stable,
    Person,
    Horse,
    Alias,
    Image,
    PendingImage,
    PersonRole,
    PersonStable,
    HorsePerson,
    RaceMeeting,
    Race,
    RaceEntry,
    LoveracingImportRun,
    ManualAttendee,
    Absence,
    TrackAlias,
    AuditEvent,
]
RESTORE_TABLES = BACKUP_TABLES.copy()


def _serialise_row(model, row) -> dict:
    data = {}
    for column in model.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, datetime):
            data[column.name] = value.isoformat(sep=" ")
        else:
            data[column.name] = value.isoformat() if hasattr(value, "isoformat") and not isinstance(value, str) else value
    return data


def _filter_known_columns(model, rows: list[dict]) -> list[dict]:
    known_columns = {column.name for column in model.__table__.columns}
    return [{key: value for key, value in row.items() if key in known_columns} for row in rows]


def _seed_default_racecourses(connection) -> None:
    for course in ["Cambridge Synthetic", "Te Aroha", "Matamata", "Te Rapa", "Tauranga", "Rotorua", "Ellerslie", "Pukekohe"]:
        connection.execute(
            text(
                "INSERT INTO racecourses (name, loveracing_name, source_region, is_local_tagged, active, created_at, updated_at) "
                "VALUES (:name, :loveracing_name, 'Northern', TRUE, TRUE, NOW(), NOW()) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"name": course, "loveracing_name": course},
        )


def _backup_path(filename: str) -> Path:
    path = BACKUP_DIR / filename
    if path.name != filename or not filename.endswith(".json"):
        raise ValueError("Invalid backup filename")
    return path


def _backup_files(pattern: str) -> list[Path]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(BACKUP_DIR.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)


def _setting_value(db: Session, key: str) -> str | None:
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return setting.value if setting else None


def _set_setting_value(db: Session, key: str, value: str) -> None:
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if not setting:
        setting = SystemSetting(key=key)
        db.add(setting)
    setting.value = value
    setting.updated_at = datetime.utcnow()


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None


def _auto_backup_enabled_for_instance() -> bool:
    primary = (AUTO_BACKUP_PRIMARY_INSTANCE or "").strip().lower()
    if primary in {"", "*", "all"}:
        return True
    return instance_id().strip().lower() == primary


def create_backup_snapshot(db: Session, reason: str = "manual", automatic: bool = False) -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    prefix = AUTO_BACKUP_PREFIX if automatic else MANUAL_BACKUP_PREFIX
    filename = f"{prefix}{stamp}.json"
    path = BACKUP_DIR / filename
    payload = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "reason": reason,
        "automatic": automatic,
        "instance_id": instance_id(),
        "instance_name": instance_name(),
        "tables": {
            model.__table__.name: [_serialise_row(model, row) for row in db.query(model).all()]
            for model in BACKUP_TABLES
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "filename": filename,
        "path": str(path),
        "automatic": automatic,
        "table_counts": {name: len(rows) for name, rows in payload["tables"].items()},
    }


def list_backup_snapshots() -> list[dict]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = []
    paths = _backup_files(f"{MANUAL_BACKUP_PREFIX}*.json") + _backup_files(f"{AUTO_BACKUP_PREFIX}*.json")
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        snapshots.append(
            {
                "filename": path.name,
                "path": str(path),
                "automatic": path.name.startswith(AUTO_BACKUP_PREFIX),
                "size_bytes": stat.st_size,
                "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
            }
        )
    return snapshots


def backup_directory() -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return str(BACKUP_DIR)


def delete_backup_snapshot(filename: str) -> dict:
    path = _backup_path(filename)
    if not path.exists():
        raise FileNotFoundError(filename)
    path.unlink()
    return {"status": "ok", "filename": filename}


def prune_auto_backups(keep: int = AUTO_BACKUP_KEEP) -> dict:
    keep = max(1, int(keep or AUTO_BACKUP_KEEP))
    deleted = []
    for path in _backup_files(f"{AUTO_BACKUP_PREFIX}*.json")[keep:]:
        path.unlink()
        deleted.append(path.name)
    return {"deleted": deleted, "kept": keep}


def maybe_create_weekly_auto_backup(db: Session, now: datetime | None = None) -> dict:
    if not _auto_backup_enabled_for_instance():
        return {"status": "skipped", "reason": "not_primary_instance", "instance_id": instance_id()}

    now = now or datetime.utcnow()
    last_created = _parse_utc_timestamp(_setting_value(db, AUTO_BACKUP_SETTING_KEY))
    if last_created and now - last_created < timedelta(days=AUTO_BACKUP_DAYS):
        prune_auto_backups()
        return {
            "status": "skipped",
            "reason": "not_due",
            "last_created_at": last_created.isoformat() + "Z",
        }

    snapshot = create_backup_snapshot(db, reason="weekly_auto", automatic=True)
    _set_setting_value(db, AUTO_BACKUP_SETTING_KEY, now.isoformat() + "Z")
    snapshot["retention"] = prune_auto_backups()
    return snapshot


def restore_backup_snapshot(filename: str) -> dict:
    path = _backup_path(filename)
    if not path.exists():
        raise FileNotFoundError(filename)

    payload = json.loads(path.read_text(encoding="utf-8"))
    tables = payload.get("tables") or {}
    people_rows = tables.get("people", [])
    preferred_map = {row["id"]: row.get("preferred_image_id") for row in people_rows}
    people_rows_without_preferred = [{**row, "preferred_image_id": None} for row in people_rows]
    table_rows = {**tables, "people": people_rows_without_preferred}

    with engine.begin() as connection:
        table_names = ", ".join(model.__table__.name for model in reversed(RESTORE_TABLES))
        connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))

        for model in RESTORE_TABLES:
            rows = table_rows.get(model.__table__.name, [])
            if rows:
                connection.execute(model.__table__.insert(), _filter_known_columns(model, rows))

        if not tables.get("racecourses"):
            _seed_default_racecourses(connection)

        for person_id, preferred_image_id in preferred_map.items():
            if preferred_image_id:
                connection.execute(
                    text("UPDATE people SET preferred_image_id = :preferred_image_id WHERE id = :person_id"),
                    {"preferred_image_id": preferred_image_id, "person_id": person_id},
                )

    return {
        "status": "ok",
        "restored_from": filename,
        "table_counts": {name: len(rows) for name, rows in tables.items()},
    }
