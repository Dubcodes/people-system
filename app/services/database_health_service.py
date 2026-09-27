"""Conservative, read-only database health checks and safe horse-junk classification."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from models import (
    Absence,
    Alias,
    Horse,
    HorsePerson,
    Image,
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
    TrackAlias,
)
from services.region_service import normalize_region
from services.audit_service import record_audit_event
from services.backup_service import create_backup_snapshot


GENERIC_HORSE_ARTIFACTS = {"colt", "gelding", "filly", "mare", "horse"}
WEIGHT_ARTIFACT_PATTERN = re.compile(r"^[4-6]\d\.\d{2}$")
INITIALS_STYLE_NAME_PATTERN = re.compile(r"^(?:[A-Z]\s+){1,3}[A-Z][A-Za-z'’\-]+$")
COMPOSITE_PERSON_PATTERN = re.compile(r"\s(?:and|&)\s", re.IGNORECASE)

CATEGORY_DEFINITIONS = (
    ("safe_orphan_horse_junk", "Safe orphan horse junk"),
    ("referenced_impossible_horse_artifacts", "Referenced impossible horse artifacts"),
    ("duplicate_people", "Suspected duplicate people"),
    ("duplicate_horses", "Suspected duplicate horses"),
    ("review_name_people", "People whose names need review"),
    ("suspicious_role_combinations", "Suspicious role combinations"),
    ("orphan_relationship_rows", "Orphan relationship rows"),
    ("region_racecourse_health", "Region and racecourse health"),
    ("track_alias_health", "Track alias health"),
    ("relationship_inconsistencies", "Relationship inconsistencies"),
)


class DatabaseHealthCleanupError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _normalise_text(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def _normalise_key(value: str | None) -> str:
    return _normalise_text(value).casefold()


def _serialise(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def impossible_horse_name_reason(name: str | None) -> str | None:
    """Return a reason only for deterministic malformed-nominations artifacts."""
    cleaned = _normalise_text(name)
    lowered = cleaned.casefold()
    if cleaned == "-":
        return "Lone hyphen is a malformed nominations-parser placeholder."
    if lowered in GENERIC_HORSE_ARTIFACTS:
        return f"'{cleaned}' is a generic sex/type label, not a horse name in this import context."
    if WEIGHT_ARTIFACT_PATTERN.fullmatch(cleaned):
        return f"'{cleaned}' is a pure weight-like decimal imported into the horse-name column."
    return None


def horse_reference_counts(db: Session, horse: Horse) -> dict[str, int]:
    """Count every meaningful relation currently modelled for a horse before cleanup."""
    race_entries = db.query(RaceEntry).filter(RaceEntry.horse_id == horse.id).count()
    horse_people = db.query(HorsePerson).filter(HorsePerson.horse_id == horse.id).count()
    stable_assignment = 1 if horse.stable_id is not None else 0
    return {
        "race_entries": race_entries,
        "horse_people": horse_people,
        "stable_assignment": stable_assignment,
        "total": race_entries + horse_people + stable_assignment,
    }


def safe_orphan_horse_candidate(db: Session, horse_id: int) -> dict[str, Any] | None:
    """Re-check server-side safety. Client scan output is intentionally never trusted."""
    candidate, _reason = _safe_orphan_horse_candidate_with_status(db, horse_id)
    return candidate


def _safe_orphan_horse_candidate_with_status(
    db: Session, horse_id: int, *, lock_for_update: bool = False
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a server-derived candidate or a precise non-deletion reason."""
    query = db.query(Horse).filter(Horse.id == horse_id)
    if lock_for_update:
        # PostgreSQL FOR UPDATE prevents a concurrent rename/stable assignment and
        # conflicts with FK key-share locks while this horse is being revalidated.
        query = query.with_for_update()
    horse = query.first()
    if not horse:
        return None, "missing"
    reason = impossible_horse_name_reason(horse.name)
    if not reason:
        return None, "not_an_impossible_artifact"
    reference_counts = horse_reference_counts(db, horse)
    if reference_counts["total"]:
        return None, "referenced"
    return {
        "horse": horse,
        "reason": reason,
        "reference_counts": reference_counts,
    }, None


def _finding(
    category: str,
    safety_class: str,
    table: str,
    row_id: int | None,
    display_value: str,
    reason: str,
    *,
    reference_counts: dict[str, int] | None = None,
    safe_to_delete: bool = False,
    suggested_action: str = "Review deliberately; no automatic change is available.",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "severity": safety_class,
        "safety_class": safety_class,
        "table": table,
        "row_id": row_id,
        "display_value": display_value,
        "reason": reason,
        "reference_counts": reference_counts or {},
        "safe_to_delete": safe_to_delete,
        "suggested_action": suggested_action,
        "evidence": evidence or {},
    }


