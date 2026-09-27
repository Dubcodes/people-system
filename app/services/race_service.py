import re
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from loveracing_importer.name_utils import clean_horse_name
from models import Alias, Horse, ManualAttendee, Person, PersonRole, Race, RaceEntry, Role
from services.audit_service import record_audit_event
from schemas import ManualAttendeeCreate, ManualAttendeeSummary, RaceImportRequest, RaceImportResponse
from services.region_service import default_region, normalize_region
from services.time_service import local_today


TABLE_START_MARKERS = {"runner details", "runner details flucs"}
TABLE_END_PREFIXES = (
    "boxed ",
    "add to betslip",
    "next to go",
    "race table legend",
    "touch tone",
    "download the app",
    "follow us",
    "internet gambling",
    "entain new zealand",
)
PERSON_PLACEHOLDER_KEYS = {"tba", "tobeconfirmed", "tobeadvised", "tobeadvisedjockey", "advised"}
INVALID_HORSE_KEYS = {
    "scr",
    "scratched",
    "jockey",
    "trainer",
    "draw",
    "barrier",
    "rating",
    "weight",
    "win",
    "place",
    "form",
    "silk",
    "fields",
    "gear",
}
FALSE_HORSE_PHRASE_RE = re.compile(
    r"\b(?:lengths?|months?|capable|trial|freshened|jumped|finished|winner|placing|mare|gelding|filly|colt)\b",
    re.I,
)


def _clean(value: str) -> str:
    return " ".join(value.split()).strip()


def _normalize_alias(value: str) -> str:
    value = re.sub(r"[^a-z0-9\s]", " ", value.lower())
    return " ".join(value.split())


def _match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _strip_claim(value: str) -> str:
    return _clean(re.sub(r"\s*\([^)]*\)\s*", " ", value))


def _clean_manual_horse_name(value: str | None) -> str:
    return _clean(clean_horse_name(value))


def _is_valid_horse_name(value: str | None) -> bool:
    cleaned = _clean_manual_horse_name(value)
    if not cleaned:
        return False
    if cleaned in {"-", "--", "—", "–"}:
        return False
    if _match_key(cleaned) in INVALID_HORSE_KEYS:
        return False
    if not re.search(r"[A-Za-z]", cleaned):
        return False
    if re.fullmatch(r"\$?\d+(?:\.\d+)?", cleaned):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?\s*kg", cleaned, flags=re.I):
        return False
    if re.fullmatch(r"[A-Za-z](?:\s+[A-Za-z]){0,3}", cleaned):
        return False
    if re.search(r"\(\s*a\d*(?:/[^)]*)?\s*\)\s*$", cleaned, flags=re.I):
        return False
    if re.match(r"^(?:j|t|trainer|jockey|draw|barrier|rating|weight|win|place)\s*[:\-]", cleaned, flags=re.I):
        return False
    if len(cleaned) > 90:
        return False
    if len(cleaned.split()) > 6 and FALSE_HORSE_PHRASE_RE.search(cleaned):
        return False
    return True


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


def _title_name(value: str) -> str:
    cleaned = _clean(value)
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


def _is_useful_person_name(value: str | None) -> bool:
    if not value:
        return False
    clean_name = _title_name(value)
    if _match_key(clean_name) in PERSON_PLACEHOLDER_KEYS:
        return False
    return len(clean_name.split()) >= 2


def _ensure_aliases(db: Session, person: Person) -> None:
    aliases = {_normalize_alias(person.name)}
    parts = person.name.split()
    if len(parts) > 1:
        aliases.add(_normalize_alias(f"{parts[0][0]} {' '.join(parts[1:])}"))

    existing = {alias.alias for alias in person.aliases}
    for alias in aliases:
        if alias and alias not in existing:
            db.add(Alias(person_id=person.id, alias=alias))


