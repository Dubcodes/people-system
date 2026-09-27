from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from loveracing_importer.services import (
    discover_calendar_candidates,
    import_requires_edit,
    import_meeting_url,
    list_import_status,
    preview_meeting_url,
    undo_meeting_import,
    update_automation,
    upsert_racecourse_and_rematch,
)
from services.auth_service import require_edit_pin, require_master_admin_pin


router = APIRouter(prefix="/api/loveracing", tags=["loveracing"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/status")
def status(db: Session = Depends(get_db)):
    return list_import_status(db)


@router.post("/import")
def import_url(
    payload: dict,
    db: Session = Depends(get_db),
    x_admin_pin: str | None = Header(default=None),
):
    source_url = (payload.get("source_url") or payload.get("url") or "").strip()
    if not source_url:
        raise HTTPException(status_code=422, detail="LOVERACING meeting overview URL is required.")
    lowered_url = source_url.lower()
    if "loveracing.nz" not in lowered_url or "meeting-overview" not in lowered_url:
        raise HTTPException(status_code=422, detail="Enter a LOVERACING meeting overview URL.")
    if import_requires_edit(db, source_url, payload.get("source_region")):
        require_edit_pin(x_admin_pin)
    result = import_meeting_url(db, source_url, force=bool(payload.get("force")), source_region=payload.get("source_region"))
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/preview")
def preview_url(payload: dict):
    source_url = (payload.get("source_url") or payload.get("url") or "").strip()
    if not source_url:
        raise HTTPException(status_code=422, detail="LOVERACING meeting overview URL is required.")
    lowered_url = source_url.lower()
    if "loveracing.nz" not in lowered_url or "meeting-overview" not in lowered_url:
        raise HTTPException(status_code=422, detail="Enter a LOVERACING meeting overview URL.")
    return preview_meeting_url(source_url, source_region=payload.get("source_region"))


@router.put("/racecourses")
def save_racecourse(payload: dict, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    try:
        return upsert_racecourse_and_rematch(db, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/automation")
def save_automation(payload: dict, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    return update_automation(db, payload)


@router.post("/discover")
def discover(
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    try:
        return discover_calendar_candidates(db)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Calendar discovery failed: {error.__class__.__name__}: {error}") from error


@router.delete("/meetings/{meeting_id}/imported-races")
def undo_import(meeting_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    try:
        return undo_meeting_import(db, meeting_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