def _append(groups: dict[str, list[dict[str, Any]]], finding: dict[str, Any]) -> None:
    groups[finding["category"]].append(finding)


def _id_set(db: Session, model) -> set[int]:
    return {row_id for (row_id,) in db.query(model.id).all()}


def _scan_impossible_horses(db: Session, groups: dict[str, list[dict[str, Any]]]) -> None:
    for horse in db.query(Horse).order_by(Horse.id.asc()).all():
        reason = impossible_horse_name_reason(horse.name)
        if not reason:
            continue
        reference_counts = horse_reference_counts(db, horse)
        if reference_counts["total"] == 0:
            _append(
                groups,
                _finding(
                    "safe_orphan_horse_junk",
                    "SAFE CLEANUP",
                    "horses",
                    horse.id,
                    horse.name,
                    reason,
                    reference_counts=reference_counts,
                    safe_to_delete=True,
                    suggested_action="May be deleted only after Master confirmation and a fresh backup.",
                ),
            )
            continue
        _append(
            groups,
            _finding(
                "referenced_impossible_horse_artifacts",
                "REFERENCED — DO NOT DELETE",
                "horses",
                horse.id,
                horse.name,
                reason,
                reference_counts=reference_counts,
                suggested_action="Keep this row. Review its references before any manual repair.",
            ),
        )


def _scan_duplicate_names(db: Session, groups: dict[str, list[dict[str, Any]]]) -> None:
    people = db.query(Person).order_by(Person.id.asc()).all()
    horses = db.query(Horse).order_by(Horse.id.asc()).all()
    people_by_name: dict[str, list[Person]] = defaultdict(list)
    horses_by_name: dict[str, list[Horse]] = defaultdict(list)
    for person in people:
        people_by_name[_normalise_key(person.name)].append(person)
    for horse in horses:
        horses_by_name[_normalise_key(horse.name)].append(horse)

    for normalized, rows in people_by_name.items():
        if normalized and len(rows) > 1:
            ids = [row.id for row in rows]
            for row in rows:
                _append(groups, _finding(
                    "duplicate_people", "REVIEW", "people", row.id, row.name,
                    f"Exact normalized-name collision with people {ids}.",
                    suggested_action="Review source history and aliases; do not merge automatically.",
                    evidence={"normalized_name": normalized, "matching_person_ids": ids},
                ))
    for normalized, rows in horses_by_name.items():
        if normalized and len(rows) > 1:
            ids = [row.id for row in rows]
            for row in rows:
                _append(groups, _finding(
                    "duplicate_horses", "REVIEW", "horses", row.id, row.name,
                    f"Exact normalized-name collision with horses {ids}.",
                    suggested_action="Review race history; do not merge automatically.",
                    evidence={"normalized_name": normalized, "matching_horse_ids": ids},
                ))

    people_name_ids = {key: [row.id for row in rows] for key, rows in people_by_name.items()}
    for alias in db.query(Alias).order_by(Alias.id.asc()).all():
        matching_ids = [person_id for person_id in people_name_ids.get(_normalise_key(alias.alias), []) if person_id != alias.person_id]
        if matching_ids:
            _append(groups, _finding(
                "duplicate_people", "REVIEW", "aliases", alias.id, alias.alias,
                f"Alias matches the normalized primary name of person record(s) {matching_ids}.",
                suggested_action="Review the alias and source history; do not merge or delete automatically.",
                evidence={"person_id": alias.person_id, "matching_person_ids": matching_ids},
            ))


def _scan_review_names_and_roles(db: Session, groups: dict[str, list[dict[str, Any]]]) -> None:
    people = {person.id: person for person in db.query(Person).all()}
    roles = {role.id: role for role in db.query(Role).all()}
    for person in people.values():
        if INITIALS_STYLE_NAME_PATTERN.fullmatch(_normalise_text(person.name)):
            _append(groups, _finding(
                "review_name_people", "REVIEW", "people", person.id, person.name,
                "Initials-style source shorthand needs human name review; it is not junk evidence.",
                suggested_action="Review source history only. Never auto-delete or auto-rename this person.",
            ))

    for assignment in db.query(PersonRole).order_by(PersonRole.id.asc()).all():
        person = people.get(assignment.person_id)
        role = roles.get(assignment.role_id)
        if not person or not role:
            continue
        if role.name.casefold() == "jockey" and COMPOSITE_PERSON_PATTERN.search(person.name or ""):
            _append(groups, _finding(
                "suspicious_role_combinations", "REVIEW", "person_roles", assignment.id,
                f"{person.name} — {role.name}",
                "Composite trainer-style identity currently has a Jockey role; source evidence must be reviewed.",
                suggested_action="Review import/source evidence; do not remove the role automatically.",
                evidence={
                    "person_id": person.id,
                    "role_id": role.id,
                    "start_date": _serialise(assignment.start_date),
                    "end_date": _serialise(assignment.end_date),
                },
            ))