def _ensure_person(db: Session, name: str, role_name: str, created: set[int], source_region: str) -> Person:
    clean_name = _title_name(name)
    person = db.query(Person).filter(Person.name == clean_name, Person.deleted_at.is_(None)).first()
    if not person:
        alias = db.query(Alias).filter(Alias.alias == _normalize_alias(clean_name)).first()
        person = alias.person if alias and alias.person and not alias.person.deleted_at else None
    if not person:
        person = next((item for item in db.query(Person).filter(Person.deleted_at.is_(None)).all() if _match_key(item.name) == _match_key(clean_name)), None)
    if not person:
        deleted_person = db.query(Person).filter(Person.name == clean_name, Person.deleted_at.isnot(None)).first()
        if not deleted_person:
            deleted_person = next(
                (item for item in db.query(Person).filter(Person.deleted_at.isnot(None)).all() if _match_key(item.name) == _match_key(clean_name)),
                None,
            )
        if deleted_person:
            deleted_person.deleted_at = None
            deleted_person.deleted_by_instance = None
            deleted_person.delete_reason = None
            record_audit_event(
                db,
                action="restore",
                entity_type="person",
                entity_id=deleted_person.id,
                notes="Person restored automatically because manual race import referenced the same name again.",
            )
            person = deleted_person
    if not person:
        person = Person(name=clean_name, source_region=source_region)
        db.add(person)
        db.flush()
        created.add(person.id)
    elif not person.source_region:
        person.source_region = source_region

    role = db.query(Role).filter(Role.name == role_name).first()
    if not role:
        role = Role(name=role_name)
        db.add(role)
        db.flush()

    has_role = any(item.role_id == role.id and item.end_date is None for item in person.role_assignments)
    if not has_role:
        db.add(PersonRole(person_id=person.id, role_id=role.id))

    _ensure_aliases(db, person)
    return person


def _ensure_horse(db: Session, name: str, created: set[int]) -> Horse:
    clean_name = _title_name(_clean_manual_horse_name(name))
    if not _is_valid_horse_name(clean_name):
        raise ValueError(f"Invalid horse name parsed from race import: {name!r}")
    horse = db.query(Horse).filter(Horse.name == clean_name).first()
    if not horse:
        horse = next((item for item in db.query(Horse).all() if _match_key(item.name) == _match_key(clean_name)), None)
    if not horse:
        horse = Horse(name=clean_name)
        db.add(horse)
        db.flush()
        created.add(horse.id)
    return horse


def _split_trainers(value: str) -> list[str]:
    parts = [_strip_claim(part) for part in re.split(r"\s+(?:&|and)\s+", value, flags=re.IGNORECASE) if _strip_claim(part)]
    if len(parts) <= 1:
        return parts

    last_tokens = parts[-1].split()
    shared_suffix = " ".join(last_tokens[1:]) if len(last_tokens) > 1 else ""
    if not shared_suffix:
        return parts

    # TAB often writes shared-surname teams as "Kevin & Stephen Gray".
    # Expand only one-word earlier parts, leaving full names like
    # "Lance O'Sullivan & Andrew Scott" unchanged.
    return [f"{part} {shared_suffix}" if len(part.split()) == 1 else part for part in parts[:-1]] + [parts[-1]]


def _is_runner_number(line: str) -> bool:
    return bool(re.fullmatch(r"\d+\.", line))


def _candidate_lines(raw_text: str) -> list[str]:
    lines = [_clean(line) for line in raw_text.splitlines() if _clean(line)]
    lowered = [line.lower() for line in lines]

    start = 0
    for index, line in enumerate(lowered):
        if line in TABLE_START_MARKERS:
            start = index + 1
            break

    trimmed = []
    for line in lines[start:]:
        lower = line.lower()
        if any(lower.startswith(prefix) for prefix in TABLE_END_PREFIXES):
            break
        trimmed.append(line)
    return trimmed


def _parse_runner_block(number: int, horse_name: str, block: list[str]) -> tuple[dict | None, str | None]:
    horse_name = _clean_manual_horse_name(horse_name)
    if not _is_valid_horse_name(horse_name):
        return None, None

    if any(line.upper() == "SCRATCHED" for line in block):
        return None, horse_name

    jockey = None
    trainers = []
    for line in block:
        lower = line.lower()
        if lower.startswith("j:"):
            jockey = _strip_claim(line[2:])
        elif lower.startswith("t:"):
            trainers = _split_trainers(line[2:])

    if not jockey and not trainers:
        return None, None

    return {"number": number, "horse": horse_name, "jockey": jockey, "trainers": trainers}, None


