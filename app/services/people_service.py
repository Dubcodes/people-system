import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from models import Absence, Alias, Horse, HorsePerson, Image, ManualAttendee, Person, PersonRole, PersonStable, Race, RaceCourse, RaceEntry, RaceMeeting, Role, Stable
from loveracing_importer.name_utils import split_trainer_names as _split_trainer_names
from services.audit_service import record_audit_event
from services.instance_service import instance_id
from services.region_service import allowed_regions, default_region, normalize_region
from services.race_day_service import races_for_race_day, resolve_current_race_day
from services.search_service import values_match_search
from services.time_service import local_today
from schemas import ActionResponse, ImageUpdate, PersonDetail, PersonUpdate, RaceEntrySummary, RacePickerItem, RaceSummary, TimelineItem, ViewerOverview, ViewerPerson


BASE_DIR = Path(__file__).resolve().parent.parent
PRE_RACE_PRIORITY_ROLE_NAMES = {"presenter", "staff", "track staff", "track manager"}
HORSE_CONTEXT_RACE_ROLE_NAMES = {"stable rep", "stable representative"}
PERSON_ACTIVE_DAYS = 730
HORSE_ACTIVE_DAYS = 183


def _stored_regions(value: Optional[str]) -> list[str]:
    regions = []
    seen = set()
    for part in (value or "").split(","):
        normalized = normalize_region(part)
        if normalized and normalized not in seen:
            seen.add(normalized)
            regions.append(normalized)
    return regions


def _serialize_regions(regions: list[str]) -> Optional[str]:
    normalized = []
    seen = set()
    for region in regions:
        clean = normalize_region(region)
        if clean and clean not in seen:
            seen.add(clean)
            normalized.append(clean)
    return ", ".join(normalized) if normalized else None


def _stored_id_list(value: Optional[str]) -> list[int]:
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


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace(".", " ").replace(",", " ").split())