def _scan_orphan_relationships(db: Session, groups: dict[str, list[dict[str, Any]]]) -> None:
    person_ids = _id_set(db, Person)
    horse_ids = _id_set(db, Horse)
    role_ids = _id_set(db, Role)
    stable_ids = _id_set(db, Stable)
    race_ids = _id_set(db, Race)
    image_ids = _id_set(db, Image)
    meeting_ids = _id_set(db, RaceMeeting)

    checks = (
        (HorsePerson, "horse_people", (("horse_id", horse_ids), ("person_id", person_ids))),
        (PersonRole, "person_roles", (("person_id", person_ids), ("role_id", role_ids))),
        (PersonStable, "person_stables", (("person_id", person_ids), ("stable_id", stable_ids))),
        (RaceEntry, "race_entries", (("race_id", race_ids), ("horse_id", horse_ids), ("jockey_id", person_ids), ("trainer_id", person_ids))),
        (Image, "images", (("person_id", person_ids),)),
        (Alias, "aliases", (("person_id", person_ids),)),
        (ManualAttendee, "manual_attendees", (("person_id", person_ids), ("race_id", race_ids))),
        (Absence, "absences", (("person_id", person_ids),)),
        (PendingImage, "pending_images", (("processed_person_id", person_ids), ("processed_image_id", image_ids))),
    )
    for model, table, fields in checks:
        for row in db.query(model).order_by(model.id.asc()).all():
            for field, valid_ids in fields:
                value = getattr(row, field)
                if value is not None and value not in valid_ids:
                    _append(groups, _finding(
                        "orphan_relationship_rows", "REVIEW", table, row.id, f"{table} {row.id}",
                        f"{field}={value} references a missing parent row.",
                        suggested_action="Report only in this pass; repair deliberately after reviewing source data.",
                        evidence={"field": field, "missing_parent_id": value},
                    ))

    for row in db.query(Race).order_by(Race.id.asc()).all():
        if row.meeting_id is not None and row.meeting_id not in meeting_ids:
            _append(groups, _finding(
                "orphan_relationship_rows", "REVIEW", "races", row.id, f"{row.track} R{row.race_number}",
                f"meeting_id={row.meeting_id} references a missing race meeting.",
                suggested_action="Report only in this pass; repair deliberately after reviewing source data.",
                evidence={"field": "meeting_id", "missing_parent_id": row.meeting_id},
            ))


def _scan_region_health(db: Session, groups: dict[str, list[dict[str, Any]]]) -> None:
    courses = db.query(RaceCourse).order_by(RaceCourse.id.asc()).all()
    courses_by_track: dict[str, list[RaceCourse]] = defaultdict(list)
    for course in courses:
        normalized_region = normalize_region(course.source_region)
        if not normalized_region:
            reason = "RaceCourse source_region is missing." if not _normalise_text(course.source_region) else f"RaceCourse source_region '{course.source_region}' is invalid."
            _append(groups, _finding(
                "region_racecourse_health", "REVIEW", "racecourses", course.id, course.name, reason,
                suggested_action="Assign the course region deliberately; no automatic rewrite is performed.",
                evidence={"stored_region": course.source_region},
            ))
        for value in (course.name, course.loveracing_name):
            key = _normalise_key(value)
            if key:
                courses_by_track[key].append(course)

    meetings = {meeting.id: meeting for meeting in db.query(RaceMeeting).all()}
    for race in db.query(Race).order_by(Race.date.asc(), Race.id.asc()).all():
        stored_region = normalize_region(race.source_region)
        matching_courses = courses_by_track.get(_normalise_key(race.track), [])
        current_course = next((course for course in matching_courses if normalize_region(course.source_region)), None)
        evidence = {
            "race_id": race.id,
            "track": race.track,
            "date": _serialise(race.date),
            "stored_race_region": race.source_region,
            "current_course_region": current_course.source_region if current_course else None,
            "source_meeting_id": meetings.get(race.meeting_id).source_meeting_id if race.meeting_id in meetings else None,
        }
        if not stored_region:
            reason = "Race source_region is missing." if not _normalise_text(race.source_region) else f"Race source_region '{race.source_region}' is invalid."
            _append(groups, _finding(
                "region_racecourse_health", "REVIEW", "races", race.id, f"{race.track} R{race.race_number}", reason,
                suggested_action="Review the race and its course assignment; no historical region is rewritten automatically.", evidence=evidence,
            ))
        if current_course and stored_region != normalize_region(current_course.source_region):
            _append(groups, _finding(
                "region_racecourse_health", "REVIEW", "races", race.id, f"{race.track} R{race.race_number}",
                "Stored race region conflicts with the current assigned RaceCourse region.",
                suggested_action="Review historical/source evidence, including Wingatui-style conflicts; no automatic rewrite is performed.", evidence=evidence,
            ))


