import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from loveracing_importer.discovery import CALENDAR_URL, calendar_discovery_is_complete, discover_calendar_meetings, fetch_calendar_html
from loveracing_importer.fetcher import fetch_meeting_html, meeting_id_from_url
from loveracing_importer.models import FetchResult, ParsedMeeting, ParsedRace, ParsedRunner
from loveracing_importer.name_utils import clean_horse_name as _clean_horse_name, clean_trainer_name as _clean_trainer_name, is_initial_style_name as _is_initial_style_name, split_trainer_names as _split_trainer_names
from loveracing_importer.parser import PARSER_VERSION, parse_meeting_html, parse_race_detail_html
from loveracing_importer.scheduler import coalesce_refreshes, planned_refreshes, respects_minimum_interval
from models import Alias, Horse, HorsePerson, LoveracingImportRun, Person, PersonRole, Race, RaceCourse, RaceEntry, RaceMeeting, Role
from services.audit_service import record_audit_event
from services.region_service import allowed_regions, default_region, normalize_region
from services.system_settings_service import get_setting_value, set_setting_value
from services.time_service import local_now


BACKOFF_STATUSES = {403, 429}
TRANSIENT_STATUSES = {500, 502, 503, 504}
LOCAL_COURSE_SEEDS = ["Cambridge Synthetic", "Te Aroha", "Matamata", "Te Rapa", "Tauranga", "Rotorua", "Ellerslie", "Pukekohe"]
PERSON_PLACEHOLDER_KEYS = {"", "tba", "tobeconfirmed", "tobeadvised", "advised", "nojockey"}
RACING_ROLE_NAMES = {"Jockey", "Trainer"}
AUTO_LOADER_LOCK_ID = 2026050501
INACTIVE_MEETING_STATUSES = {"abandoned", "cancelled", "postponed"}
NO_RACES_FAILURE_MARKER = "no races found in loveracing meeting page"
INACTIVE_CONFIRMATION_FAILURES = 2


@contextmanager
def _auto_loader_lock(db: Session):
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        yield True
        return
    with bind.connect() as connection:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": AUTO_LOADER_LOCK_ID},
            ).scalar()
        )
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": AUTO_LOADER_LOCK_ID},
                )