def _parse_runners(raw_text: str) -> tuple[list[dict], list[str]]:
    lines = _candidate_lines(raw_text)
    runners = []
    scratched = []
    index = 0

    while index < len(lines):
        if not _is_runner_number(lines[index]):
            index += 1
            continue

        number = int(lines[index].rstrip("."))
        horse_name = lines[index + 1] if index + 1 < len(lines) else ""
        block = []
        cursor = index + 2
        while cursor < len(lines) and not _is_runner_number(lines[cursor]):
            block.append(lines[cursor])
            cursor += 1

        runner, scratched_horse = _parse_runner_block(number, horse_name, block)
        if runner:
            runners.append(runner)
        if scratched_horse:
            scratched.append(scratched_horse)

        index = cursor

    if not runners and not scratched:
        raise ValueError("No runners found. Paste from Runner details through the scratched runners, or paste the full TAB race page.")

    return runners, scratched


def import_race_data(db: Session, payload: RaceImportRequest) -> RaceImportResponse:
    try:
        runners, scratched = _parse_runners(payload.raw_text)
        people_created: set[int] = set()
        horses_created: set[int] = set()
        source_region = normalize_region(payload.source_region) or default_region()

        track = _clean(payload.track).title()
        race = (
            db.query(Race)
            .filter(Race.track == track, Race.race_number == payload.race_number, Race.date == payload.race_date)
            .first()
        )
        if not race:
            race = Race(track=track, race_number=payload.race_number, date=payload.race_date, source_region=source_region)
            db.add(race)
            db.flush()
        else:
            race.source_region = source_region
            for entry in list(race.entries):
                db.delete(entry)
            db.flush()

        for runner in runners:
            horse = _ensure_horse(db, runner["horse"], horses_created)
            jockey = _ensure_person(db, runner["jockey"], "Jockey", people_created, source_region) if _is_useful_person_name(runner.get("jockey")) else None
            trainer_people = [
                _ensure_person(db, name, "Trainer", people_created, source_region)
                for name in runner.get("trainers", [])
                if _is_useful_person_name(name)
            ]

            db.add(
                RaceEntry(
                    race_id=race.id,
                    horse_id=horse.id,
                    jockey_id=jockey.id if jockey else None,
                    trainer_id=trainer_people[0].id if trainer_people else None,
                    additional_trainer_ids=_serialize_id_list([person.id for person in trainer_people[1:]]),
                    horse_number=runner.get("number"),
                )
            )

        record_audit_event(
            db,
            action="import",
            entity_type="race",
            entity_id=race.id,
            before=None,
            after={
                "track": track,
                "source_region": source_region,
                "race_number": payload.race_number,
                "race_date": payload.race_date,
                "entries_created": len(runners),
                "people_created": len(people_created),
                "horses_created": len(horses_created),
                "scratched": scratched,
            },
            notes="Race data imported from pasted race-day source text.",
        )
        db.commit()
        return RaceImportResponse(
            status="ok",
            race_id=race.id,
            source_region=source_region,
            entries_created=len(runners),
            people_created=len(people_created),
            horses_created=len(horses_created),
            scratched=scratched,
        )
    except Exception:
        db.rollback()
        raise


def preview_race_data(raw_text: str) -> dict:
    runners, scratched = _parse_runners(raw_text)
    return {
        "runners": runners,
        "scratched": scratched,
        "runner_count": len(runners),
        "scratched_count": len(scratched),
    }


def list_race_days(db: Session) -> list[dict]:
    races = (
        db.query(Race)
        .options(joinedload(Race.entries))
        .filter(Race.source_region == default_region())
        .order_by(Race.date.desc(), Race.track.asc(), Race.race_number.asc())
        .all()
    )
    grouped = defaultdict(lambda: {"date": None, "track": None, "race_count": 0, "runner_count": 0, "races": []})
    for race in races:
        key = (race.date, race.track)
        item = grouped[key]
        item["date"] = race.date.isoformat()
        item["track"] = race.track
        item["race_count"] += 1
        item["runner_count"] += len(race.entries)
        item["races"].append(
            {
                "id": race.id,
                "race_number": race.race_number,
                "runner_count": len(race.entries),
                "start_time": race.start_time,
                "race_name": race.race_name,
                "conditions": race.conditions,
            }
        )
    return list(grouped.values())