def _scan_track_alias_health(db: Session, groups: dict[str, list[dict[str, Any]]]) -> None:
    aliases = db.query(TrackAlias).order_by(TrackAlias.id.asc()).all()
    by_alias: dict[str, list[TrackAlias]] = defaultdict(list)
    for row in aliases:
        normalized_alias = _normalise_key(row.alias)
        by_alias[normalized_alias].append(row)
        if not normalized_alias or not _normalise_text(row.canonical_track):
            _append(groups, _finding(
                "track_alias_health", "REVIEW", "track_aliases", row.id, row.alias or f"Track alias {row.id}",
                "Track alias has a blank or whitespace-only alias/canonical track.",
                suggested_action="Review and repair manually; this scan does not rewrite aliases.",
            ))
    for normalized_alias, rows in by_alias.items():
        canonical_tracks = {_normalise_key(row.canonical_track) for row in rows}
        if normalized_alias and len(rows) > 1:
            reason = "Duplicate normalized alias rows." if len(canonical_tracks) == 1 else "Conflicting normalized alias rows map to different canonical tracks."
            for row in rows:
                _append(groups, _finding(
                    "track_alias_health", "REVIEW", "track_aliases", row.id, row.alias, reason,
                    suggested_action="Review deliberately; this scan does not rewrite aliases.",
                    evidence={"normalized_alias": normalized_alias, "matching_alias_ids": [item.id for item in rows], "canonical_tracks": sorted(canonical_tracks)},
                ))


def _scan_relationship_inconsistencies(db: Session, groups: dict[str, list[dict[str, Any]]]) -> None:
    duplicate_specs = (
        (HorsePerson, "horse_people", ("horse_id", "person_id", "role", "start_date", "end_date")),
        (PersonRole, "person_roles", ("person_id", "role_id", "start_date", "end_date")),
        (RaceEntry, "race_entries", ("race_id", "horse_id")),
    )
    for model, table, fields in duplicate_specs:
        grouped: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
        for row in db.query(model).order_by(model.id.asc()).all():
            grouped[tuple(getattr(row, field) for field in fields)].append(row)
        for key, rows in grouped.items():
            if len(rows) < 2:
                continue
            ids = [row.id for row in rows]
            for row in rows:
                _append(groups, _finding(
                    "relationship_inconsistencies", "REVIEW", table, row.id, f"{table} {row.id}",
                    f"Duplicate identical relationship identity across rows {ids}.",
                    suggested_action="Review source evidence; no relationship rows are removed automatically.",
                    evidence={"identity_fields": dict(zip(fields, map(_serialise, key))), "matching_row_ids": ids},
                ))


def scan_database_health(db: Session) -> dict[str, Any]:
    """Read-only health report. This function does not add, delete, flush, or commit."""
    groups: dict[str, list[dict[str, Any]]] = {key: [] for key, _label in CATEGORY_DEFINITIONS}
    # A request-scoped API session is normally clean. no_autoflush also makes this
    # reusable scanner incapable of flushing a caller's pending work by accident.
    with db.no_autoflush:
        _scan_impossible_horses(db, groups)
        _scan_duplicate_names(db, groups)
        _scan_review_names_and_roles(db, groups)
        _scan_orphan_relationships(db, groups)
        _scan_region_health(db, groups)
        _scan_track_alias_health(db, groups)
        _scan_relationship_inconsistencies(db, groups)

    all_findings = [finding for rows in groups.values() for finding in rows]
    safe_count = sum(1 for finding in all_findings if finding["safety_class"] == "SAFE CLEANUP")
    review_count = sum(1 for finding in all_findings if finding["safety_class"] == "REVIEW")
    referenced_count = sum(1 for finding in all_findings if finding["safety_class"] == "REFERENCED — DO NOT DELETE")
    region_count = len(groups["region_racecourse_health"])
    return {
        "status": "ok",
        "read_only": True,
        "summary": {
            "safe_cleanup": safe_count,
            "needs_review": review_count,
            "referenced_issues": referenced_count,
            "region_warnings": region_count,
            "other_warnings": max(0, review_count - region_count),
        },
        "categories": [
            {"key": key, "label": label, "count": len(groups[key]), "findings": groups[key]}
            for key, label in CATEGORY_DEFINITIONS
        ],
    }