def _clean(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def _is_no_races_failure(error_message: str | None) -> bool:
    return NO_RACES_FAILURE_MARKER in (error_message or "").lower()


def _calendar_candidate_matches_meeting(candidate: dict, meeting: RaceMeeting) -> bool:
    candidate_id = meeting_id_from_url(candidate.get("source_url") or "")
    return bool(candidate_id and meeting.source_meeting_id and candidate_id == meeting.source_meeting_id)


def _meeting_is_conservatively_abandoned(db: Session, meeting: RaceMeeting, failure_message: str, now: datetime | None = None) -> bool:
    now = now or local_now()
    if not _is_no_races_failure(failure_message) or not meeting.date or meeting.date != now.date() or not meeting.race_count:
        return False
    discovered_at = _parse_local_datetime(get_setting_value(db, "loveracing_last_discovery_success_at"))
    if not discovered_at or discovered_at.date() != now.date():
        return False
    if get_setting_value(db, "loveracing_last_discovery_error"):
        return False
    if any(_calendar_candidate_matches_meeting(candidate, meeting) for candidate in last_calendar_candidates(db)):
        return False
    runs = (
        db.query(LoveracingImportRun)
        .filter(LoveracingImportRun.meeting_id == meeting.id)
        .order_by(LoveracingImportRun.started_at.desc())
        .all()
    )
    has_success = any(run.success for run in runs)
    previous_no_race_failures = sum(not run.success and _is_no_races_failure(run.error_message) for run in runs)
    return has_success and previous_no_race_failures + 1 >= INACTIVE_CONFIRMATION_FAILURES


def _needs_independent_calendar_confirmation(db: Session, meeting: RaceMeeting, failure_message: str) -> bool:
    if not _is_no_races_failure(failure_message) or not meeting.date or meeting.date != local_now().date() or not meeting.race_count:
        return False
    runs = db.query(LoveracingImportRun).filter(LoveracingImportRun.meeting_id == meeting.id).all()
    return any(not run.success and _is_no_races_failure(run.error_message) for run in runs)


def _set_parse_failure_status(db: Session, meeting: RaceMeeting, failure_message: str) -> str:
    if _needs_independent_calendar_confirmation(db, meeting, failure_message):
        try:
            discover_calendar_candidates(db)
        except Exception:
            # A failed confirmation attempt is never evidence that a meeting disappeared.
            pass
    if _meeting_is_conservatively_abandoned(db, meeting, failure_message):
        meeting.import_status = "abandoned"
        meeting.last_error = "Confirmed inactive meeting: repeated empty meeting pages after current calendar discovery no longer listed this meeting."
        meeting.next_scheduled_refresh_at = None
        meeting.backoff_until = None
    else:
        meeting.import_status = "parse_failed"
        meeting.last_error = failure_message
    meeting.updated_at = datetime.utcnow()
    return failure_message


def _match_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _serialize_id_list(values: list[int]) -> str | None:
    cleaned = []
    seen = set()
    for value in values:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if numeric <= 0 or numeric in seen:
            continue
        seen.add(numeric)
        cleaned.append(str(numeric))
    return ",".join(cleaned) if cleaned else None


def _stored_id_list(value: str | None) -> list[int]:
    ids = []
    seen = set()
    for part in (value or "").split(","):
        try:
            person_id = int(part.strip())
        except (TypeError, ValueError):
            continue
        if person_id > 0 and person_id not in seen:
            seen.add(person_id)
            ids.append(person_id)
    return ids


def _source_hash(value: object) -> str:
    import hashlib

    payload = json.dumps(value, default=str, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_odds(value: str | None) -> str | None:
    cleaned = _clean(value)
    if not cleaned or cleaned in {"-", "$"}:
        return None
    if len(cleaned) > 20:
        return None
    if re.fullmatch(r"\$?\d+(?:\.\d{1,2})?", cleaned):
        return cleaned
    return None


def _role(db: Session, name: str) -> Role:
    role = db.query(Role).filter(Role.name == name).first()
    if not role:
        role = Role(name=name)
        db.add(role)
        db.flush()
    return role


def _ensure_alias(db: Session, person: Person) -> None:
    alias_value = " ".join(re.sub(r"[^a-z0-9\s]", " ", person.name.lower()).split())
    if not alias_value:
        return
    if any(alias.alias == alias_value for alias in person.aliases):
        return
    if not db.query(Alias).filter(Alias.person_id == person.id, Alias.alias == alias_value).first():
        db.add(Alias(person_id=person.id, alias=alias_value))


def _has_conflicting_racing_role(person: Person, role_name: str) -> bool:
    if role_name not in RACING_ROLE_NAMES:
        return False
    active_racing_roles = {
        assignment.role.name
        for assignment in person.role_assignments
        if assignment.end_date is None and assignment.role and assignment.role.name in RACING_ROLE_NAMES
    }
    return bool(active_racing_roles - {role_name})


def _ensure_person(db: Session, name: str | None, role_name: str) -> Person | None:
    clean_name = _clean(name)
    if _match_key(clean_name) in PERSON_PLACEHOLDER_KEYS:
        return None
    person = db.query(Person).filter(Person.name == clean_name).first()
    if not person:
        person = next((item for item in db.query(Person).all() if _match_key(item.name) == _match_key(clean_name)), None)
    if not person:
        person = Person(name=clean_name, source_region=default_region())
        db.add(person)
        db.flush()
    elif person.deleted_at:
        person.deleted_at = None
        person.deleted_by_instance = None
        person.delete_reason = None
    if not person.source_region:
        person.source_region = default_region()
    if role_name == "Trainer" and _is_initial_style_name(clean_name):
        review_note = "Needs trainer name review: imported from LOVERACING initials-style trainer text."
        if review_note not in (person.notes or ""):
            person.notes = f"{person.notes}\n\n{review_note}".strip() if person.notes else review_note
    role = _role(db, role_name)
    has_role = any(item.role_id == role.id and item.end_date is None for item in person.role_assignments)
    if not has_role:
        with db.no_autoflush:
            has_role = (
                db.query(PersonRole)
                .filter(PersonRole.person_id == person.id, PersonRole.role_id == role.id, PersonRole.end_date.is_(None))
                .first()
                is not None
            )
    if not has_role and not _has_conflicting_racing_role(person, role_name):
        person.role_assignments.append(PersonRole(role=role))
    _ensure_alias(db, person)
    return person


def _ensure_people(db: Session, names: list[str], role_name: str) -> list[Person]:
    people = []
    seen = set()
    for name in names:
        person = _ensure_person(db, name, role_name)
        if person and person.id not in seen:
            people.append(person)
            seen.add(person.id)
    return people


def _ensure_horse(db: Session, name: str | None) -> Horse | None:
    clean_name = _clean_horse_name(name)
    if not clean_name:
        return None
    horse = db.query(Horse).filter(Horse.name == clean_name).first()
    if not horse:
        clean_key = _match_key(clean_name)
        horse = next((item for item in db.query(Horse).all() if _match_key(_clean_horse_name(item.name)) == clean_key), None)
    if horse and horse.name != clean_name:
        duplicate = db.query(Horse).filter(Horse.name == clean_name, Horse.id != horse.id).first()
        if duplicate:
            horse = duplicate
        else:
            horse.name = clean_name
    if not horse:
        horse = Horse(name=clean_name)
        db.add(horse)
        db.flush()
    return horse


def _cleanup_existing_horse_name_suffixes(db: Session) -> dict[str, int]:
    renamed = 0
    merged = 0
    for horse in db.query(Horse).order_by(Horse.id.asc()).all():
        clean_name = _clean_horse_name(horse.name)
        if not clean_name or clean_name == horse.name:
            continue
        duplicate = db.query(Horse).filter(Horse.name == clean_name, Horse.id != horse.id).first()
        if duplicate:
            if not duplicate.notes and horse.notes:
                duplicate.notes = horse.notes
            if not duplicate.stable_id and horse.stable_id:
                duplicate.stable_id = horse.stable_id
            db.query(RaceEntry).filter(RaceEntry.horse_id == horse.id).update({RaceEntry.horse_id: duplicate.id}, synchronize_session=False)
            db.query(HorsePerson).filter(HorsePerson.horse_id == horse.id).update({HorsePerson.horse_id: duplicate.id}, synchronize_session=False)
            db.delete(horse)
            merged += 1
        else:
            horse.name = clean_name
            renamed += 1
    if renamed or merged:
        db.flush()
    return {"renamed": renamed, "merged": merged}


def _cleanup_existing_trainer_location_suffixes(db: Session) -> dict[str, int]:
    def stored_ids(value: str | None) -> list[int]:
        ids = []
        for part in (value or "").split(","):
            try:
                ids.append(int(part.strip()))
            except (TypeError, ValueError):
                continue
        return ids

    updated_entries = 0
    hidden_people = 0
    trainer_role = db.query(Role).filter(Role.name == "Trainer").first()
    if not trainer_role:
        return {"updated_entries": 0, "hidden_people": 0}
    for person in db.query(Person).filter(Person.deleted_at.is_(None)).order_by(Person.id.asc()).all():
        if "," not in person.name:
            continue
        has_trainer_role = any(item.role_id == trainer_role.id and item.end_date is None for item in person.role_assignments)
        if not has_trainer_role:
            continue
        clean_name = _clean_trainer_name(person.name)
        if not clean_name or clean_name == person.name:
            continue
        target = _ensure_person(db, clean_name, "Trainer")
        if not target or target.id == person.id:
            continue
        if person.notes and person.notes not in (target.notes or ""):
            target.notes = f"{target.notes}\n\nMerged note from {person.name}: {person.notes}".strip() if target.notes else person.notes
        for entry in db.query(RaceEntry).all():
            existing_additional_ids = stored_ids(entry.additional_trainer_ids)
            touched = False
            if entry.trainer_id == person.id:
                entry.trainer_id = target.id
                touched = True
            if person.id in existing_additional_ids:
                existing_additional_ids = [item_id for item_id in existing_additional_ids if item_id != person.id]
                if entry.trainer_id is None:
                    entry.trainer_id = target.id
                elif entry.trainer_id != target.id:
                    existing_additional_ids.append(target.id)
                touched = True
            if touched:
                entry.additional_trainer_ids = _serialize_id_list(
                    [item_id for item_id in existing_additional_ids if item_id != entry.trainer_id]
                )
                updated_entries += 1
        active_non_trainer_roles = [
            assignment
            for assignment in person.role_assignments
            if assignment.end_date is None and assignment.role_id != trainer_role.id
        ]
        for assignment in list(person.role_assignments):
            if assignment.role_id == trainer_role.id and assignment.end_date is None:
                db.delete(assignment)
        has_active_images = any(not image.deleted_at for image in person.images)
        has_manual_context = bool(person.manual_attendees or person.horse_links)
        if not active_non_trainer_roles and not has_active_images and not has_manual_context:
            person.deleted_at = datetime.utcnow()
            person.deleted_by_instance = "loveracing_importer"
            person.delete_reason = "Trainer location suffix removed from imported LOVERACING name."
            hidden_people += 1
    if updated_entries or hidden_people:
        db.flush()
    return {"updated_entries": updated_entries, "hidden_people": hidden_people}


def _cleanup_existing_trainer_partnerships(db: Session) -> dict[str, int]:
    def stored_ids(value: str | None) -> list[int]:
        ids = []
        for part in (value or "").split(","):
            try:
                ids.append(int(part.strip()))
            except (TypeError, ValueError):
                continue
        return ids

    updated_entries = 0
    hidden_people = 0
    trainer_role = db.query(Role).filter(Role.name == "Trainer").first()
    for person in db.query(Person).filter(Person.deleted_at.is_(None)).order_by(Person.id.asc()).all():
        if not re.search(r"\s+(?:&|and)\s+", person.name, re.I):
            continue
        split_names = _split_trainer_names(person.name)
        if len(split_names) <= 1:
            continue
        trainer_people = _ensure_people(db, split_names, "Trainer")
        if not trainer_people:
            continue
        primary = trainer_people[0]
        additional_ids = [item.id for item in trainer_people[1:] if item.id != primary.id]
        for entry in db.query(RaceEntry).all():
            existing_additional_ids = stored_ids(entry.additional_trainer_ids)
            touched = False
            if entry.trainer_id == person.id:
                entry.trainer_id = primary.id
                touched = True
            if person.id in existing_additional_ids:
                existing_additional_ids = [item_id for item_id in existing_additional_ids if item_id != person.id]
                if entry.trainer_id is None:
                    entry.trainer_id = primary.id
                elif entry.trainer_id != primary.id:
                    existing_additional_ids.append(primary.id)
                touched = True
            if touched:
                entry.additional_trainer_ids = _serialize_id_list(
                    [item_id for item_id in existing_additional_ids + additional_ids if item_id != entry.trainer_id]
                )
                updated_entries += 1
        active_non_trainer_roles = [
            assignment
            for assignment in person.role_assignments
            if assignment.end_date is None and (not trainer_role or assignment.role_id != trainer_role.id)
        ]
        if trainer_role:
            for assignment in list(person.role_assignments):
                if assignment.role_id == trainer_role.id and assignment.end_date is None:
                    db.delete(assignment)
        _ensure_alias(db, person)
        has_active_images = any(not image.deleted_at for image in person.images)
        has_manual_context = bool(person.manual_attendees or person.horse_links)
        if not active_non_trainer_roles and not has_active_images and not has_manual_context and not person.notes:
            person.deleted_at = datetime.utcnow()
            person.deleted_by_instance = "loveracing_importer"
            person.delete_reason = "Trainer partnership split into individual trainer records."
            hidden_people += 1
    if updated_entries or hidden_people:
        db.flush()
    return {"updated_entries": updated_entries, "hidden_people": hidden_people}


def _evidence_backed_racing_role(jockey_entries: int, trainer_entries: int) -> str | None:
    if jockey_entries > 0 and trainer_entries == 0:
        return "Jockey"
    if trainer_entries > 0 and jockey_entries == 0:
        return "Trainer"
    return None


def _cleanup_conflicting_racing_roles(db: Session) -> dict[str, object]:
    role_by_name = {
        role.name: role
        for role in db.query(Role).filter(Role.name.in_(tuple(sorted(RACING_ROLE_NAMES)))).all()
    }
    if set(role_by_name) != RACING_ROLE_NAMES:
        return {"fixed": 0, "people": []}

    usage: dict[int, dict[str, int]] = {}

    def add_usage(person_id: int | None, role_name: str) -> None:
        if not person_id:
            return
        counts = usage.setdefault(person_id, {"Jockey": 0, "Trainer": 0})
        counts[role_name] += 1

    for entry in db.query(RaceEntry).all():
        add_usage(entry.jockey_id, "Jockey")
        add_usage(entry.trainer_id, "Trainer")
        for person_id in _stored_id_list(entry.additional_trainer_ids):
            add_usage(person_id, "Trainer")

    fixed_people = []
    for person in db.query(Person).filter(Person.deleted_at.is_(None)).all():
        active_racing_assignments = [
            assignment
            for assignment in person.role_assignments
            if assignment.end_date is None
            and assignment.role
            and assignment.role.name in RACING_ROLE_NAMES
        ]
        active_names = {assignment.role.name for assignment in active_racing_assignments}
        if active_names != RACING_ROLE_NAMES:
            continue
        counts = usage.get(person.id, {"Jockey": 0, "Trainer": 0})
        correct_role = _evidence_backed_racing_role(counts["Jockey"], counts["Trainer"])
        if not correct_role:
            continue
        incorrect_role = next(iter(RACING_ROLE_NAMES - {correct_role}))
        for assignment in active_racing_assignments:
            if assignment.role.name != incorrect_role:
                continue
            record_audit_event(
                db,
                action="repair_imported_role",
                entity_type="person_role",
                entity_id=assignment.id,
                before={"person_id": person.id, "person_name": person.name, "role": incorrect_role},
                after={"person_id": person.id, "person_name": person.name, "role": correct_role},
                notes="Removed a conflicting imported racing role with no race-entry evidence.",
                actor="loveracing_importer",
            )
            db.delete(assignment)
        fixed_people.append(person.name)
    if fixed_people:
        db.flush()
    return {"fixed": len(fixed_people), "people": fixed_people}


def cleanup_loveracing_import_artifacts(db: Session) -> dict[str, int]:
    horse_cleanup = _cleanup_existing_horse_name_suffixes(db)
    trainer_location_cleanup = _cleanup_existing_trainer_location_suffixes(db)
    trainer_partnership_cleanup = _cleanup_existing_trainer_partnerships(db)
    role_cleanup = _cleanup_conflicting_racing_roles(db)
    return {
        "horse_suffix_renamed": horse_cleanup["renamed"],
        "horse_suffix_merged": horse_cleanup["merged"],
        "trainer_location_entries": trainer_location_cleanup["updated_entries"],
        "trainer_location_hidden": trainer_location_cleanup["hidden_people"],
        "trainer_partnership_entries": trainer_partnership_cleanup["updated_entries"],
        "trainer_partnership_hidden": trainer_partnership_cleanup["hidden_people"],
        "conflicting_roles_fixed": role_cleanup["fixed"],
    }


def _warn_initial_style_trainers(parsed: ParsedMeeting) -> None:
    flagged = []
    seen = set()
    for race in parsed.races:
        for runner in race.runners:
            for name in _split_trainer_names(runner.trainer_name):
                if _is_initial_style_name(name) and name not in seen:
                    seen.add(name)
                    flagged.append(name)
    if flagged:
        warning = (
            "Trainer names need review: "
            + ", ".join(flagged[:10])
            + ("." if len(flagged) <= 10 else f", and {len(flagged) - 10} more.")
        )
        if warning not in parsed.warnings:
            parsed.warnings.append(warning)


def seed_local_racecourses(db: Session) -> None:
    for course in LOCAL_COURSE_SEEDS:
        if not db.query(RaceCourse).filter(RaceCourse.name == course).first():
            db.add(RaceCourse(name=course, loveracing_name=course, source_region=None, is_local_tagged=True, active=True))
    db.flush()


def _stable_region_from_evidence(values) -> str | None:
    regions = {normalized for value in values if (normalized := normalize_region(value))}
    return next(iter(regions)) if len(regions) == 1 else None


def _racecourse_region_remediation(db: Session, course: RaceCourse) -> dict:
    evidence = [
        race.source_region
        for race in db.query(Race).all()
        if race.source_region and _course_matches(course, race.track)
    ]
    normalized_evidence = sorted({region for value in evidence if (region := normalize_region(value))})
    return {
        "suggested_region": _stable_region_from_evidence(evidence),
        "evidence_regions": normalized_evidence,
        "evidence_count": len(evidence),
    }


def _racecourse_status_payload(item: RaceCourse, remediation: dict | None = None) -> dict:
    source_region = normalize_region(item.source_region)
    return {
        "id": item.id,
        "name": item.name,
        "loveracing_name": item.loveracing_name,
        "short_code": item.short_code,
        "source_region": source_region,
        "region_state": "assigned" if source_region else "unassigned",
        "is_local_tagged": item.is_local_tagged,
        "active": item.active,
        **(remediation or {
            "suggested_region": None,
            "evidence_regions": [source_region] if source_region else [],
            "evidence_count": 0,
        }),
    }


def list_import_status(db: Session) -> dict:
    courses = db.query(RaceCourse).order_by(RaceCourse.name.asc()).all()
    meetings = db.query(RaceMeeting).order_by(RaceMeeting.updated_at.desc()).limit(20).all()
    runs = db.query(LoveracingImportRun).order_by(LoveracingImportRun.started_at.desc()).limit(20).all()
    return {
        "racecourses": [
            _racecourse_status_payload(
                item,
                _racecourse_region_remediation(db, item) if not normalize_region(item.source_region) else None,
            )
            for item in courses
        ],
        "automation": automation_status(db),
        "allowed_regions": allowed_regions(),
        "calendar_candidates": last_calendar_candidates(db),
        "meetings": [_meeting_payload(item) for item in meetings],
        "recent_runs": [_run_payload(item) for item in runs],
    }


def upsert_racecourse(db: Session, payload: dict) -> dict:
    name = _clean(payload.get("name"))
    if not name:
        raise ValueError("Course name is required.")
    row_id = payload.get("id")
    row = db.query(RaceCourse).filter(RaceCourse.id == row_id).first() if row_id else db.query(RaceCourse).filter(RaceCourse.name == name).first()
    if not row:
        row = RaceCourse(name=name)
        db.add(row)
    row.name = name
    row.loveracing_name = _clean(payload.get("loveracing_name")) or name
    row.short_code = _clean(payload.get("short_code")) or None
    row.source_region = normalize_region(payload.get("source_region"))
    row.is_local_tagged = bool(payload.get("is_local_tagged"))
    row.active = payload.get("active") is not False
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "racecourse": {"id": row.id, "name": row.name}}


def upsert_racecourse_and_rematch(db: Session, payload: dict) -> dict:
    result = upsert_racecourse(db, payload)
    try:
        result["rematch"] = discover_calendar_candidates(db)
    except Exception as error:
        result["rematch"] = {
            "status": "error",
            "error": f"Course saved, but discovery-only rematch failed: {error.__class__.__name__}: {error}",
        }
    return result


def automation_status(db: Session) -> dict:
    legacy_enabled = get_setting_value(db, "loveracing_auto_load_enabled") == "true"
    check_enabled = get_setting_value(db, "loveracing_auto_check_enabled")
    import_enabled = get_setting_value(db, "loveracing_auto_import_enabled")
    last_discovery = get_setting_value(db, "loveracing_last_discovery_date")
    last_discovery_at = get_setting_value(db, "loveracing_last_discovery_at")
    last_auto_run_date = get_setting_value(db, "loveracing_last_auto_run_date")
    last_auto_attempt_at = get_setting_value(db, "loveracing_last_auto_attempt_at")
    last_auto_success_at = get_setting_value(db, "loveracing_last_auto_success_at")
    last_error = get_setting_value(db, "loveracing_last_discovery_error")
    return {
        "enabled": check_enabled == "true" or (check_enabled is None and legacy_enabled),
        "check_enabled": check_enabled == "true" or (check_enabled is None and legacy_enabled),
        "import_enabled": import_enabled == "true" or (import_enabled is None and legacy_enabled),
        "calendar_url": CALENDAR_URL,
        "daily_check_time": "05:00",
        "last_discovery_date": last_discovery,
        "last_discovery_at": last_discovery_at,
        "last_auto_run_date": last_auto_run_date,
        "last_auto_attempt_at": last_auto_attempt_at,
        "last_auto_success_at": last_auto_success_at,
        "last_error": last_error,
        "note": "Runs at/after 5:00 AM when the app process is active; manual discovery can be run any time.",
    }


def _parse_local_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_now().tzinfo)
    return parsed