def delete_race(db: Session, race_id: int) -> dict:
    current_region = default_region()
    race = db.query(Race).filter(Race.id == race_id, Race.source_region == current_region).first()
    if not race:
        return {"deleted": False, "races_deleted": 0, "entries_deleted": 0}

    before = {
        "id": race.id,
        "track": race.track,
        "race_number": race.race_number,
        "date": race.date,
        "source_region": race.source_region,
        "entry_count": len(race.entries),
    }
    entries_deleted = db.query(RaceEntry).filter(RaceEntry.race_id == race_id).delete(synchronize_session=False)
    db.delete(race)
    record_audit_event(db, "delete", "race", race_id, before=before, notes="Race and race entries removed from Race Management.")
    db.commit()
    return {"deleted": True, "races_deleted": 1, "entries_deleted": entries_deleted}


def delete_race_day(db: Session, track: str, race_date: date) -> dict:
    current_region = default_region()
    race_ids = [
        race_id
        for (race_id,) in db.query(Race.id)
        .filter(Race.track == track, Race.date == race_date, Race.source_region == current_region)
        .all()
    ]
    if not race_ids:
        return {"deleted": False, "races_deleted": 0, "entries_deleted": 0}

    before = {"track": track, "date": race_date, "source_region": current_region, "race_ids": race_ids}
    entries_deleted = db.query(RaceEntry).filter(RaceEntry.race_id.in_(race_ids)).delete(synchronize_session=False)
    races_deleted = db.query(Race).filter(Race.id.in_(race_ids)).delete(synchronize_session=False)
    record_audit_event(
        db,
        "delete",
        "race_day",
        None,
        before=before,
        after={"source_region": current_region, "races_deleted": races_deleted, "entries_deleted": entries_deleted},
    )
    db.commit()
    return {"deleted": True, "races_deleted": races_deleted, "entries_deleted": entries_deleted}


def cleanup_old_race_data(db: Session, retention_days: int = 7) -> dict:
    cutoff = local_today() - timedelta(days=retention_days)
    current_region = default_region()
    race_ids = [
        race_id
        for (race_id,) in db.query(Race.id)
        .filter(Race.date < cutoff, Race.source_region == current_region)
        .all()
    ]
    if not race_ids:
        return {
            "deleted": False,
            "cutoff": cutoff.isoformat(),
            "source_region": current_region,
            "races_deleted": 0,
            "entries_deleted": 0,
        }

    entries_deleted = db.query(RaceEntry).filter(RaceEntry.race_id.in_(race_ids)).delete(synchronize_session=False)
    races_deleted = db.query(Race).filter(Race.id.in_(race_ids)).delete(synchronize_session=False)
    record_audit_event(
        db,
        action="cleanup",
        entity_type="race",
        before={"cutoff": cutoff, "source_region": current_region, "race_ids": race_ids},
        after={"source_region": current_region, "races_deleted": races_deleted, "entries_deleted": entries_deleted},
        notes="Old imported race data cleaned up automatically or from Race Management.",
    )
    db.commit()
    return {
        "deleted": True,
        "cutoff": cutoff.isoformat(),
        "source_region": current_region,
        "races_deleted": races_deleted,
        "entries_deleted": entries_deleted,
    }


def list_manual_attendees(db: Session, track: str | None = None, race_date: date | None = None) -> list[ManualAttendeeSummary]:
    query = (
        db.query(ManualAttendee)
        .join(Person, Person.id == ManualAttendee.person_id)
        .filter(Person.deleted_at.is_(None))
        .filter(ManualAttendee.source_region == default_region())
        .options(joinedload(ManualAttendee.person), joinedload(ManualAttendee.race))
    )
    if track:
        query = query.filter(ManualAttendee.track == track)
    if race_date:
        query = query.filter(ManualAttendee.date == race_date)
    attendees = query.order_by(ManualAttendee.date.desc(), ManualAttendee.track.asc(), ManualAttendee.race_id.asc().nullsfirst()).all()
    return [
        ManualAttendeeSummary(
            id=item.id,
            person_id=item.person_id,
            person_name=item.person.name if item.person else "Unknown person",
            track=item.track,
            race_date=item.date,
            race_id=item.race_id,
            race_label=f"R{item.race.race_number}" if item.race else "Pre-race",
            note=item.note,
            source_region=item.source_region,
        )
        for item in attendees
    ]


