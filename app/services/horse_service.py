import re
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from models import Horse, HorsePerson, Race, RaceEntry, Stable
from loveracing_importer.name_utils import clean_horse_name, split_trainer_names as _split_trainer_names
from schemas import HorseDetail, HorseRaceHistoryItem, HorseSummary, HorseUpdate
from services.audit_service import record_audit_event
from services.time_service import local_today


HORSE_ACTIVE_DAYS = 183
FALSE_HORSE_BREEDING_RE = re.compile(r"\(\s*\d{1,2}\s*[cfgmhr]\b[^)]*\)\s*$", re.I)
FALSE_HORSE_PHRASE_RE = re.compile(r"\b(?:lengths?|months?|capable|trial|freshened|jumped|finished|winner|placing)\b", re.I)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _match_key(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _is_false_import_horse_name(value: Optional[str]) -> bool:
    cleaned = _clean(value) or ""
    if not cleaned:
        return True
    if cleaned in {"-", "--", "—", "–"}:
        return True
    if not re.search(r"[A-Za-z]", cleaned):
        return True
    if re.fullmatch(r"\$?\d+(?:\.\d+)?", cleaned):
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?\s*kg", cleaned, flags=re.I):
        return True
    if re.search(r"\(\s*a\d*(?:/[^)]*)?\s*\)\s*$", cleaned, flags=re.I):
        return True
    if FALSE_HORSE_BREEDING_RE.search(cleaned):
        return True
    if len(cleaned) > 90:
        return True
    if len(cleaned.split()) > 6 and FALSE_HORSE_PHRASE_RE.search(cleaned):
        return True
    return False


def _trainer_display_name(value: Optional[str]) -> Optional[str]:
    names = _split_trainer_names(value)
    return ", ".join(names) if names else None


def _smart_title(value: Optional[str]) -> Optional[str]:
    cleaned = _clean(value)
    if not cleaned:
        return None

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


def _find_horse_by_name(db: Session, name: Optional[str]) -> Optional[Horse]:
    key = _match_key(name)
    if not key:
        return None
    exact_name = _smart_title(name)
    if exact_name:
        exact = db.query(Horse).filter(Horse.name == exact_name).first()
        if exact:
            return exact
    return next((horse for horse in db.query(Horse).all() if _match_key(horse.name) == key), None)


def _stable_by_name(db: Session, stable_name: Optional[str]) -> Optional[Stable]:
    stable_name = _smart_title(stable_name)
    if not stable_name:
        return None
    stable = db.query(Stable).filter(Stable.name == stable_name).first()
    if not stable:
        stable = Stable(name=stable_name)
        db.add(stable)
        db.flush()
    return stable


def _last_race_date(horse: Horse):
    dates = [entry.race.date for entry in horse.race_entries or [] if entry.race]
    return max(dates) if dates else None


def _is_active(last_seen_date) -> bool:
    if not last_seen_date:
        return True
    return last_seen_date >= local_today() - timedelta(days=HORSE_ACTIVE_DAYS)


def _summary(horse: Horse) -> HorseSummary:
    last_seen_date = _last_race_date(horse)
    return HorseSummary(
        id=horse.id,
        name=clean_horse_name(horse.name) or horse.name,
        stable_name=horse.stable.name if horse.stable else None,
        notes=horse.notes,
        race_count=len(horse.race_entries or []),
        last_seen_date=last_seen_date,
        is_active=_is_active(last_seen_date),
    )


def list_horses(db: Session, search: Optional[str] = None, include_inactive: bool = False) -> list[HorseSummary]:
    query = db.query(Horse).options(
        joinedload(Horse.stable),
        joinedload(Horse.race_entries).joinedload(RaceEntry.race),
    ).order_by(Horse.name.asc())
    horses = [horse for horse in query.all() if not _is_false_import_horse_name(horse.name)]
    if search:
        term = _match_key(search)
        plain_term = search.lower().strip()
        horses = [
            horse
            for horse in horses
            if term in _match_key(horse.name)
            or (horse.stable and plain_term in horse.stable.name.lower())
            or (horse.notes and plain_term in horse.notes.lower())
        ]
    summaries = [_summary(horse) for horse in horses]
    if not include_inactive:
        summaries = [horse for horse in summaries if horse.is_active]
    return summaries


def get_horse_detail(db: Session, horse_id: int) -> Optional[HorseDetail]:
    horse = (
        db.query(Horse)
        .options(
            joinedload(Horse.stable),
            joinedload(Horse.people).joinedload(HorsePerson.person),
            joinedload(Horse.race_entries).joinedload(RaceEntry.race),
            joinedload(Horse.race_entries).joinedload(RaceEntry.jockey),
            joinedload(Horse.race_entries).joinedload(RaceEntry.trainer),
        )
        .filter(Horse.id == horse_id)
        .first()
    )
    if not horse:
        return None

    base = _summary(horse).model_dump()
    race_history = sorted(
        (
            HorseRaceHistoryItem(
                race_id=entry.race_id,
                track=entry.race.track if entry.race else "",
                race_number=entry.race.race_number if entry.race else 0,
                date=entry.race.date if entry.race else None,
                horse_number=entry.horse_number,
                jockey_name=entry.jockey.name if entry.jockey and not entry.jockey.deleted_at else None,
                trainer_name=_trainer_display_name(entry.trainer.name) if entry.trainer and not entry.trainer.deleted_at else None,
            )
            for entry in horse.race_entries
            if entry.race
        ),
        key=lambda item: (item.date, item.track, item.race_number),
        reverse=True,
    )

    associated_people = [
        {
            "id": link.id,
            "person_id": link.person_id,
            "person_name": link.person.name,
            "role": link.role,
            "start_date": link.start_date,
            "end_date": link.end_date,
        }
        for link in horse.people
        if link.person and not link.person.deleted_at
    ]

    return HorseDetail(**base, race_history=race_history, associated_people=associated_people)


def update_horse(db: Session, horse_id: int, payload: HorseUpdate) -> Optional[HorseDetail]:
    horse = db.query(Horse).filter(Horse.id == horse_id).first()
    if not horse:
        return None

    before = {
        "name": horse.name,
        "stable_name": horse.stable.name if horse.stable else None,
        "notes": horse.notes,
    }
    horse.name = _smart_title(payload.name) or horse.name
    horse.notes = _clean(payload.notes)
    stable = _stable_by_name(db, payload.stable_name)
    horse.stable_id = stable.id if stable else None
    after = {
        "name": horse.name,
        "stable_name": stable.name if stable else None,
        "notes": horse.notes,
    }
    record_audit_event(
        db,
        "update",
        "horse",
        horse.id,
        before=before,
        after=after,
        notes="Horse record updated from the Horses page.",
        actor="edit",
    )
    db.commit()
    return get_horse_detail(db, horse_id)
