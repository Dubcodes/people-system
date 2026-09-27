from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import SessionLocal
from schemas import HorseDetail, HorseSummary, HorseUpdate
from services.auth_service import require_edit_pin
from services.horse_service import get_horse_detail, list_horses, update_horse


router = APIRouter(prefix="/api/horses", tags=["horses"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("", response_model=list[HorseSummary])
def horses(
    search: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    return list_horses(db, search, include_inactive=include_inactive)


@router.get("/{horse_id}", response_model=HorseDetail)
def horse_detail(horse_id: int, db: Session = Depends(get_db)):
    horse = get_horse_detail(db, horse_id)
    if not horse:
        raise HTTPException(status_code=404, detail="Horse not found")
    return horse


@router.put("/{horse_id}", response_model=HorseDetail)
def save_horse(horse_id: int, payload: HorseUpdate, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    horse = update_horse(db, horse_id, payload)
    if not horse:
        raise HTTPException(status_code=404, detail="Horse not found")
    return horse