def _utc_naive_to_local(value: datetime | None) -> datetime | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(local_now().tzinfo)


def _local_to_db_datetime(value: datetime | None) -> datetime | None:
    if not value:
        return None
    return value.astimezone(local_now().tzinfo).replace(tzinfo=None) if value.tzinfo else value


def _parse_race_start_datetime(race: Race) -> datetime | None:
    if not race.date or not race.start_time:
        return None
    cleaned = _clean(race.start_time).lower().replace(".", "")
    cleaned = re.sub(r"\s*([ap])m$", r" \1m", cleaned)
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            clock = datetime.strptime(cleaned.upper(), fmt).time()
            return datetime.combine(race.date, clock, tzinfo=local_now().tzinfo)
        except ValueError:
            continue
    return None


def _last_successful_import_at(db: Session, meeting_id: int) -> datetime | None:
    run = (
        db.query(LoveracingImportRun)
        .filter(LoveracingImportRun.meeting_id == meeting_id, LoveracingImportRun.success.is_(True))
        .order_by(LoveracingImportRun.started_at.desc())
        .first()
    )
    return _utc_naive_to_local(run.started_at) if run else None


def _last_import_attempt_at(db: Session, meeting_id: int) -> datetime | None:
    run = (
        db.query(LoveracingImportRun)
        .filter(LoveracingImportRun.meeting_id == meeting_id)
        .order_by(LoveracingImportRun.started_at.desc())
        .first()
    )
    return _utc_naive_to_local(run.started_at) if run else None