def _manual_attendee_summary(item: ManualAttendee) -> ManualAttendeeSummary:
    return ManualAttendeeSummary(
        id=item.id,
        person_id=item.person_id,
        person_name=item.person.name if item.person else "Unknown person",
        track=item.track,
        race_date=item.date,
        race_id=item.race_id,
        race_label=f"R{item.race.race_number}" if item.race else "Pre-race",
        note=item.note,
        source_region=item.source_region,
    )


def create_manual_attendee(db: Session, payload: ManualAttendeeCreate) -> ManualAttendeeSummary:
    track = _clean(payload.track).title()
    current_region = default_region()
    source_region = normalize_region(payload.source_region) or default_region()
    person = db.query(Person).filter(Person.id == payload.person_id, Person.deleted_at.is_(None)).first()
    if not person:
        raise ValueError("Selected person was not found.")
    if not person.source_region:
        person.source_region = source_region

    race = None
    if payload.race_id:
        race = db.query(Race).filter(Race.id == payload.race_id, Race.source_region == current_region).first()
        if not race:
            raise ValueError("Selected race was not found.")
        track = race.track
        source_region = race.source_region or source_region
        if race.date != payload.race_date:
            raise ValueError("Selected race does not match the selected date.")

    existing_query = db.query(ManualAttendee).filter(
        ManualAttendee.person_id == payload.person_id,
        ManualAttendee.date == payload.race_date,
        ManualAttendee.track == track,
    )
    existing_query = existing_query.filter(ManualAttendee.race_id == payload.race_id) if payload.race_id else existing_query.filter(ManualAttendee.race_id.is_(None))
    existing = existing_query.first()
    if existing:
        before_note = existing.note
        existing.note = payload.note
        record_audit_event(
            db,
            action="update",
            entity_type="manual_attendee",
            entity_id=existing.id,
            before={"note": before_note, "track": existing.track, "date": existing.date, "race_id": existing.race_id, "source_region": existing.source_region},
            after={"note": payload.note, "track": existing.track, "date": existing.date, "race_id": existing.race_id, "source_region": existing.source_region},
            notes="Manual attendee note updated.",
        )
        db.commit()
        db.refresh(existing)
        return _manual_attendee_summary(existing)

    item = ManualAttendee(person_id=payload.person_id, date=payload.race_date, track=track, source_region=source_region, race_id=payload.race_id, note=payload.note)
    db.add(item)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise ValueError("That manual attendee is already attached to this scope.") from error
    db.refresh(item)
    item = db.query(ManualAttendee).options(joinedload(ManualAttendee.person), joinedload(ManualAttendee.race)).filter(ManualAttendee.id == item.id).first()
    record_audit_event(
        db,
        action="create",
        entity_type="manual_attendee",
        entity_id=item.id,
        after={"person_id": item.person_id, "track": item.track, "date": item.date, "race_id": item.race_id, "note": item.note, "source_region": item.source_region},
        notes="Manual attendee added to race-day scope.",
    )
    db.commit()
    return _manual_attendee_summary(item)


def delete_manual_attendee(db: Session, attendee_id: int) -> dict:
    item = db.query(ManualAttendee).filter(ManualAttendee.id == attendee_id, ManualAttendee.source_region == default_region()).first()
    if not item:
        return {"deleted": False}
    record_audit_event(
        db,
        action="delete",
        entity_type="manual_attendee",
        entity_id=item.id,
        before={"person_id": item.person_id, "track": item.track, "date": item.date, "race_id": item.race_id, "note": item.note},
        notes="Manual attendee removed from race-day scope.",
    )
    db.delete(item)
    db.commit()
    return {"deleted": True}


