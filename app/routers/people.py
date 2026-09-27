from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import SessionLocal
from schemas import AbsenceUpdate, ActionResponse, HorseLinkUpdate, ImageUpdate, PersonDetail, PersonMergeRequest, PersonUpdate, TimelineUpdate, ViewerOverview
from services.auth_service import require_edit_pin, require_master_admin_pin
from services.people_service import delete_horse_link, delete_image, delete_person, delete_role_assignment, delete_stable_assignment, get_person_detail, get_suggestions, get_viewer_overview, merge_people, set_absence, update_horse_link, update_image, update_person, update_role_assignment, update_stable_assignment

router = APIRouter(prefix="/api", tags=["people"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/people/overview", response_model=ViewerOverview)
def people_overview(
    search: str | None = Query(default=None),
    race_id: int | None = Query(default=None),
    pre: bool = Query(default=False),
    track: str | None = Query(default=None),
    race_date: date | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    region_scope: str = Query(default="current"),
    db: Session = Depends(get_db),
):
    return get_viewer_overview(
        db,
        search=search,
        race_id=race_id,
        pre_mode=pre,
        track=track,
        race_date=race_date,
        include_inactive=include_inactive,
        region_scope=region_scope,
    )



@router.get("/suggestions")
def suggestions(db: Session = Depends(get_db)):
    return get_suggestions(db)


@router.get("/people/{person_id}", response_model=PersonDetail)
def person_detail(person_id: int, race_date: date | None = Query(default=None), track: str | None = Query(default=None), db: Session = Depends(get_db)):
    person = get_person_detail(db, person_id, race_date=race_date, track=track)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


@router.put("/people/{person_id}", response_model=PersonDetail)
def save_person(person_id: int, payload: PersonUpdate, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    person = update_person(db, person_id, payload)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    return person



@router.delete("/people/{person_id}", response_model=ActionResponse)
def remove_person(person_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    result = delete_person(db, person_id)
    if not result:
        raise HTTPException(status_code=404, detail="Person not found")
    return result


@router.post("/people/merge", response_model=ActionResponse)
def merge_person_records(payload: PersonMergeRequest, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    try:
        result = merge_people(db, payload.source_person_id, payload.target_person_id, payload.note)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not result:
        raise HTTPException(status_code=404, detail="Both people must exist and be active.")
    return result


@router.post("/people/{person_id}/absence", response_model=AbsenceUpdate)
def update_absence(person_id: int, payload: AbsenceUpdate, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    set_absence(db, person_id, payload.is_absent, payload.reason, payload.race_date, payload.track)
    return payload


@router.put("/images/{image_id}", response_model=ActionResponse)
def save_image(image_id: int, payload: ImageUpdate, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    result = update_image(db, image_id, payload)
    if not result:
        raise HTTPException(status_code=404, detail="Image not found")
    return result


@router.delete("/images/{image_id}", response_model=ActionResponse)
def remove_image(image_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    result = delete_image(db, image_id)
    if not result:
        raise HTTPException(status_code=404, detail="Image not found")
    return result


@router.put("/person-roles/{assignment_id}", response_model=ActionResponse)
def save_role_assignment(assignment_id: int, payload: TimelineUpdate, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    result = update_role_assignment(db, assignment_id, payload)
    if not result:
        raise HTTPException(status_code=404, detail="Role assignment not found")
    return result


@router.put("/person-stables/{assignment_id}", response_model=ActionResponse)
def save_stable_assignment(assignment_id: int, payload: TimelineUpdate, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    result = update_stable_assignment(db, assignment_id, payload)
    if not result:
        raise HTTPException(status_code=404, detail="Stable assignment not found")
    return result


@router.put("/horse-links/{link_id}", response_model=ActionResponse)
def save_horse_link(link_id: int, payload: HorseLinkUpdate, db: Session = Depends(get_db), _: None = Depends(require_edit_pin)):
    result = update_horse_link(db, link_id, payload)
    if not result:
        raise HTTPException(status_code=404, detail="Horse link not found")
    return result


@router.delete("/person-roles/{assignment_id}", response_model=ActionResponse)
def remove_role_assignment(assignment_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    result = delete_role_assignment(db, assignment_id)
    if not result:
        raise HTTPException(status_code=404, detail="Role assignment not found")
    return result


@router.delete("/person-stables/{assignment_id}", response_model=ActionResponse)
def remove_stable_assignment(assignment_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    result = delete_stable_assignment(db, assignment_id)
    if not result:
        raise HTTPException(status_code=404, detail="Stable assignment not found")
    return result


@router.delete("/horse-links/{link_id}", response_model=ActionResponse)
def remove_horse_link(link_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    result = delete_horse_link(db, link_id)
    if not result:
        raise HTTPException(status_code=404, detail="Horse link not found")
    return result