def _meeting_source_region(meeting: RaceMeeting) -> str | None:
    return _stable_region_from_evidence(race.source_region for race in meeting.races)


def update_automation(db: Session, payload: dict) -> dict:
    check_enabled = payload.get("check_enabled", payload.get("enabled"))
    import_enabled = payload.get("import_enabled", False)
    set_setting_value(db, "loveracing_auto_load_enabled", "true" if check_enabled else "false")
    set_setting_value(db, "loveracing_auto_check_enabled", "true" if check_enabled else "false")
    set_setting_value(db, "loveracing_auto_import_enabled", "true" if import_enabled else "false")
    db.commit()
    return {"status": "ok", "automation": automation_status(db)}


def _course_matches(course: RaceCourse, text: str) -> bool:
    haystack = _match_key(text)
    names = [course.name, course.loveracing_name, course.short_code]
    return any(_match_key(name) and _match_key(name) in haystack for name in names)


def _inspect_discovered_meeting(meeting) -> tuple[ParsedMeeting | None, list[str]]:
    warnings = []
    fetch = fetch_meeting_html(meeting.source_url)
    if not fetch.success:
        warnings.append(f"Could not inspect {meeting.label}: HTTP {fetch.status or 'error'}")
        return None, warnings
    try:
        return parse_meeting_html(fetch.html or "", meeting.source_url), warnings
    except Exception as error:
        warnings.append(f"Could not parse meeting header for {meeting.label}: {error.__class__.__name__}: {error}")
        return None, warnings


def _match_discovered_course(meeting, courses: list[RaceCourse], parsed: ParsedMeeting | None = None) -> tuple[RaceCourse | None, list[str]]:
    warnings = []
    if parsed:
        text = f"{parsed.meeting_name or ''} {parsed.venue_name or ''} {parsed.club_name or ''}"
        return next((item for item in courses if _course_matches(item, text)), None), warnings
    parsed, warnings = _inspect_discovered_meeting(meeting)
    if parsed:
        text = f"{parsed.meeting_name or ''} {parsed.venue_name or ''} {parsed.club_name or ''}"
        return next((item for item in courses if _course_matches(item, text)), None), warnings
    return next((item for item in courses if _course_matches(item, meeting.label)), None), warnings


def _parsed_preview_payload(parsed: ParsedMeeting, region: str | None = None, status: str = "ready", error: str | None = None) -> dict:
    if parsed.stage == "nominations" and status == "ready":
        status = "nominations"
    return {
        "status": status,
        "stage": parsed.stage,
        "importable": parsed.stage in {"fields", "results"},
        "source": parsed.source,
        "source_url": parsed.source_url,
        "source_meeting_id": parsed.source_meeting_id,
        "date": parsed.date.isoformat() if parsed.date else None,
        "venue_name": parsed.venue_name,
        "club_name": parsed.club_name,
        "meeting_name": parsed.meeting_name,
        "last_updated": parsed.last_updated.isoformat() if parsed.last_updated else None,
        "going": parsed.going,
        "weather": parsed.weather,
        "rail": parsed.rail,
        "region": normalize_region(region) or region,
        "race_count": len(parsed.races),
        "runner_count": sum(len(race.runners) for race in parsed.races),
        "warnings": parsed.warnings,
        "error": error,
        "races": [
            {
                "race_number": race.race_number,
                "start_time": race.start_time,
                "race_name": race.race_name,
                "conditions": race.conditions,
                "distance_m": race.distance_m,
                "stake_text": race.stake_text,
                "status": race.status,
                "runner_count": len(race.runners),
                "scratched_horse_names": race.scratched_horse_names,
                "runners": [
                    {
                        "runner_number": runner.runner_number,
                        "horse_name": runner.horse_name,
                        "form": runner.form,
                        "barrier": runner.barrier,
                        "rating": runner.rating,
                        "weight": runner.weight,
                        "jockey_name": runner.jockey_name,
                        "jockey_claim_text": runner.jockey_claim_text,
                        "trainer_name": runner.trainer_name,
                        "status": runner.status,
                        "is_scratched": runner.is_scratched,
                        "is_ballot": runner.is_ballot,
                        "silk_image_url": runner.silk_image_url,
                        "silk_description": runner.silk_description,
                    }
                    for runner in race.runners
                ],
            }
            for race in parsed.races
        ],
    }


def preview_meeting_url(source_url: str, source_region: str | None = None) -> dict:
    fetch = fetch_meeting_html(source_url)
    if not fetch.success:
        return {
            "status": "failed",
            "source_url": source_url,
            "http_status": fetch.status,
            "error": fetch.error_message or f"HTTP {fetch.status}",
        }
    try:
        parsed = parse_meeting_html(fetch.html or "", source_url)
        _augment_with_detail_pages(parsed)
        _warn_initial_style_trainers(parsed)
        return _parsed_preview_payload(parsed, region=source_region, status="ready")
    except Exception as error:
        return {
            "status": "problem",
            "source_url": source_url,
            "http_status": fetch.status,
            "raw_html_path": fetch.raw_html_path,
            "error": f"{error.__class__.__name__}: {error}",
        }


def _existing_meeting_payload(db: Session, source_url: str) -> dict | None:
    source_meeting_id = meeting_id_from_url(source_url)
    query = db.query(RaceMeeting).filter(RaceMeeting.source == "loveracing")
    meeting = query.filter(RaceMeeting.source_meeting_id == source_meeting_id).first() if source_meeting_id else None
    if not meeting:
        meeting = query.filter(RaceMeeting.source_url == source_url).first()
    return _meeting_payload(meeting) if meeting else None