def _match_key(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _append_unique_person(items: list[Person], person: Optional[Person]) -> None:
    if person and not person.deleted_at and all(item.id != person.id for item in items):
        items.append(person)


def _trainer_people_for_person(person: Optional[Person], people_by_key: dict[str, Person]) -> list[Person]:
    if not person or person.deleted_at:
        return []
    split_names = _split_trainer_names(person.name)
    if len(split_names) <= 1:
        return [person]

    resolved = []
    for name in split_names:
        matched = people_by_key.get(_match_key(name))
        if matched and matched.id != person.id:
            _append_unique_person(resolved, matched)
    return resolved if len(resolved) == len(split_names) else [person]


def _entry_trainer_people(entry: RaceEntry, extra_trainer_people: dict[int, Person], people_by_key: dict[str, Person]) -> list[Person]:
    trainer_people = []
    for person in _trainer_people_for_person(entry.trainer, people_by_key):
        _append_unique_person(trainer_people, person)
    for person_id in _stored_id_list(entry.additional_trainer_ids):
        for person in _trainer_people_for_person(extra_trainer_people.get(person_id), people_by_key):
            _append_unique_person(trainer_people, person)
    return trainer_people


def _trainer_partnership_person_ids_to_hide(people: list[Person], today: date, people_by_key: dict[str, Person]) -> set[int]:
    hidden_ids = set()
    for person in people:
        current_role = _current_assignment([item for item in person.role_assignments if item.role], today)
        if not current_role or current_role.role.name.lower() != "trainer":
            continue
        split_names = _split_trainer_names(person.name)
        if len(split_names) <= 1:
            continue
        if all((people_by_key.get(_match_key(name)) and people_by_key[_match_key(name)].id != person.id) for name in split_names):
            hidden_ids.add(person.id)
    return hidden_ids


def _title_normalize(value: str) -> str:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        return cleaned

    lower_suffixes = {"s", "t", "re", "ve", "ll", "d", "m"}

    def title_piece(piece: str) -> str:
        apostrophe_parts = piece.lower().split("'")
        titled = []
        for index, part in enumerate(apostrophe_parts):
            if index > 0 and part in lower_suffixes:
                titled.append(part)
            else:
                titled.append(part[:1].upper() + part[1:])
        return "'".join(titled)

    return " ".join("-".join(title_piece(part) for part in word.split("-")) for word in cleaned.split())


def _active_images(person: Person) -> list[Image]:
    return [image for image in person.images if not image.deleted_at]


def _image_url(person: Person) -> Optional[str]:
    preferred = person.preferred_image
    if preferred and not preferred.deleted_at:
        return f"/{preferred.file_path.lstrip('/')}"

    images = _active_images(person)
    preferred_flagged = next((image for image in images if image.is_preferred), None)
    if preferred_flagged:
        return f"/{preferred_flagged.file_path.lstrip('/')}"

    if images:
        return f"/{images[0].file_path.lstrip('/')}"

    return None


def _thumbnail_url(person: Person) -> Optional[str]:
    images = _active_images(person)
    preferred = person.preferred_image if person.preferred_image and not person.preferred_image.deleted_at else None
    image = preferred or next((item for item in images if item.is_preferred), None) or (images[0] if images else None)
    if not image:
        return None
    path = image.thumbnail_path or image.file_path
    return f"/{path.lstrip('/')}"


def _is_current(start_date, end_date, today: date) -> bool:
    if start_date and start_date > today:
        return False
    if end_date and end_date < today:
        return False
    return True


def _current_assignment(assignments, today: date):
    current = [item for item in assignments if _is_current(item.start_date, item.end_date, today)]
    current.sort(key=lambda item: (item.start_date or date.min, item.id or 0), reverse=True)
    return current[0] if current else None


def _to_viewer_person(
    person: Person,
    today: date,
    track: Optional[str] = None,
    last_seen_date: Optional[date] = None,
    direct_race_regions: Optional[dict[int, set[str]]] = None,
) -> ViewerPerson:
    current_role = _current_assignment([item for item in person.role_assignments if item.role], today)
    current_stable = _current_assignment([item for item in person.stable_assignments if item.stable], today)
    absent_today = next((absence for absence in person.absences if absence.date == today and (absence.track == track or absence.track is None)), None)
    current_role_name = current_role.role.name.lower() if current_role else ""
    always_visible_roles = PRE_RACE_PRIORITY_ROLE_NAMES | HORSE_CONTEXT_RACE_ROLE_NAMES
    recent_cutoff = today - timedelta(days=PERSON_ACTIVE_DAYS)
    is_active = bool(
        _active_images(person)
        or current_role_name in always_visible_roles
        or not last_seen_date
        or last_seen_date >= recent_cutoff
    )

    detected_regions = _automatic_person_regions(person, (direct_race_regions or {}).get(person.id, set()))
    return ViewerPerson(
        id=person.id,
        name=person.name,
        notes=person.notes,
        image_url=_image_url(person),
        thumbnail_url=_thumbnail_url(person),
        source_region=person.source_region,
        visible_regions=_stored_regions(person.visible_regions),
        detected_regions=detected_regions,
        regions=_person_regions(person, detected_regions),
        roles=[current_role.role.name] if current_role else [],
        stables=[current_stable.stable.name] if current_stable else [],
        aliases=sorted({alias.alias for alias in person.aliases}),
        locations=sorted({image.location_taken for image in _active_images(person) if image.location_taken}),
        is_absent_today=absent_today is not None,
        last_seen_date=last_seen_date,
        is_active=is_active,
    )


def _base_people_query(db: Session):
    return db.query(Person).filter(Person.deleted_at.is_(None)).options(
        joinedload(Person.aliases),
        joinedload(Person.images),
        joinedload(Person.manual_attendees),
        joinedload(Person.preferred_image),
        joinedload(Person.role_assignments).joinedload(PersonRole.role),
        joinedload(Person.stable_assignments).joinedload(PersonStable.stable),
        joinedload(Person.horse_links).joinedload(HorsePerson.horse).joinedload(Horse.race_entries).joinedload(RaceEntry.race),
        joinedload(Person.absences),
    )


def _matches_search(person: ViewerPerson, search: str) -> bool:
    if not search:
        return True

    haystacks = [person.name, person.notes or "", *person.aliases, *person.stables]
    return values_match_search(haystacks, search)


def _automatic_person_regions(person: Person, direct_race_regions=()) -> list[str]:
    regions = set()
    normalized_person_region = normalize_region(person.source_region)
    if normalized_person_region:
        regions.add(normalized_person_region)
    for image in person.images:
        normalized_image_region = normalize_region(image.source_region)
        if normalized_image_region:
            regions.add(normalized_image_region)
    for attendee in person.manual_attendees:
        normalized_attendee_region = normalize_region(attendee.source_region)
        if normalized_attendee_region:
            regions.add(normalized_attendee_region)
    for link in person.horse_links:
        horse = link.horse
        if not horse:
            continue
        for entry in horse.race_entries:
            if not entry.race:
                continue
            normalized_race_region = normalize_region(entry.race.source_region)
            if normalized_race_region:
                regions.add(normalized_race_region)
    for direct_race_region in direct_race_regions:
        normalized_direct_race_region = normalize_region(direct_race_region)
        if normalized_direct_race_region:
            regions.add(normalized_direct_race_region)
    return sorted(regions)


def _person_regions(person: Person, detected_regions=()) -> list[str]:
    return sorted(set(_stored_regions(person.visible_regions)) | set(detected_regions or _automatic_person_regions(person)))


def _race_entry_participation_regions(rows) -> dict[int, set[str]]:
    regions_by_person: dict[int, set[str]] = {}
    for jockey_id, trainer_id, additional_trainer_ids, race_region in rows:
        normalized_race_region = normalize_region(race_region)
        if not normalized_race_region:
            continue
        person_ids = [jockey_id, trainer_id, *_stored_id_list(additional_trainer_ids)]
        for person_id in person_ids:
            if isinstance(person_id, int) and person_id > 0:
                regions_by_person.setdefault(person_id, set()).add(normalized_race_region)
    return regions_by_person


def _direct_race_regions_by_person(db: Session) -> dict[int, set[str]]:
    rows = (
        db.query(
            RaceEntry.jockey_id,
            RaceEntry.trainer_id,
            RaceEntry.additional_trainer_ids,
            Race.source_region,
        )
        .join(Race, Race.id == RaceEntry.race_id)
        .all()
    )
    return _race_entry_participation_regions(rows)


def _matches_region_scope(person: ViewerPerson, region_scope: str, current_region: str) -> bool:
    if region_scope == "all":
        return True
    return current_region in set(person.regions or [])


def _person_last_seen_dates(db: Session) -> dict[int, date]:
    last_seen: dict[int, date] = {}
    jockey_rows = (
        db.query(RaceEntry.jockey_id, func.max(Race.date))
        .join(Race, Race.id == RaceEntry.race_id)
        .filter(RaceEntry.jockey_id.isnot(None))
        .group_by(RaceEntry.jockey_id)
        .all()
    )
    trainer_rows = (
        db.query(RaceEntry.trainer_id, func.max(Race.date))
        .join(Race, Race.id == RaceEntry.race_id)
        .filter(RaceEntry.trainer_id.isnot(None))
        .group_by(RaceEntry.trainer_id)
        .all()
    )
    for person_id, seen_date in [*jockey_rows, *trainer_rows]:
        if person_id and seen_date and (person_id not in last_seen or seen_date > last_seen[person_id]):
            last_seen[person_id] = seen_date
    additional_trainer_rows = (
        db.query(RaceEntry.additional_trainer_ids, Race.date)
        .join(Race, Race.id == RaceEntry.race_id)
        .filter(RaceEntry.additional_trainer_ids.isnot(None))
        .all()
    )
    for raw_ids, seen_date in additional_trainer_rows:
        if not seen_date:
            continue
        for person_id in _stored_id_list(raw_ids):
            if person_id not in last_seen or seen_date > last_seen[person_id]:
                last_seen[person_id] = seen_date
    return last_seen


def _horse_last_seen_date(horse: Optional[Horse]) -> Optional[date]:
    if not horse:
        return None
    dates = [entry.race.date for entry in horse.race_entries or [] if entry.race]
    return max(dates) if dates else None


def _horse_is_active(last_seen_date: Optional[date], today: date) -> bool:
    if not last_seen_date:
        return True
    return last_seen_date >= today - timedelta(days=HORSE_ACTIVE_DAYS)


def _replace_current_role(db: Session, person: Person, role_name: Optional[str]) -> None:
    normalized_role = _title_normalize(role_name) if role_name else None
    active_assignments = [item for item in person.role_assignments if item.end_date is None]

    if not normalized_role:
        for assignment in active_assignments:
            db.delete(assignment)
        return

    role = db.query(Role).filter(Role.name == normalized_role).first()
    if not role:
        role = Role(name=normalized_role)
        db.add(role)
        db.flush()

    has_current = False
    for assignment in active_assignments:
        if assignment.role_id == role.id:
            has_current = True
        else:
            db.delete(assignment)

    if not has_current:
        db.add(PersonRole(person_id=person.id, role_id=role.id))


def _replace_current_stable(db: Session, person: Person, stable_name: Optional[str]) -> Optional[Stable]:
    normalized_stable = _title_normalize(stable_name) if stable_name else None
    active_assignments = [item for item in person.stable_assignments if item.end_date is None]

    if not normalized_stable:
        for assignment in active_assignments:
            db.delete(assignment)
        return None

    stable = db.query(Stable).filter(Stable.name == normalized_stable).first()
    if not stable:
        stable = Stable(name=normalized_stable)
        db.add(stable)
        db.flush()

    has_current = False
    for assignment in active_assignments:
        if assignment.stable_id == stable.id:
            has_current = True
        else:
            db.delete(assignment)

    if not has_current:
        db.add(PersonStable(person_id=person.id, stable_id=stable.id))

    return stable


def _find_horse_by_name(db: Session, horse_name: str) -> Optional[Horse]:
    horse_key = _match_key(horse_name)
    if not horse_key:
        return None
    clean_name = _title_normalize(horse_name)
    horse = db.query(Horse).filter(Horse.name == clean_name).first()
    if horse:
        return horse
    return next((item for item in db.query(Horse).all() if _match_key(item.name) == horse_key), None)


def _ensure_horse_link(db: Session, person: Person, horse_name: Optional[str], fallback_role: Optional[str], stable: Optional[Stable]) -> None:
    if not horse_name:
        return

    horse_name = _title_normalize(horse_name)
    horse = _find_horse_by_name(db, horse_name)
    if not horse:
        horse = Horse(name=horse_name, stable_id=stable.id if stable else None)
        db.add(horse)
        db.flush()
    elif stable and not horse.stable_id:
        horse.stable_id = stable.id

    resolved_role = _title_normalize(fallback_role or "associated")
    active = next(
        (item for item in person.horse_links if item.horse_id == horse.id and item.role.lower() == resolved_role.lower() and item.end_date is None),
        None,
    )
    if not active:
        db.add(HorsePerson(horse_id=horse.id, person_id=person.id, role=resolved_role))


def _replace_aliases(db: Session, person: Person, aliases: list[str]) -> None:
    normalized = {_normalize(alias) for alias in aliases if _normalize(alias)}
    normalized.add(_normalize(person.name))
    parts = person.name.split()
    if len(parts) > 1:
        normalized.add(_normalize(f"{parts[0][0]} {' '.join(parts[1:])}"))

    for alias in list(person.aliases):
        db.delete(alias)
    db.flush()

    for alias in sorted(normalized):
        db.add(Alias(person_id=person.id, alias=alias))


def _current_race_day(db: Session, requested_date: Optional[date] = None) -> Optional[date]:
    return resolve_current_race_day(db, requested_date, default_region(), local_today())


def _tagged_course_track_names(db: Session, current_region: str) -> set[str]:
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


def _today_races(db: Session, requested_date: Optional[date] = None):
    race_day = _current_race_day(db, requested_date)
    if not race_day:
        return []

    current_region = default_region()
    races = (
        races_for_race_day(db, race_day, current_region, include_inactive=bool(requested_date))
        .options(
            joinedload(Race.entries).joinedload(RaceEntry.horse).joinedload(Horse.people).joinedload(HorsePerson.person),
            joinedload(Race.entries).joinedload(RaceEntry.jockey),
            joinedload(Race.entries).joinedload(RaceEntry.trainer),
        )
        .order_by(Race.race_number.asc())
        .all()
    )
    return _filter_to_tagged_tracks_if_available(races, _tagged_course_track_names(db, current_region))


def get_viewer_overview(db: Session, search: Optional[str] = None, race_id: Optional[int] = None, pre_mode: bool = False, track: Optional[str] = None, race_date: Optional[date] = None, include_inactive: bool = False, region_scope: str = "current") -> ViewerOverview:
    today = local_today()
    current_region = default_region()
    race_day = _current_race_day(db, race_date)
    is_fallback_day = bool(race_day and race_date is None and race_day != today)
    people = _base_people_query(db).order_by(Person.name.asc()).all()
    races = _today_races(db, race_day)
    tracks = sorted({race.track for race in races})
    selected_track = track if track in tracks else (tracks[0] if tracks else None)
    viewer_day = race_day or today
    people_by_key = {_match_key(person.name): person for person in people if _match_key(person.name)}
    partnership_person_ids = _trainer_partnership_person_ids_to_hide(people, viewer_day, people_by_key)
    last_seen_dates = _person_last_seen_dates(db)
    direct_race_regions = _direct_race_regions_by_person(db)
    all_viewer_people = [
        _to_viewer_person(person, viewer_day, selected_track, last_seen_dates.get(person.id), direct_race_regions)
        for person in people
        if person.id not in partnership_person_ids
    ]
    viewer_people = list(all_viewer_people)
    if not include_inactive:
        viewer_people = [person for person in viewer_people if person.is_active]
    if search:
        viewer_people = [person for person in viewer_people if _matches_search(person, search)]
    focus_people = [person for person in all_viewer_people if not search or _matches_search(person, search)]

    visible_races = [race for race in races if not selected_track or race.track == selected_track]
    meeting = None
    if visible_races and visible_races[0].meeting_id:
        meeting = db.query(RaceMeeting).filter(RaceMeeting.id == visible_races[0].meeting_id).first()
    picker_items = [RacePickerItem(id=race.id, label=f"R{race.race_number}", race_number=race.race_number, track=race.track) for race in visible_races]

    selected_race = None
    if not pre_mode:
        if race_id:
            selected_race = next((race for race in visible_races if race.id == race_id), None)
        if not selected_race and visible_races:
            selected_race = visible_races[0]

    active_people = []
    active_race_meta = None
    selected_race_id = None
    if selected_race:
        selected_race_id = selected_race.id
        person_ids = []
        seen = set()
        active_entries = []
        extra_trainer_ids = sorted(
            {
                person_id
                for entry in selected_race.entries
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
        for entry in selected_race.entries:
            trainer_people = _entry_trainer_people(entry, extra_trainer_people, people_by_key)
            trainer_names = [person.name for person in trainer_people]
            associated_person_ids = []
            if entry.horse:
                associated_person_ids = list(dict.fromkeys(
                    link.person_id
                    for link in entry.horse.people
                    if link.end_date is None
                    and "jockey" not in link.role.lower()
                    and link.person
                    and not link.person.deleted_at
                ))
            active_entries.append(
                RaceEntrySummary(
                    horse_id=entry.horse_id,
                    horse_name=entry.horse.name,
                    horse_number=entry.horse_number,
                    jockey_person_id=entry.jockey_id if entry.jockey and not entry.jockey.deleted_at else None,
                    jockey_name=entry.jockey.name if entry.jockey and not entry.jockey.deleted_at else None,
                    trainer_name=", ".join(trainer_names) if trainer_names else None,
                    trainer_people=[{"person_id": person.id, "name": person.name} for person in trainer_people],
                    associated_person_ids=associated_person_ids,
                    status=entry.status,
                    is_scratched=entry.is_scratched,
                    is_ballot=entry.is_ballot,
                    silk_image_url=entry.silk_image_url,
                    silk_description=entry.silk_description,
                )
            )

            linked_person_ids = []
            if entry.jockey and not entry.jockey.deleted_at:
                linked_person_ids.append(entry.jockey_id)
            linked_person_ids.extend(person.id for person in trainer_people)
            if entry.horse:
                linked_person_ids.extend(
                    link.person_id
                    for link in entry.horse.people
                    if link.end_date is None and link.role.lower() in HORSE_CONTEXT_RACE_ROLE_NAMES and link.person and not link.person.deleted_at
                )
            for person_id in linked_person_ids:
                if person_id and person_id not in seen:
                    person_ids.append(person_id)
                    seen.add(person_id)

        manual_race_person_ids = [
            person_id
            for (person_id,) in db.query(ManualAttendee.person_id)
            .join(Person, Person.id == ManualAttendee.person_id)
            .filter(ManualAttendee.race_id == selected_race.id, ManualAttendee.source_region == current_region, Person.deleted_at.is_(None))
            .all()
        ]
        for person_id in manual_race_person_ids:
            if person_id and person_id not in seen:
                person_ids.append(person_id)
                seen.add(person_id)

        active_people = [person for person in focus_people if person.id in person_ids and not person.is_absent_today]
        active_race_meta = RaceSummary(
            id=selected_race.id,
            label=f"{selected_race.track} R{selected_race.race_number}",
            track=selected_race.track,
            race_number=selected_race.race_number,
            date=selected_race.date,
            entries=active_entries,
        )

    priority_people = []
    if pre_mode and selected_track and race_day:
        manual_pre_person_ids = {
            person_id
            for (person_id,) in db.query(ManualAttendee.person_id)
            .filter(
                ManualAttendee.date == race_day,
                ManualAttendee.track == selected_track,
                ManualAttendee.race_id.is_(None),
                ManualAttendee.source_region == current_region,
            )
            .all()
        }
        existing_priority_ids = {person.id for person in priority_people}
        priority_people.extend(
            person
            for person in focus_people
            if person.id in manual_pre_person_ids and person.id not in existing_priority_ids and not person.is_absent_today
        )

    filtered_roster_people = [person for person in viewer_people if _matches_region_scope(person, region_scope, current_region)]

    return ViewerOverview(
        pre_mode=pre_mode,
        selected_race_id=selected_race_id,
        selected_track=selected_track,
        race_day=race_day,
        is_fallback_day=is_fallback_day,
        current_region=current_region,
        region_scope=region_scope,
        import_status=meeting.import_status if meeting else None,
        import_warning=meeting.last_error if meeting and meeting.last_error else None,
        loveracing_last_updated_at=meeting.loveracing_last_updated_at if meeting else None,
        tracks=tracks,
        races=picker_items,
        active_race=active_people,
        active_race_meta=active_race_meta,
        priority_people=priority_people,
        all_people=filtered_roster_people,
    )



def get_suggestions(db: Session) -> dict[str, object]:
    def values(query):
        return [value for (value,) in query if value]

    return {
        "people": values(db.query(Person.name).filter(Person.deleted_at.is_(None)).order_by(Person.name).all()),
        "aliases": values(db.query(Alias.alias).join(Person, Person.id == Alias.person_id).filter(Person.deleted_at.is_(None)).order_by(Alias.alias).all()),
        "roles": values(db.query(Role.name).order_by(Role.name).all()),
        "stables": values(db.query(Stable.name).order_by(Stable.name).all()),
        "horses": [
            horse.name
            for horse in db.query(Horse).options(joinedload(Horse.race_entries).joinedload(RaceEntry.race)).order_by(Horse.name).all()
            if _horse_is_active(_horse_last_seen_date(horse), local_today())
        ],
        "locations": values(db.query(Image.location_taken).filter(Image.location_taken.isnot(None), Image.deleted_at.is_(None)).distinct().order_by(Image.location_taken).all()),
        "tracks": values(db.query(Race.track).distinct().order_by(Race.track).all()),
        "regions": allowed_regions(),
        "default_region": default_region(),
    }


def get_person_detail(db: Session, person_id: int, race_date: Optional[date] = None, track: Optional[str] = None) -> Optional[PersonDetail]:
    today = race_date or local_today()
    person = _base_people_query(db).filter(Person.id == person_id).first()
    if not person:
        return None

    base = _to_viewer_person(
        person,
        today,
        track,
        _person_last_seen_dates(db).get(person.id),
        _direct_race_regions_by_person(db),
    )
    absence = next((item for item in person.absences if item.date == today and (item.track == track or item.track is None)), None)

    return PersonDetail(
        **base.model_dump(),
        images=[
            {
                "id": image.id,
                "url": f"/{image.file_path.lstrip('/')}",
                "thumbnail_url": f"/{(image.thumbnail_path or image.file_path).lstrip('/')}",
                "width": image.width,
                "height": image.height,
                "is_preferred": image.is_preferred,
                "source_region": image.source_region,
                "location_taken": image.location_taken,
                "notes": image.notes,
                "created_at": image.created_at,
            }
            for image in _active_images(person)
        ],
        horse_links=[
            {
                "id": link.id,
                "horse_id": link.horse_id,
                "horse_name": link.horse.name,
                "role": link.role,
                "start_date": link.start_date,
                "end_date": link.end_date,
                "last_seen_date": _horse_last_seen_date(link.horse),
                "is_active": _horse_is_active(_horse_last_seen_date(link.horse), today),
            }
            for link in person.horse_links
            if link.horse
        ],
        role_timeline=[
            TimelineItem(id=assignment.id, name=assignment.role.name, start_date=assignment.start_date, end_date=assignment.end_date)
            for assignment in person.role_assignments
            if assignment.role
        ],
        stable_timeline=[
            TimelineItem(id=assignment.id, name=assignment.stable.name, start_date=assignment.start_date, end_date=assignment.end_date)
            for assignment in person.stable_assignments
            if assignment.stable
        ],
        absence_reason=absence.reason if absence else None,
    )


def set_absence(db: Session, person_id: int, is_absent: bool, reason: Optional[str], absence_date: Optional[date] = None, track: Optional[str] = None) -> bool:
    today = absence_date or local_today()
    absence = db.query(Absence).filter(Absence.person_id == person_id, Absence.date == today, Absence.track == track).first()
    before = {
        "person_id": person_id,
        "is_absent": absence is not None,
        "reason": absence.reason if absence else None,
        "date": today,
        "track": track,
    }

    if is_absent and not absence:
        db.add(Absence(person_id=person_id, date=today, track=track, reason=reason))
    elif is_absent and absence:
        absence.reason = reason
    elif absence:
        db.delete(absence)

    record_audit_event(
        db,
        action="set_absence",
        entity_type="absence",
        entity_id=person_id,
        before=before,
        after={"person_id": person_id, "is_absent": is_absent, "reason": reason, "date": today, "track": track},
        notes="Race-day absence marker updated.",
        actor="edit",
    )
    db.commit()
    return True



def delete_person(db: Session, person_id: int) -> Optional[ActionResponse]:
    person = db.query(Person).options(joinedload(Person.images)).filter(Person.id == person_id).first()
    if not person or person.deleted_at:
        return None

    before = {
        "id": person.id,
        "name": person.name,
        "image_count": len(_active_images(person)),
        "roles": [assignment.role.name for assignment in person.role_assignments if assignment.role],
        "stables": [assignment.stable.name for assignment in person.stable_assignments if assignment.stable],
    }
    person.deleted_at = datetime.utcnow()
    person.deleted_by_instance = instance_id()
    person.delete_reason = "Deleted from Manage People"
    record_audit_event(
        db,
        action="soft_delete",
        entity_type="person",
        entity_id=person.id,
        before=before,
        after={"deleted_at": person.deleted_at, "deleted_by_instance": person.deleted_by_instance},
        notes="Person hidden from active records. Linked race data and image files were preserved.",
    )
    db.commit()

    return ActionResponse(status="ok")


def _replace_id_list_value(raw_ids: Optional[str], source_id: int, target_id: int) -> Optional[str]:
    updated = []
    seen = set()
    for person_id in _stored_id_list(raw_ids):
        next_id = target_id if person_id == source_id else person_id
        if next_id and next_id not in seen:
            seen.add(next_id)
            updated.append(str(next_id))
    return ",".join(updated) if updated else None


def _move_aliases_for_merge(db: Session, source: Person, target: Person) -> None:
    target_aliases = {alias.alias for alias in target.aliases}
    wanted_aliases = {source.name, _normalize(source.name)}
    wanted_aliases.update(alias.alias for alias in source.aliases)
    for alias in list(source.aliases):
        if alias.alias in target_aliases:
            db.delete(alias)
        else:
            alias.person_id = target.id
            target_aliases.add(alias.alias)
    for alias_value in wanted_aliases:
        normalized = _normalize(alias_value)
        if normalized and normalized not in target_aliases:
            db.add(Alias(person_id=target.id, alias=normalized))
            target_aliases.add(normalized)


def _move_unique_scoped_rows(db: Session, model, source_id: int, target_id: int, scope_fields: list[str]) -> None:
    for row in db.query(model).filter(model.person_id == source_id).all():
        filters = [model.person_id == target_id]
        for field_name in scope_fields:
            filters.append(getattr(model, field_name) == getattr(row, field_name))
        existing = db.query(model).filter(*filters).first()
        if existing:
            db.delete(row)
        else:
            row.person_id = target_id


def merge_people(db: Session, source_person_id: int, target_person_id: int, note: Optional[str] = None) -> Optional[ActionResponse]:
    if source_person_id == target_person_id:
        raise ValueError("Choose two different people to merge.")
    source = _base_people_query(db).filter(Person.id == source_person_id).first()
    target = _base_people_query(db).filter(Person.id == target_person_id).first()
    if not source or not target or source.deleted_at or target.deleted_at:
        return None

    before = {
        "source": {"id": source.id, "name": source.name},
        "target": {"id": target.id, "name": target.name},
        "note": note,
    }

    _move_aliases_for_merge(db, source, target)
    for model in (PersonRole, PersonStable, HorsePerson):
        db.query(model).filter(model.person_id == source.id).update({model.person_id: target.id}, synchronize_session=False)
    _move_unique_scoped_rows(db, ManualAttendee, source.id, target.id, ["date", "track", "race_id"])
    _move_unique_scoped_rows(db, Absence, source.id, target.id, ["date", "track"])

    for image in source.images:
        image.person_id = target.id
    if not target.preferred_image_id:
        target.preferred_image_id = source.preferred_image_id or next((image.id for image in source.images if not image.deleted_at), None)
    source.preferred_image_id = None

    db.query(RaceEntry).filter(RaceEntry.jockey_id == source.id).update({RaceEntry.jockey_id: target.id}, synchronize_session=False)
    db.query(RaceEntry).filter(RaceEntry.trainer_id == source.id).update({RaceEntry.trainer_id: target.id}, synchronize_session=False)
    for entry in db.query(RaceEntry).filter(RaceEntry.additional_trainer_ids.isnot(None)).all():
        updated_ids = _replace_id_list_value(entry.additional_trainer_ids, source.id, target.id)
        if updated_ids != entry.additional_trainer_ids:
            entry.additional_trainer_ids = updated_ids

    if not target.notes and source.notes:
        target.notes = source.notes
    elif source.notes and source.notes not in (target.notes or ""):
        target.notes = f"{target.notes}\n\nMerged note from {source.name}: {source.notes}".strip()

    source.deleted_at = datetime.utcnow()
    source.deleted_by_instance = instance_id()
    source.delete_reason = f"Merged into {target.name} via Manage People."

    record_audit_event(
        db,
        action="merge",
        entity_type="person",
        entity_id=target.id,
        before=before,
        after={"source_deleted_at": source.deleted_at, "target_id": target.id, "target_name": target.name},
        notes=note or f"Merged duplicate person record {source.name} into {target.name}.",
        actor="master_admin",
    )
    db.commit()
    return ActionResponse(status="ok")


def update_person(db: Session, person_id: int, payload: PersonUpdate) -> Optional[PersonDetail]:
    person = _base_people_query(db).filter(Person.id == person_id).first()
    if not person:
        return None

    before = {
        "name": person.name,
        "notes": person.notes,
        "role": next((assignment.role.name for assignment in person.role_assignments if assignment.end_date is None and assignment.role), None),
        "stable": next((assignment.stable.name for assignment in person.stable_assignments if assignment.end_date is None and assignment.stable), None),
        "aliases": sorted(alias.alias for alias in person.aliases),
        "visible_regions": _stored_regions(person.visible_regions),
    }
    person.name = _title_normalize(payload.name)
    person.notes = payload.notes
    _replace_current_role(db, person, payload.role)
    stable = _replace_current_stable(db, person, payload.stable)
    _ensure_horse_link(db, person, payload.horse_name, payload.role, stable)
    _replace_aliases(db, person, payload.aliases)
    person.visible_regions = _serialize_regions(payload.visible_regions)

    record_audit_event(
        db,
        action="update",
        entity_type="person",
        entity_id=person.id,
        before=before,
        after={
            "name": person.name,
            "notes": person.notes,
            "role": payload.role,
            "stable": payload.stable,
            "aliases": sorted(set(payload.aliases)),
            "visible_regions": sorted(set(payload.visible_regions)),
        },
        notes="Manage People updated core person metadata.",
    )
    db.commit()
    return get_person_detail(db, person_id)


def update_image(db: Session, image_id: int, payload: ImageUpdate) -> Optional[ActionResponse]:
    image = db.query(Image).filter(Image.id == image_id, Image.deleted_at.is_(None)).first()
    if not image:
        return None

    before = {
        "notes": image.notes,
        "location_taken": image.location_taken,
        "is_preferred": image.is_preferred,
    }
    image.notes = payload.notes
    image.location_taken = payload.location_taken
    if payload.is_preferred:
        for sibling in image.person.images:
            sibling.is_preferred = sibling.id == image.id
        image.person.preferred_image_id = image.id

    record_audit_event(
        db,
        action="update",
        entity_type="image",
        entity_id=image.id,
        before=before,
        after={"notes": image.notes, "location_taken": image.location_taken, "is_preferred": payload.is_preferred},
        notes="Image metadata updated.",
    )
    db.commit()
    return ActionResponse(status="ok")


def delete_image(db: Session, image_id: int) -> Optional[ActionResponse]:
    image = db.query(Image).filter(Image.id == image_id).first()
    if not image or image.deleted_at:
        return None

    person = image.person
    before = {
        "id": image.id,
        "person_id": image.person_id,
        "file_path": image.file_path,
        "thumbnail_path": image.thumbnail_path,
        "is_preferred": image.is_preferred,
    }
    image.deleted_at = datetime.utcnow()
    image.deleted_by_instance = instance_id()
    image.delete_reason = "Deleted from Manage People"
    image.is_preferred = False
    remaining = [item for item in _active_images(person) if item.id != image.id]
    replacement = remaining[0] if remaining else None

    if person.preferred_image_id == image.id:
        person.preferred_image_id = replacement.id if replacement else None
        for sibling in remaining:
            sibling.is_preferred = replacement is not None and sibling.id == replacement.id

    record_audit_event(
        db,
        action="soft_delete",
        entity_type="image",
        entity_id=image.id,
        before=before,
        after={"deleted_at": image.deleted_at, "deleted_by_instance": image.deleted_by_instance},
        notes="Image hidden from records. Original image file was preserved.",
    )
    db.commit()

    return ActionResponse(status="ok")


def update_role_assignment(db: Session, assignment_id: int, payload) -> Optional[ActionResponse]:
    assignment = db.query(PersonRole).filter(PersonRole.id == assignment_id).first()
    if not assignment:
        return None

    before = {
        "name": assignment.role.name if assignment.role else None,
        "start_date": assignment.start_date,
        "end_date": assignment.end_date,
    }
    role_name = _title_normalize(payload.name)
    role = db.query(Role).filter(Role.name == role_name).first()
    if not role:
        role = Role(name=role_name)
        db.add(role)
        db.flush()

    assignment.role_id = role.id
    assignment.start_date = payload.start_date
    assignment.end_date = payload.end_date
    record_audit_event(
        db,
        action="update",
        entity_type="person_role",
        entity_id=assignment.id,
        before=before,
        after={"name": role.name, "start_date": assignment.start_date, "end_date": assignment.end_date},
        notes="Role timeline row updated.",
    )
    db.commit()
    return ActionResponse(status="ok")


def update_stable_assignment(db: Session, assignment_id: int, payload) -> Optional[ActionResponse]:
    assignment = db.query(PersonStable).filter(PersonStable.id == assignment_id).first()
    if not assignment:
        return None

    before = {
        "name": assignment.stable.name if assignment.stable else None,
        "start_date": assignment.start_date,
        "end_date": assignment.end_date,
    }
    stable_name = _title_normalize(payload.name)
    stable = db.query(Stable).filter(Stable.name == stable_name).first()
    if not stable:
        stable = Stable(name=stable_name)
        db.add(stable)
        db.flush()

    assignment.stable_id = stable.id
    assignment.start_date = payload.start_date
    assignment.end_date = payload.end_date
    record_audit_event(
        db,
        action="update",
        entity_type="person_stable",
        entity_id=assignment.id,
        before=before,
        after={"name": stable.name, "start_date": assignment.start_date, "end_date": assignment.end_date},
        notes="Stable timeline row updated.",
    )
    db.commit()
    return ActionResponse(status="ok")


def update_horse_link(db: Session, link_id: int, payload) -> Optional[ActionResponse]:
    link = db.query(HorsePerson).filter(HorsePerson.id == link_id).first()
    if not link:
        return None

    before = {
        "horse_name": link.horse.name if link.horse else None,
        "role": link.role,
        "start_date": link.start_date,
        "end_date": link.end_date,
    }
    horse_name = _title_normalize(payload.horse_name)
    horse = _find_horse_by_name(db, horse_name)
    if not horse:
        horse = Horse(name=horse_name)
        db.add(horse)
        db.flush()

    link.horse_id = horse.id
    link.role = _title_normalize(payload.role)
    link.start_date = payload.start_date
    link.end_date = payload.end_date
    record_audit_event(
        db,
        action="update",
        entity_type="horse_link",
        entity_id=link.id,
        before=before,
        after={"horse_name": horse.name, "role": link.role, "start_date": link.start_date, "end_date": link.end_date},
        notes="Horse link updated.",
    )
    db.commit()
    return ActionResponse(status="ok")


def delete_role_assignment(db: Session, assignment_id: int) -> Optional[ActionResponse]:
    assignment = db.query(PersonRole).filter(PersonRole.id == assignment_id).first()
    if not assignment:
        return None
    record_audit_event(
        db,
        action="delete",
        entity_type="person_role",
        entity_id=assignment.id,
        before={"name": assignment.role.name if assignment.role else None, "start_date": assignment.start_date, "end_date": assignment.end_date},
        notes="Role timeline row deleted.",
    )
    db.delete(assignment)
    db.commit()
    return ActionResponse(status="ok")


def delete_stable_assignment(db: Session, assignment_id: int) -> Optional[ActionResponse]:
    assignment = db.query(PersonStable).filter(PersonStable.id == assignment_id).first()
    if not assignment:
        return None
    record_audit_event(
        db,
        action="delete",
        entity_type="person_stable",
        entity_id=assignment.id,
        before={"name": assignment.stable.name if assignment.stable else None, "start_date": assignment.start_date, "end_date": assignment.end_date},
        notes="Stable timeline row deleted.",
    )
    db.delete(assignment)
    db.commit()
    return ActionResponse(status="ok")


def delete_horse_link(db: Session, link_id: int) -> Optional[ActionResponse]:
    link = db.query(HorsePerson).filter(HorsePerson.id == link_id).first()
    if not link:
        return None
    record_audit_event(
        db,
        action="delete",
        entity_type="horse_link",
        entity_id=link.id,
        before={"horse_name": link.horse.name if link.horse else None, "role": link.role, "start_date": link.start_date, "end_date": link.end_date},
        notes="Horse link deleted.",
    )
    db.delete(link)
    db.commit()
    return ActionResponse(status="ok")















