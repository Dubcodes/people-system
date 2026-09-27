from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from database import SessionLocal
from models import Horse, HorsePerson, Person, Race, RaceCourse, RaceEntry, RaceMeeting
from loveracing_importer.name_utils import split_trainer_names as _split_trainer_names
from schemas import ManualAttendeeCreate, ManualAttendeeSummary, RaceImportRequest, RaceImportResponse, RaceParsePreviewResponse
from services.auth_service import require_master_admin_pin
from services.region_service import default_region, normalize_region
from services.race_day_service import races_for_race_day, resolve_current_race_day
from services.race_service import cleanup_old_race_data, create_manual_attendee, delete_manual_attendee, delete_race, delete_race_day, import_race_data, list_manual_attendees, list_race_days, preview_race_data
from services.time_service import local_today

router = APIRouter(prefix="/api/races", tags=["races"])


def _stored_id_list(value: str | None) -> list[int]:
    ids = []
    seen = set()
    for part in (value or "").split(","):
        try:
            numeric = int(part.strip())
        except (TypeError, ValueError):
            continue
        if numeric <= 0 or numeric in seen:
            continue
        seen.add(numeric)
        ids.append(numeric)
    return ids


def _append_trainer_names(names: list[str], raw_name: str | None) -> None:
    for name in _split_trainer_names(raw_name):
        if name and name not in names:
            names.append(name)


def _tagged_course_track_names(db: Session) -> set[str]:
    current_region = default_region()
    names = set()
    for course in db.query(RaceCourse).filter(RaceCourse.active.is_(True), RaceCourse.is_local_tagged.is_(True)).all():
        if normalize_region(course.source_region) and normalize_region(course.source_region) != current_region:
            continue
        for value in (course.name, course.loveracing_name, course.short_code):
            cleaned = " ".join((value or "").split()).strip()
            if cleaned:
                names.add(cleaned.lower())
    return names


def _filter_to_tagged_tracks_if_available(races: list[Race], tagged_track_names: set[str]) -> list[Race]:
    if not tagged_track_names:
        return races
    tagged_races = [race for race in races if race.track and race.track.lower() in tagged_track_names]
    return tagged_races or races


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _current_race_day(db: Session, requested_date: date | None = None) -> date | None:
    return resolve_current_race_day(db, requested_date, default_region(), local_today())


@router.get("/today")
def today_races(track: str | None = Query(default=None), race_date: date | None = Query(default=None), db: Session = Depends(get_db)):
    if race_date is None:
        cleanup_old_race_data(db)
    race_day = _current_race_day(db, race_date)
    is_fallback_day = bool(race_day and race_date is None and race_day != local_today())
    races = []
    if race_day:
        races = (
            races_for_race_day(db, race_day, default_region(), include_inactive=bool(race_date))
            .options(
                joinedload(Race.entries).joinedload(RaceEntry.horse).joinedload(Horse.people).joinedload(HorsePerson.person),
                joinedload(Race.entries).joinedload(RaceEntry.jockey),
                joinedload(Race.entries).joinedload(RaceEntry.trainer),
            )
            .order_by(Race.track.asc(), Race.race_number.asc())
            .all()
        )
        races = _filter_to_tagged_tracks_if_available(races, _tagged_course_track_names(db))
    tracks = sorted({race.track for race in races})
    selected_track = track if track in tracks else (tracks[0] if tracks else None)
    visible_races = [race for race in races if not selected_track or race.track == selected_track]
    extra_trainer_ids = sorted(
        {
            person_id
            for race in visible_races
            for entry in race.entries
            for person_id in _stored_id_list(entry.additional_trainer_ids)
        }
    )
    extra_trainer_people = {}
    if extra_trainer_ids:
        extra_trainer_people = {
            person.id: person
            for person in db.query(Person)
            .filter(Person.id.in_(extra_trainer_ids), Person.deleted_at.is_(None))
            .all()
        }

    race_payloads = []
    for race in visible_races:
        entries = []
        for entry in sorted(race.entries, key=lambda item: (item.horse.name if item.horse else "")):
            trainer_names = []
            if entry.trainer and not entry.trainer.deleted_at:
                _append_trainer_names(trainer_names, entry.trainer.name)
            for person_id in _stored_id_list(entry.additional_trainer_ids):
                person = extra_trainer_people.get(person_id)
                if person:
                    _append_trainer_names(trainer_names, person.name)
            entries.append(
                {
                    "race_name": race.race_name,
                    "start_time": race.start_time,
                    "conditions": race.conditions,
                    "horse_name": entry.horse.name if entry.horse else "Unknown horse",
                    "horse_number": entry.horse_number,
                    "jockey_name": entry.jockey.name if entry.jockey else None,
                    "trainer_name": ", ".join(trainer_names) if trainer_names else None,
                    "status": entry.status,
                    "is_scratched": entry.is_scratched,
                    "is_ballot": entry.is_ballot,
                    "silk_image_url": entry.silk_image_url,
                    "silk_description": entry.silk_description,
                }
            )
        race_payloads.append(
            {
                "id": race.id,
                "track": race.track,
                "race_number": race.race_number,
                "date": race.date.isoformat(),
                "start_time": race.start_time,
                "race_name": race.race_name,
                "conditions": race.conditions,
                "entries": entries,
            }
        )

    meeting = None
    if visible_races and visible_races[0].meeting_id:
        meeting = db.query(RaceMeeting).filter(RaceMeeting.id == visible_races[0].meeting_id).first()

    return {
        "tracks": tracks,
        "selected_track": selected_track,
        "race_date": race_day.isoformat() if race_day else None,
        "is_fallback_day": is_fallback_day,
        "import_status": meeting.import_status if meeting else None,
        "import_warning": meeting.last_error if meeting and meeting.last_error else None,
        "loveracing_last_updated_at": meeting.loveracing_last_updated_at.isoformat() if meeting and meeting.loveracing_last_updated_at else None,
        "last_imported_at": meeting.updated_at.isoformat() if meeting and meeting.updated_at else None,
        "races": race_payloads,
    }