def _calendar_candidate(db: Session, meeting, courses: list[RaceCourse]) -> dict:
    parsed, inspect_warnings = _inspect_discovered_meeting(meeting)
    course, match_warnings = _match_discovered_course(meeting, courses, parsed)
    source_region = normalize_region(course.source_region) if course else None
    is_assigned = bool(course and source_region)
    payload = {
        "label": meeting.label,
        "source_url": meeting.source_url,
        "matched_course": course.name if course else None,
        "matched_course_id": course.id if course else None,
        "region": source_region,
        "is_matched": bool(course),
        "is_assigned": is_assigned,
        "is_tagged": is_assigned,
        "status": "unassigned" if course and not is_assigned else "unmatched",
        "warnings": inspect_warnings + match_warnings,
        "existing_meeting": _existing_meeting_payload(db, meeting.source_url),
    }
    if parsed:
        payload.update(
            {
                "date": parsed.date.isoformat() if parsed.date else None,
                "venue_name": parsed.venue_name,
                "club_name": parsed.club_name,
                "meeting_name": parsed.meeting_name or meeting.label,
                "race_count": len(parsed.races),
                "runner_count": sum(len(race.runners) for race in parsed.races),
                "loveracing_last_updated_at": parsed.last_updated.isoformat() if parsed.last_updated else None,
                "warnings": (payload["warnings"] or []) + (parsed.warnings or []),
            }
        )
    if not course:
        payload["message"] = "No tagged course match"
        return payload
    if not is_assigned:
        payload["message"] = "Matched course is UNASSIGNED; select and save a region before import."
        return payload
    if parsed:
        try:
            _augment_with_detail_pages(parsed)
            _warn_initial_style_trainers(parsed)
            preview = _parsed_preview_payload(parsed, region=source_region, status="ready")
        except Exception as error:
            preview = {
                "status": "problem",
                "source_url": meeting.source_url,
                "error": f"{error.__class__.__name__}: {error}",
            }
    else:
        preview = preview_meeting_url(meeting.source_url, source_region=source_region)
    payload.update(
        {
            "status": preview.get("status", "problem"),
            "date": preview.get("date") or payload.get("date"),
            "venue_name": preview.get("venue_name") or payload.get("venue_name"),
            "club_name": preview.get("club_name") or payload.get("club_name"),
            "meeting_name": preview.get("meeting_name") or payload.get("meeting_name") or meeting.label,
            "race_count": preview.get("race_count") or payload.get("race_count"),
            "runner_count": preview.get("runner_count") or payload.get("runner_count"),
            "loveracing_last_updated_at": preview.get("last_updated") or payload.get("loveracing_last_updated_at"),
            "warnings": (payload["warnings"] or []) + (preview.get("warnings") or []),
            "error": preview.get("error"),
        }
    )
    return payload


def last_calendar_candidates(db: Session) -> list[dict]:
    raw = get_setting_value(db, "loveracing_last_calendar_candidates_json")
    if not raw:
        return []
    try:
        candidates = json.loads(raw)
    except Exception:
        return []
    today = local_now().date()
    next_week = today + timedelta(days=7)
    return [
        item
        for item in candidates
        if not item.get("date") or today.isoformat() <= item.get("date") <= next_week.isoformat()
    ]


def import_requires_edit(db: Session, source_url: str, source_region: str | None) -> bool:
    requested_region = normalize_region(source_region)
    candidate = next((item for item in last_calendar_candidates(db) if item.get("source_url") == source_url), None)
    if not candidate or not candidate.get("is_assigned"):
        return True
    assigned_region = normalize_region(candidate.get("region"))
    return not assigned_region or (requested_region is not None and requested_region != assigned_region)


def discover_calendar_candidates(db: Session) -> dict:
    courses = db.query(RaceCourse).filter(RaceCourse.active.is_(True), RaceCourse.is_local_tagged.is_(True)).all()
    html = fetch_calendar_html()
    discovered = discover_calendar_meetings(html)
    if not calendar_discovery_is_complete(html, discovered):
        raise ValueError("LOVERACING calendar discovery was incomplete; retained the previous successful calendar evidence.")
    today = local_now().date()
    next_week = today + timedelta(days=7)
    candidates = []
    for meeting in discovered:
        candidate = _calendar_candidate(db, meeting, courses)
        candidate_date = candidate.get("date")
        if candidate_date and not (today.isoformat() <= candidate_date <= next_week.isoformat()):
            continue
        candidates.append(candidate)
    now = local_now()
    set_setting_value(db, "loveracing_last_discovery_date", now.date().isoformat())
    set_setting_value(db, "loveracing_last_discovery_at", now.isoformat())
    set_setting_value(db, "loveracing_last_discovery_success_at", now.isoformat())
    set_setting_value(db, "loveracing_last_discovery_error", None)
    set_setting_value(db, "loveracing_last_calendar_candidates_json", json.dumps(candidates, default=str))
    db.commit()
    return {
        "status": "ok",
        "calendar_url": CALENDAR_URL,
        "discovered_count": len(discovered),
        "candidate_count": len(candidates),
        "matched_count": len([item for item in candidates if item.get("is_tagged")]),
        "candidates": candidates,
    }


def discover_and_import(db: Session, force: bool = False, auto_import: bool = True) -> dict:
    discovery = discover_calendar_candidates(db)
    if not auto_import:
        return {**discovery, "imports": []}
    matched = []
    skipped = []
    imported = []
    for candidate in discovery["candidates"]:
        if not candidate.get("is_tagged"):
            skipped.append(
                {
                    "label": candidate.get("label"),
                    "source_url": candidate.get("source_url"),
                    "reason": "No tagged course match",
                    "warnings": candidate.get("warnings") or [],
                }
            )
            continue
        if candidate.get("status") != "ready":
            skipped.append(
                {
                    "label": candidate.get("meeting_name") or candidate.get("label"),
                    "source_url": candidate.get("source_url"),
                    "reason": candidate.get("error") or f"Preview status was {candidate.get('status')}",
                    "warnings": candidate.get("warnings") or [],
                }
            )
            continue
        matched.append(
            {
                "label": candidate.get("meeting_name") or candidate.get("label"),
                "source_url": candidate.get("source_url"),
                "course": candidate.get("matched_course"),
                "source_region": candidate.get("region"),
            }
        )
        result = import_meeting_url(db, candidate["source_url"], force=force, source_region=candidate.get("region"))
        imported.append(result)
    return {
        **discovery,
        "matched_count": len(matched),
        "matched": matched,
        "skipped": skipped[:20],
        "imports": imported,
    }


def maybe_run_due_auto_load(db: Session) -> None:
    with _auto_loader_lock(db) as acquired:
        if not acquired:
            return
        _run_due_auto_load(db)


def _run_due_auto_load(db: Session) -> None:
    status = automation_status(db)
    if not status["check_enabled"]:
        return
    now = local_now()
    if now.hour < 5:
        return
    today_key = now.date().isoformat()
    if get_setting_value(db, "loveracing_last_auto_run_date") != today_key:
        last_attempt = _parse_local_datetime(get_setting_value(db, "loveracing_last_auto_attempt_at"))
        if not last_attempt or now - last_attempt >= timedelta(hours=1):
            set_setting_value(db, "loveracing_last_auto_attempt_at", now.isoformat())
            db.commit()
            try:
                discover_and_import(db, auto_import=status["import_enabled"])
                set_setting_value(db, "loveracing_last_auto_run_date", today_key)
                set_setting_value(db, "loveracing_last_auto_success_at", now.isoformat())
                set_setting_value(db, "loveracing_last_discovery_error", None)
                db.commit()
            except Exception as error:
                set_setting_value(db, "loveracing_last_discovery_at", now.isoformat())
                set_setting_value(db, "loveracing_last_discovery_error", f"{error.__class__.__name__}: {error}")
                db.commit()
    if status["import_enabled"]:
        _refresh_due_imported_meetings(db, now)


