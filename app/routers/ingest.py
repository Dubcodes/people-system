import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from database import SessionLocal
from models import PendingImage
from schemas import IngestResponse
from services.audit_service import record_audit_event
from services.auth_service import require_edit_pin, require_master_admin_pin
from services.ingest_service import generate_missing_thumbnails, save_ingested_image
from services.instance_service import instance_id, instance_name
from services.region_service import default_region, normalize_region

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
BASE_DIR = Path(__file__).resolve().parent.parent
PENDING_DIR = BASE_DIR / "pending_images"
PENDING_STATUSES = {"pending", "claimed", "processed", "skipped"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _pending_payload(row: PendingImage) -> dict:
    return {
        "id": row.id,
        "original_filename": row.original_filename,
        "file_url": f"/{row.file_path}",
        "file_hash": row.file_hash,
        "uploading_instance_id": row.uploading_instance_id,
        "uploading_instance_name": row.uploading_instance_name,
        "uploading_region": row.uploading_region,
        "intended_region": row.intended_region,
        "status": row.status,
        "notes": row.notes,
        "processed_person_id": row.processed_person_id,
        "processed_image_id": row.processed_image_id,
        "claimed_by_instance": row.claimed_by_instance,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/", response_model=IngestResponse)
async def upload_image(
    file: UploadFile = File(...),
    name: str = Form(...),
    role: str | None = Form(default=None),
    stable: str | None = Form(default=None),
    horse_name: str | None = Form(default=None),
    location_taken: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    aliases: str | None = Form(default=None),
    aspect_ratio: str = Form(default="4:3"),
    source_region: str | None = Form(default=None),
    db: Session = Depends(get_db),
    _: None = Depends(require_edit_pin),
):
    file_bytes = await file.read()
    return save_ingested_image(
        db,
        file_bytes=file_bytes,
        original_filename=file.filename or "upload.jpg",
        name=name,
        role=role,
        stable=stable,
        horse_name=horse_name,
        location_taken=location_taken,
        notes=notes,
        alias_text=aliases,
        aspect_ratio=aspect_ratio,
        source_region=source_region,
    )


@router.post("/thumbnails/generate")
def build_missing_thumbnails(
    db: Session = Depends(get_db),
    _: None = Depends(require_edit_pin),
):
    return generate_missing_thumbnails(db)


@router.post("/pending")
async def upload_pending_image(
    file: UploadFile = File(...),
    intended_region: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    db: Session = Depends(get_db),
    _: None = Depends(require_edit_pin),
):
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=422, detail="Image file is empty.")
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    existing = (
        db.query(PendingImage)
        .filter(PendingImage.file_hash == file_hash, PendingImage.status.in_(["pending", "claimed"]))
        .first()
    )
    if existing:
        return {"status": "duplicate", "pending_image": _pending_payload(existing)}
    normalized_region = normalize_region(intended_region) if intended_region else None
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload.jpg").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    filename = f"{uuid.uuid4().hex}{suffix}"
    relative_path = f"pending_images/{filename}"
    disk_path = BASE_DIR / relative_path
    with disk_path.open("wb") as handle:
        handle.write(file_bytes)
    row = PendingImage(
        original_filename=file.filename or "upload.jpg",
        file_path=relative_path,
        file_hash=file_hash,
        uploading_instance_id=instance_id(),
        uploading_instance_name=instance_name(),
        uploading_region=default_region(),
        intended_region=normalized_region,
        status="pending",
        notes=notes,
    )
    db.add(row)
    db.flush()
    record_audit_event(
        db,
        action="upload_pending",
        entity_type="pending_image",
        entity_id=row.id,
        after=_pending_payload(row),
        notes="Image uploaded to shared pending bulk-ingest queue.",
        actor="edit",
    )
    db.commit()
    db.refresh(row)
    return {"status": "ok", "pending_image": _pending_payload(row)}


@router.get("/pending")
def list_pending_images(
    scope: str = Query(default="pending"),
    db: Session = Depends(get_db),
):
    query = db.query(PendingImage)
    current_region = default_region()
    if scope == "my-region":
        query = query.filter(PendingImage.intended_region == current_region)
    elif scope == "uploaded-by-this-region":
        query = query.filter(PendingImage.uploading_region == current_region)
    elif scope == "unassigned":
        query = query.filter(PendingImage.intended_region.is_(None))
    elif scope == "done":
        query = query.filter(PendingImage.status.in_(["processed", "skipped"]))
    else:
        query = query.filter(PendingImage.status.in_(["pending", "claimed"]))
    rows = query.order_by(PendingImage.created_at.desc(), PendingImage.id.desc()).limit(300).all()
    return {"pending_images": [_pending_payload(row) for row in rows], "current_region": current_region}


@router.put("/pending/{pending_id}")
def update_pending_image(
    pending_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    _: None = Depends(require_edit_pin),
):
    row = db.query(PendingImage).filter(PendingImage.id == pending_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Pending image not found.")
    before = _pending_payload(row)
    if "intended_region" in payload:
        row.intended_region = normalize_region(payload.get("intended_region")) if payload.get("intended_region") else None
    if "notes" in payload:
        row.notes = payload.get("notes") or None
    if payload.get("status") in {"pending", "claimed", "skipped"}:
        row.status = payload["status"]
    row.updated_at = datetime.utcnow()
    after = _pending_payload(row)
    record_audit_event(db, "update", "pending_image", row.id, before=before, after=after, notes="Pending image metadata updated.", actor="edit")
    db.commit()
    db.refresh(row)
    return {"status": "ok", "pending_image": _pending_payload(row)}


@router.post("/pending/{pending_id}/process")
async def process_pending_image(
    pending_id: int,
    name: str = Form(...),
    role: str | None = Form(default=None),
    stable: str | None = Form(default=None),
    horse_name: str | None = Form(default=None),
    location_taken: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    aliases: str | None = Form(default=None),
    aspect_ratio: str = Form(default="4:3"),
    source_region: str | None = Form(default=None),
    db: Session = Depends(get_db),
    _: None = Depends(require_edit_pin),
):
    row = db.query(PendingImage).filter(PendingImage.id == pending_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Pending image not found.")
    current_region = default_region()
    intended = normalize_region(row.intended_region) or normalize_region(source_region) or current_region
    if intended != current_region:
        raise HTTPException(status_code=403, detail="Only process and submit images for your own region. Leave other-region images pending.")
    disk_path = BASE_DIR / row.file_path
    if not disk_path.exists():
        raise HTTPException(status_code=404, detail="Pending image file is missing.")
    before = _pending_payload(row)
    result = save_ingested_image(
        db,
        file_bytes=disk_path.read_bytes(),
        original_filename=row.original_filename,
        name=name,
        role=role,
        stable=stable,
        horse_name=horse_name,
        location_taken=location_taken,
        notes=notes or row.notes,
        alias_text=aliases,
        aspect_ratio=aspect_ratio,
        source_region=current_region,
    )
    row.status = "processed"
    row.processed_person_id = result.person_id
    row.processed_image_id = result.image_id
    row.intended_region = current_region
    row.claimed_by_instance = instance_id()
    row.updated_at = datetime.utcnow()
    after = _pending_payload(row)
    record_audit_event(db, "process", "pending_image", row.id, before=before, after=after, notes="Pending image processed into a person image.", actor="edit")
    db.commit()
    return {"status": "ok", "result": result, "pending_image": _pending_payload(row)}


@router.post("/pending/{pending_id}/skip")
def skip_pending_image(
    pending_id: int,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    _: None = Depends(require_edit_pin),
):
    row = db.query(PendingImage).filter(PendingImage.id == pending_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Pending image not found.")
    before = _pending_payload(row)
    row.status = "skipped"
    row.notes = (payload or {}).get("notes") or row.notes
    row.updated_at = datetime.utcnow()
    after = _pending_payload(row)
    record_audit_event(db, "skip", "pending_image", row.id, before=before, after=after, notes="Pending image skipped.", actor="edit")
    db.commit()
    return {"status": "ok", "pending_image": _pending_payload(row)}


@router.delete("/pending/{pending_id}")
def delete_pending_image(
    pending_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_master_admin_pin),
):
    row = db.query(PendingImage).filter(PendingImage.id == pending_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Pending image not found.")
    before = _pending_payload(row)
    record_audit_event(db, "delete", "pending_image", row.id, before=before, notes="Master deleted a pending image queue item.", actor="master_admin")
    disk_path = BASE_DIR / row.file_path
    db.delete(row)
    db.commit()
    if disk_path.exists():
        try:
            disk_path.unlink()
        except OSError:
            pass
    return {"status": "ok", "deleted": before}