@router.get("/days")
def race_days(db: Session = Depends(get_db)):
    return {"race_days": list_race_days(db)}


@router.get("/silks")
def race_silks(db: Session = Depends(get_db)):
    rows = (
        db.query(RaceEntry, Horse, Race)
        .join(Horse, Horse.id == RaceEntry.horse_id)
        .join(Race, Race.id == RaceEntry.race_id)
        .filter(Race.source_region == default_region(), RaceEntry.silk_image_url.isnot(None))
        .order_by(Race.date.desc(), Race.track.asc(), Race.race_number.asc(), RaceEntry.horse_number.asc())
        .all()
    )
    catalog = {}
    for entry, horse, race in rows:
        key = (entry.silk_image_url, entry.silk_description or "")
        item = catalog.setdefault(
            key,
            {
                "silk_image_url": entry.silk_image_url,
                "silk_description": entry.silk_description,
                "horses": [],
                "last_seen_date": race.date.isoformat() if race.date else None,
            },
        )
        if horse.name not in item["horses"]:
            item["horses"].append(horse.name)
        if race.date and (not item["last_seen_date"] or race.date.isoformat() > item["last_seen_date"]):
            item["last_seen_date"] = race.date.isoformat()
    return {"silks": list(catalog.values())}


@router.get("/manual-attendees", response_model=list[ManualAttendeeSummary])
def manual_attendees(track: str | None = Query(default=None), race_date: date | None = Query(default=None), db: Session = Depends(get_db)):
    return list_manual_attendees(db, track, race_date)


@router.post("/manual-attendees", response_model=ManualAttendeeSummary)
def add_manual_attendee(payload: ManualAttendeeCreate, db: Session = Depends(get_db)):
    try:
        return create_manual_attendee(db, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.delete("/manual-attendees/{attendee_id}")
def remove_manual_attendee(attendee_id: int, db: Session = Depends(get_db)):
    result = delete_manual_attendee(db, attendee_id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="Manual attendee not found")
    return result


@router.post("/cleanup")
def cleanup_races(retention_days: int = Query(default=7, ge=1, le=90), db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    return cleanup_old_race_data(db, retention_days)


@router.delete("/day")
def remove_race_day(track: str = Query(...), race_date: date = Query(...), db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    return delete_race_day(db, track, race_date)


@router.delete("/{race_id}")
def remove_race(race_id: int, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    result = delete_race(db, race_id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="Race not found")
    return result

@router.post("/import", response_model=RaceImportResponse)
def import_race(payload: RaceImportRequest, db: Session = Depends(get_db), _: None = Depends(require_master_admin_pin)):
    try:
        return import_race_data(db, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SQLAlchemyError as error:
        raise HTTPException(status_code=500, detail=f"Database import error: {error.__class__.__name__}: {str(error)}") from error
    except Exception as error:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Unexpected import error: {error.__class__.__name__}: {str(error)}") from error


@router.post("/preview", response_model=RaceParsePreviewResponse)
def preview_race(payload: RaceImportRequest):
    try:
        return preview_race_data(payload.raw_text)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error