def _refresh_due_imported_meetings(db: Session, now: datetime) -> None:
    today = now.date()
    meetings = (
        db.query(RaceMeeting)
        .filter(
            RaceMeeting.source == "loveracing",
            RaceMeeting.date == today,
            RaceMeeting.source_url.isnot(None),
        )
        .all()
    )
    due_requests = []
    meeting_rows = {}
    for meeting in meetings:
        if meeting.import_status == "undone" or meeting.import_status in INACTIVE_MEETING_STATUSES:
            continue
        if meeting.backoff_until and meeting.backoff_until > datetime.utcnow():
            continue
        race_times = [value for value in (_parse_race_start_datetime(race) for race in meeting.races) if value]
        if not race_times:
            meeting.next_scheduled_refresh_at = None
            continue
        last_success = _last_successful_import_at(db, meeting.id)
        schedule = planned_refreshes(meeting.id, race_times)
        next_due = next((request.due_at for request in schedule if request.due_at > now), None)
        meeting.next_scheduled_refresh_at = _local_to_db_datetime(next_due)
        for request in schedule:
            if request.due_at <= now and (not last_success or request.due_at > last_success):
                due_requests.append(request)
        meeting_rows[meeting.id] = {
            "source_url": meeting.source_url,
            "source_region": _meeting_source_region(meeting),
        }
    db.commit()
    if not due_requests:
        return

    last_global = _parse_local_datetime(get_setting_value(db, "loveracing_last_auto_refresh_at"))
    if not respects_minimum_interval(last_global, now, 10):
        return

    due_batches = [batch for batch in coalesce_refreshes(due_requests) if batch["run_at"] <= now]
    if not due_batches:
        return
    batch = due_batches[0]
    attempted = 0
    for meeting_id in batch["meeting_ids"]:
        row = meeting_rows.get(meeting_id)
        if not row or not row["source_region"]:
            continue
        last_attempt = _last_import_attempt_at(db, meeting_id)
        if not respects_minimum_interval(last_attempt, now, 15):
            continue
        import_meeting_url(db, row["source_url"], force=False, source_region=row["source_region"])
        attempted += 1
    if attempted:
        set_setting_value(db, "loveracing_last_auto_refresh_at", now.isoformat())
        db.commit()


def _meeting_payload(meeting: RaceMeeting) -> dict:
    return {
        "id": meeting.id,
        "source_url": meeting.source_url,
        "source_meeting_id": meeting.source_meeting_id,
        "date": meeting.date.isoformat() if meeting.date else None,
        "venue_name": meeting.venue_name,
        "club_name": meeting.club_name,
        "meeting_name": meeting.meeting_name,
        "race_count": meeting.race_count,
        "going": meeting.going,
        "weather": meeting.weather,
        "rail": meeting.rail,
        "track_direction": meeting.track_direction,
        "straight_length": meeting.straight_length,
        "loveracing_last_updated_at": meeting.loveracing_last_updated_at.isoformat() if meeting.loveracing_last_updated_at else None,
        "import_status": meeting.import_status,
        "raw_html_path": meeting.raw_html_path,
        "warnings": json.loads(meeting.warnings_json or "[]"),
        "last_error": meeting.last_error,
        "next_scheduled_refresh_at": meeting.next_scheduled_refresh_at.isoformat() if meeting.next_scheduled_refresh_at else None,
        "updated_at": meeting.updated_at.isoformat() if meeting.updated_at else None,
    }


def _run_payload(run: LoveracingImportRun) -> dict:
    return {
        "id": run.id,
        "meeting_id": run.meeting_id,
        "source_url": run.source_url,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "success": run.success,
        "http_status": run.http_status,
        "content_hash": run.content_hash,
        "raw_html_path": run.raw_html_path,
        "parser_version": run.parser_version,
        "races_found": run.races_found,
        "runners_found": run.runners_found,
        "warnings": json.loads(run.warnings_json or "[]"),
        "error_message": run.error_message,
    }


def _meeting_row(db: Session, source_url: str, parsed: ParsedMeeting | None = None) -> RaceMeeting:
    source_meeting_id = parsed.source_meeting_id if parsed else meeting_id_from_url(source_url)
    query = db.query(RaceMeeting).filter(RaceMeeting.source == "loveracing")
    meeting = None
    if source_meeting_id:
        meeting = query.filter(RaceMeeting.source_meeting_id == source_meeting_id).first()
    if not meeting:
        meeting = query.filter(RaceMeeting.source_url == source_url).first()
    if not meeting:
        meeting = RaceMeeting(source="loveracing", source_meeting_id=source_meeting_id, source_url=source_url)
        db.add(meeting)
        db.flush()
    return meeting


def _record_fetch_failure(db: Session, source_url: str, result: FetchResult, started_at: datetime) -> dict:
    meeting = _meeting_row(db, source_url)
    run = LoveracingImportRun(
        meeting_id=meeting.id,
        source_url=source_url,
        started_at=started_at,
        finished_at=datetime.utcnow(),
        success=False,
        http_status=result.status,
        error_message=result.error_message,
    )
    db.add(run)
    meeting.import_status = "fetch_failed"
    meeting.last_error = result.error_message or f"HTTP {result.status}"
    meeting.updated_at = datetime.utcnow()
    if result.status in BACKOFF_STATUSES or result.status in TRANSIENT_STATUSES:
        meeting.backoff_until = datetime.utcnow() + timedelta(minutes=45)
    db.commit()
    return {"status": "error", "message": meeting.last_error, "meeting": _meeting_payload(meeting), "run": _run_payload(run)}


def _source_region_for_meeting(db: Session, parsed: ParsedMeeting, override: str | None = None) -> str:
    normalized_override = normalize_region(override)
    if normalized_override:
        return normalized_override
    text = f"{parsed.meeting_name or ''} {parsed.venue_name or ''} {parsed.club_name or ''}"
    course = next(
        (
            item
            for item in db.query(RaceCourse).filter(RaceCourse.active.is_(True)).all()
            if _course_matches(item, text)
        ),
        None,
    )
    source_region = normalize_region(course.source_region) if course else None
    if not source_region:
        raise ValueError("Matched LOVERACING course is UNASSIGNED; assign its region before importing.")
    return source_region


def _horse_name_key(value: str | None) -> str:
    return _match_key(_clean_horse_name(value))


def _scratched_name_keys(parsed_race: ParsedRace) -> set[str]:
    names = list(parsed_race.scratched_horse_names or [])
    names.extend(runner.horse_name for runner in parsed_race.runners if runner.is_scratched)
    return {_horse_name_key(name) for name in names if _horse_name_key(name)}


def _mark_parsed_runner_scratches(parsed_race: ParsedRace) -> None:
    scratched_keys = _scratched_name_keys(parsed_race)
    if not scratched_keys:
        return
    for runner in parsed_race.runners:
        if _horse_name_key(runner.horse_name) in scratched_keys:
            runner.status = "scratched"
            runner.is_scratched = True
            if runner.jockey_name and runner.jockey_name.lower() in {"scr", "scratched"}:
                runner.jockey_name = None


def _mark_existing_entry_scratches(race: Race, parsed_race: ParsedRace) -> int:
    scratched_keys = _scratched_name_keys(parsed_race)
    if not scratched_keys:
        return 0
    marked = 0
    for entry in race.entries:
        if entry.horse and _horse_name_key(entry.horse.name) in scratched_keys:
            if not entry.is_scratched or entry.status != "scratched":
                marked += 1
            entry.is_scratched = True
            entry.status = "scratched"
    return marked


def _merge_result_runner_people_into_existing_entries(db: Session, race: Race, parsed_race: ParsedRace) -> int:
    entries_by_horse = {
        _horse_name_key(entry.horse.name): entry
        for entry in race.entries
        if entry.horse and _horse_name_key(entry.horse.name)
    }
    updated = 0
    for runner in parsed_race.runners:
        entry = entries_by_horse.get(_horse_name_key(runner.horse_name))
        if not entry:
            continue
        changed = False
        jockey = _ensure_person(db, runner.jockey_name, "Jockey") if runner.jockey_name else None
        if jockey and entry.jockey_id != jockey.id:
            entry.jockey_id = jockey.id
            changed = True
        trainer_people = _ensure_people(db, _split_trainer_names(runner.trainer_name), "Trainer")
        if trainer_people:
            additional_ids = _serialize_id_list([person.id for person in trainer_people[1:]])
            if entry.trainer_id != trainer_people[0].id:
                entry.trainer_id = trainer_people[0].id
                changed = True
            if entry.additional_trainer_ids != additional_ids:
                entry.additional_trainer_ids = additional_ids
                changed = True
        if runner.silk_image_url and entry.silk_image_url != runner.silk_image_url:
            entry.silk_image_url = runner.silk_image_url
            changed = True
        if runner.silk_description and entry.silk_description != runner.silk_description:
            entry.silk_description = runner.silk_description
            changed = True
        if changed:
            updated += 1
    return updated


def _existing_entry_horse_keys(race: Race) -> set[str]:
    return {
        _horse_name_key(entry.horse.name)
        for entry in race.entries
        if entry.horse and _horse_name_key(entry.horse.name)
    }