def remove_safe_orphan_horses(db: Session, requested: Any, confirmed: bool) -> dict[str, Any]:
    """Delete selected horse artifacts only after backup and immediate revalidation."""
    if not confirmed:
        raise DatabaseHealthCleanupError(422, "Explicit cleanup confirmation is required.")
    if not isinstance(requested, list) or not requested:
        raise DatabaseHealthCleanupError(422, "Select at least one safe orphan horse row to remove.")

    horse_ids: list[int] = []
    for value in requested:
        if isinstance(value, bool):
            raise DatabaseHealthCleanupError(422, "Horse IDs must be whole numbers.")
        try:
            horse_id = int(value)
        except (TypeError, ValueError) as error:
            raise DatabaseHealthCleanupError(422, "Horse IDs must be whole numbers.") from error
        if horse_id <= 0:
            raise DatabaseHealthCleanupError(422, "Horse IDs must be positive whole numbers.")
        if horse_id not in horse_ids:
            horse_ids.append(horse_id)

    # Preflight avoids creating a backup when every selected row is already stale/unsafe.
    # The exact same checks run again immediately before every delete below.
    preflight = [candidate for horse_id in horse_ids if (candidate := safe_orphan_horse_candidate(db, horse_id))]
    if not preflight:
        raise DatabaseHealthCleanupError(409, "None of the selected rows are currently safe orphan junk.")

    try:
        backup = create_backup_snapshot(db, reason="database_health_pre_cleanup")
        if not backup or backup.get("status") != "ok":
            raise RuntimeError("Backup service did not confirm a successful snapshot.")
    except Exception as error:
        db.rollback()
        raise DatabaseHealthCleanupError(503, "Safety backup failed; no database cleanup was performed.") from error

    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        # Backup reads may have opened an implicit transaction. End that read-only
        # scope, then make the revalidation, deletes, and audit events one commit.
        db.rollback()
        with db.begin():
            # Do not trust the old scan or the client payload. Re-check name and
            # references inside the mutation transaction so new links protect a horse.
            for horse_id in horse_ids:
                candidate, skipped_reason = _safe_orphan_horse_candidate_with_status(
                    db, horse_id, lock_for_update=True
                )
                if not candidate:
                    skipped.append({"id": horse_id, "reason": skipped_reason or "not_safe"})
                    continue
                horse = candidate["horse"]
                before = {
                    "id": horse.id,
                    "name": horse.name,
                    "reason": candidate["reason"],
                    "reference_counts": candidate["reference_counts"],
                }
                record_audit_event(
                    db,
                    action="database_health_safe_cleanup",
                    entity_type="horse",
                    entity_id=horse.id,
                    before=before,
                    notes="Database Health removed an objectively impossible, completely unreferenced horse import artifact after a fresh backup.",
                    actor="master_admin",
                )
                db.delete(horse)
                deleted.append(before)

            record_audit_event(
                db,
                action="database_health_cleanup_summary",
                entity_type="database_health",
                after={
                    "backup_filename": backup["filename"],
                    "requested_horse_ids": horse_ids,
                    "deleted_horse_ids": [row["id"] for row in deleted],
                    "skipped": skipped,
                },
                notes="Database Health safe orphan cleanup run. Every selected row was revalidated immediately before deletion.",
                actor="master_admin",
            )
    except Exception as error:
        db.rollback()
        raise DatabaseHealthCleanupError(500, "Safe orphan cleanup failed and was rolled back. The safety backup remains available.") from error

    return {
        "status": "ok",
        "backup": {"filename": backup["filename"], "table_counts": backup.get("table_counts", {})},
        "deleted": deleted,
        "skipped": skipped,
        "skipped_horse_ids": [row["id"] for row in skipped],
    }