def _parsed_runner_horse_keys(parsed_race: ParsedRace) -> set[str]:
    return {
        _horse_name_key(runner.horse_name)
        for runner in parsed_race.runners
        if _horse_name_key(runner.horse_name)
    }


def _detail_race_matches_overview(overview_race: ParsedRace, detail_race: ParsedRace) -> bool:
    overview_keys = _parsed_runner_horse_keys(overview_race)
    detail_keys = _parsed_runner_horse_keys(detail_race)
    return not overview_keys or overview_keys.issubset(detail_keys)


def _parsed_result_matches_existing_entries(race: Race, parsed_race: ParsedRace) -> bool:
    parsed_keys = _parsed_runner_horse_keys(parsed_race)
    if not parsed_keys:
        return False
    return parsed_keys.issubset(_existing_entry_horse_keys(race))


def _should_preserve_existing_result_entries(race: Race, parsed_race: ParsedRace) -> bool:
    return (
        parsed_race.status == "results"
        and bool(race.entries)
        and len(parsed_race.runners) < len(race.entries)
        and _parsed_result_matches_existing_entries(race, parsed_race)
    )


def _existing_race_for_parsed(db: Session, track: str, race_date, parsed_race: ParsedRace) -> Race | None:
    if not race_date:
        return None
    return (
        db.query(Race)
        .filter(Race.track == track, Race.race_number == parsed_race.race_number, Race.date == race_date)
        .first()
    )


def _partial_result_races_without_existing_fields(db: Session, parsed: ParsedMeeting) -> list[int]:
    track = parsed.venue_name or parsed.meeting_name or "LOVERACING"
    missing = []
    for parsed_race in parsed.races:
        if parsed_race.status != "results" or len(parsed_race.runners) > 3:
            continue
        existing = _existing_race_for_parsed(db, track, parsed.date, parsed_race)
        if not existing or not existing.entries or not _parsed_result_matches_existing_entries(existing, parsed_race):
            missing.append(parsed_race.race_number)
    return missing


def _apply_meeting(db: Session, source_url: str, parsed: ParsedMeeting, fetch: FetchResult, source_region_override: str | None = None) -> tuple[RaceMeeting, int, int]:
    if not parsed.date:
        raise ValueError("Meeting date could not be parsed from the LOVERACING page.")
    meeting = _meeting_row(db, source_url, parsed)
    meeting.source_url = source_url
    meeting.source_meeting_id = parsed.source_meeting_id
    meeting.date = parsed.date
    meeting.venue_name = parsed.venue_name
    meeting.club_name = parsed.club_name
    meeting.meeting_name = parsed.meeting_name
    meeting.race_count = len(parsed.races)
    meeting.going = parsed.going
    meeting.weather = parsed.weather
    meeting.rail = parsed.rail
    meeting.track_direction = parsed.track_direction
    meeting.straight_length = parsed.straight_length
    meeting.loveracing_last_updated_at = parsed.last_updated
    meeting.import_status = "ok"
    meeting.content_hash = fetch.content_hash
    meeting.raw_html_path = fetch.raw_html_path
    meeting.warnings_json = json.dumps(parsed.warnings)
    meeting.last_error = None
    meeting.updated_at = datetime.utcnow()
    db.flush()

    entries_written = 0
    track = parsed.venue_name or parsed.meeting_name or "LOVERACING"
    race_date = parsed.date
    source_region = _source_region_for_meeting(db, parsed, source_region_override)
    _warn_initial_style_trainers(parsed)
    horse_cleanup = _cleanup_existing_horse_name_suffixes(db)
    if horse_cleanup["renamed"] or horse_cleanup["merged"]:
        parsed.warnings.append(
            "Cleaned horse name suffixes: "
            f"{horse_cleanup['renamed']} renamed, {horse_cleanup['merged']} merged."
        )
    trainer_location_cleanup = _cleanup_existing_trainer_location_suffixes(db)
    if trainer_location_cleanup["updated_entries"] or trainer_location_cleanup["hidden_people"]:
        parsed.warnings.append(
            "Cleaned trainer location suffixes: "
            f"{trainer_location_cleanup['updated_entries']} race entries updated, "
            f"{trainer_location_cleanup['hidden_people']} safe location-suffix records hidden."
        )
    trainer_cleanup = _cleanup_existing_trainer_partnerships(db)
    if trainer_cleanup["updated_entries"] or trainer_cleanup["hidden_people"]:
        parsed.warnings.append(
            "Split trainer partnerships: "
            f"{trainer_cleanup['updated_entries']} race entries updated, "
            f"{trainer_cleanup['hidden_people']} safe partnership records hidden."
        )
    parsed_race_numbers = {race.race_number for race in parsed.races}
    stale_races = [
        race
        for race in db.query(Race).filter(Race.meeting_id == meeting.id, Race.source == "loveracing").all()
        if race.track != track or race.date != race_date or race.race_number not in parsed_race_numbers
    ]
    for race in stale_races:
        db.delete(race)
    if stale_races:
        db.flush()
        parsed.warnings.append(f"Removed {len(stale_races)} stale race rows from an earlier parse of this meeting.")
    for parsed_race in parsed.races:
        race = (
            db.query(Race)
            .filter(Race.track == track, Race.race_number == parsed_race.race_number, Race.date == race_date)
            .first()
        )
        if not race:
            race = Race(track=track, race_number=parsed_race.race_number, date=race_date, source_region=source_region)
            db.add(race)
            db.flush()
        race.meeting_id = meeting.id
        race.source = "loveracing"
        race.source_hash = _source_hash(parsed_race)
        race.source_region = source_region
        race.start_time = parsed_race.start_time
        race.race_name = parsed_race.race_name
        race.conditions = parsed_race.conditions
        race.distance_m = parsed_race.distance_m
        race.stake_text = parsed_race.stake_text
        race.status = parsed_race.status
        _mark_parsed_runner_scratches(parsed_race)
        if _should_preserve_existing_result_entries(race, parsed_race):
            merged = _merge_result_runner_people_into_existing_entries(db, race, parsed_race)
            marked = _mark_existing_entry_scratches(race, parsed_race)
            entries_written += len(race.entries)
            parsed.warnings.append(
                f"Race {parsed_race.race_number} result refresh preserved {len(race.entries)} existing runners "
                f"and merged {merged} result-row people updates, then marked {marked} scratched from the result summary."
            )
            continue
        if parsed_race.status == "results" and race.entries and len(parsed_race.runners) < len(race.entries):
            parsed.warnings.append(
                f"Race {parsed_race.race_number} result refresh replaced {len(race.entries)} existing runners "
                "because the result runners did not match the stored field."
            )
        for entry in list(race.entries):
            db.delete(entry)
        db.flush()
        for runner in parsed_race.runners:
            entry = _runner_entry(db, race, runner)
            if entry:
                db.add(entry)
                entries_written += 1
    meeting.warnings_json = json.dumps(parsed.warnings)
    record_audit_event(
        db,
        action="loveracing_import",
        entity_type="race_meeting",
        entity_id=meeting.id,
        after={"source_url": source_url, "races": len(parsed.races), "entries": entries_written, "warnings": parsed.warnings},
        notes="Imported LOVERACING meeting overview from raw HTML.",
    )
    return meeting, len(parsed.races), entries_written


def _race_needs_detail_page(race: ParsedRace) -> bool:
    return race.status == "results" or len(race.runners) < 6


def _should_fetch_detail_pages(parsed: ParsedMeeting) -> bool:
    return parsed.stage != "nominations" and any(_race_needs_detail_page(race) for race in parsed.races)


def _augment_with_detail_pages(parsed: ParsedMeeting) -> None:
    if not _should_fetch_detail_pages(parsed):
        return
    replaced = 0
    for index, race in enumerate(list(parsed.races)):
        if not _race_needs_detail_page(race):
            continue
        if not race.source_detail_url:
            parsed.warnings.append(f"Race {race.race_number} has no race-detail URL for full-field fallback.")
            continue
        detail_fetch = fetch_meeting_html(race.source_detail_url)
        if not detail_fetch.success:
            parsed.warnings.append(f"Race {race.race_number} detail fetch failed: HTTP {detail_fetch.status or 'error'}.")
            continue
        try:
            detail_race = parse_race_detail_html(detail_fetch.html or "", race.source_detail_url, race)
        except Exception as error:
            parsed.warnings.append(f"Race {race.race_number} detail parse failed: {error.__class__.__name__}: {error}.")
            continue
        if not _detail_race_matches_overview(race, detail_race):
            parsed.warnings.append(
                f"Race {race.race_number} detail page runner mismatch; ignored instead of replacing the overview field."
            )
            continue
        detail_race.status = race.status or detail_race.status
        scratched_names = []
        seen_scratched = set()
        for name in list(race.scratched_horse_names or []) + list(detail_race.scratched_horse_names or []):
            key = _horse_name_key(name)
            if key and key not in seen_scratched:
                scratched_names.append(name)
                seen_scratched.add(key)
        detail_race.scratched_horse_names = scratched_names
        if len(detail_race.runners) > len(race.runners):
            parsed.races[index] = detail_race
            replaced += 1
    if replaced:
        parsed.warnings.append(f"Used race-detail pages for full runner lists on {replaced} races.")


def _runner_entry(db: Session, race: Race, runner: ParsedRunner) -> RaceEntry | None:
    horse = _ensure_horse(db, runner.horse_name)
    if not horse:
        return None
    jockey = _ensure_person(db, runner.jockey_name, "Jockey")
    trainer_people = _ensure_people(db, _split_trainer_names(runner.trainer_name), "Trainer")
    return RaceEntry(
        race_id=race.id,
        horse_id=horse.id,
        jockey_id=jockey.id if jockey else None,
        trainer_id=trainer_people[0].id if trainer_people else None,
        additional_trainer_ids=_serialize_id_list([person.id for person in trainer_people[1:]]),
        horse_number=runner.runner_number,
        source_hash=_source_hash(runner),
        horse_age_sex_breeding_text=runner.horse_age_sex_breeding_text,
        form=runner.form,
        barrier=runner.barrier,
        rating=runner.rating,
        weight=runner.weight,
        jockey_claim_text=runner.jockey_claim_text,
        win_odds=_safe_odds(runner.win_odds),
        place_odds=_safe_odds(runner.place_odds),
        status=runner.status,
        is_scratched=runner.is_scratched,
        is_ballot=runner.is_ballot,
        silk_image_url=runner.silk_image_url,
        silk_description=runner.silk_description,
    )


def import_meeting_url(db: Session, source_url: str, force: bool = False, source_region: str | None = None) -> dict:
    started_at = datetime.utcnow()
    meeting = _meeting_row(db, source_url)
    if meeting.backoff_until and meeting.backoff_until > datetime.utcnow() and not force:
        return {"status": "backoff", "message": f"LOVERACING source is in backoff until {meeting.backoff_until.isoformat()}.", "meeting": _meeting_payload(meeting)}
    fetch = fetch_meeting_html(source_url)
    if not fetch.success:
        return _record_fetch_failure(db, source_url, fetch, started_at)
    try:
        parsed = parse_meeting_html(fetch.html or "", source_url)
    except Exception as error:
        db.rollback()
        meeting = _meeting_row(db, source_url)
        failure_message = _set_parse_failure_status(db, meeting, f"{error.__class__.__name__}: {error}")
        run = LoveracingImportRun(
            meeting_id=meeting.id,
            source_url=source_url,
            started_at=started_at,
            finished_at=datetime.utcnow(),
            success=False,
            http_status=fetch.status,
            content_hash=fetch.content_hash,
            raw_html_path=fetch.raw_html_path,
            parser_version=PARSER_VERSION,
            error_message=failure_message,
        )
        db.add(run)
        db.commit()
        return {"status": "error", "message": meeting.last_error, "meeting": _meeting_payload(meeting), "run": _run_payload(run)}
    if parsed.stage == "nominations":
        db.rollback()
        return {
            "status": "nominations",
            "message": "Race nominations are available; waiting for declared Fields before live import.",
            "preview": _parsed_preview_payload(parsed, region=source_region),
        }
    last_successful_run = (
        db.query(LoveracingImportRun)
        .filter(LoveracingImportRun.meeting_id == meeting.id, LoveracingImportRun.success.is_(True))
        .order_by(LoveracingImportRun.started_at.desc())
        .first()
    )
    parser_changed = not last_successful_run or last_successful_run.parser_version != PARSER_VERSION
    if not force and not parser_changed and meeting.content_hash and meeting.content_hash == fetch.content_hash:
        run = LoveracingImportRun(
            meeting_id=meeting.id,
            source_url=source_url,
            started_at=started_at,
            finished_at=datetime.utcnow(),
            success=True,
            http_status=fetch.status,
            content_hash=fetch.content_hash,
            raw_html_path=fetch.raw_html_path,
            parser_version=PARSER_VERSION,
            races_found=meeting.race_count,
            runners_found=0,
            warnings_json=json.dumps(["Content hash unchanged; database update skipped."]),
        )
        meeting.import_status = "unchanged"
        meeting.raw_html_path = fetch.raw_html_path
        meeting.updated_at = datetime.utcnow()
        db.add(run)
        db.commit()
        return {"status": "unchanged", "meeting": _meeting_payload(meeting), "run": _run_payload(run)}
    try:
        _augment_with_detail_pages(parsed)
        if sum(len(race.runners) for race in parsed.races) == 0:
            raise ValueError("No runners found in parsed LOVERACING meeting page.")
        partial_result_races = _partial_result_races_without_existing_fields(db, parsed)
        if partial_result_races:
            raise ValueError(
                "LOVERACING overview only exposed partial result rows for races "
                + ", ".join(str(number) for number in partial_result_races)
                + "; race-detail fallback did not produce full fields and no previous full import exists to preserve."
            )
        meeting, races_found, runners_found = _apply_meeting(db, source_url, parsed, fetch, source_region)
        run = LoveracingImportRun(
            meeting_id=meeting.id,
            source_url=source_url,
            started_at=started_at,
            finished_at=datetime.utcnow(),
            success=True,
            http_status=fetch.status,
            content_hash=fetch.content_hash,
            raw_html_path=fetch.raw_html_path,
            parser_version=PARSER_VERSION,
            races_found=races_found,
            runners_found=runners_found,
            warnings_json=json.dumps(parsed.warnings),
        )
        db.add(run)
        db.commit()
        return {"status": "ok", "meeting": _meeting_payload(meeting), "run": _run_payload(run)}
    except Exception as error:
        db.rollback()
        meeting = _meeting_row(db, source_url)
        failure_message = _set_parse_failure_status(db, meeting, f"{error.__class__.__name__}: {error}")
        run = LoveracingImportRun(
            meeting_id=meeting.id,
            source_url=source_url,
            started_at=started_at,
            finished_at=datetime.utcnow(),
            success=False,
            http_status=fetch.status,
            content_hash=fetch.content_hash,
            raw_html_path=fetch.raw_html_path,
            parser_version=PARSER_VERSION,
            error_message=failure_message,
        )
        db.add(run)
        db.commit()
        return {"status": "error", "message": meeting.last_error, "meeting": _meeting_payload(meeting), "run": _run_payload(run)}


def undo_meeting_import(db: Session, meeting_id: int) -> dict:
    meeting = db.query(RaceMeeting).filter(RaceMeeting.id == meeting_id, RaceMeeting.source == "loveracing").first()
    if not meeting:
        raise ValueError("LOVERACING meeting was not found.")
    races = db.query(Race).filter(Race.meeting_id == meeting_id, Race.source == "loveracing").all()
    race_ids = [race.id for race in races]
    entries_deleted = 0
    for race in races:
        entries_deleted += len(race.entries)
        db.delete(race)
    meeting.import_status = "undone"
    meeting.updated_at = datetime.utcnow()
    record_audit_event(
        db,
        action="loveracing_import_undo",
        entity_type="race_meeting",
        entity_id=meeting.id,
        before={"race_ids": race_ids, "source_url": meeting.source_url},
        after={"races_deleted": len(race_ids), "entries_deleted": entries_deleted},
        notes="Removed LOVERACING-imported races and entries for this meeting. People and horse master records were preserved.",
    )
    db.commit()
    return {"status": "ok", "meeting_id": meeting_id, "races_deleted": len(race_ids), "entries_deleted": entries_deleted}
