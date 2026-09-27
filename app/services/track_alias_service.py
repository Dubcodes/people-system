import re

from sqlalchemy.orm import Session

from models import TrackAlias


TRACK_LOCATION_RULES: dict[str, dict[str, object]] = {
    "Arawa Park": {"primary": "Rotorua", "aliases": []},
    "Ascot Park": {"primary": "Invercargill", "aliases": []},
    "Ashburton": {"primary": "Ashburton", "aliases": []},
    "Avondale": {"primary": "Auckland", "aliases": ["Avondale"]},
    "Awapuni": {"primary": "Palmerston North", "aliases": []},
    "Awapuni Synthetic": {"primary": "Palmerston North", "aliases": []},
    "Cambridge": {"primary": "Cambridge", "aliases": []},
    "Cromwell": {"primary": "Cromwell", "aliases": []},
    "Ellerslie": {"primary": "Auckland", "aliases": ["Ellerslie"]},
    "Foxton": {"primary": "Foxton", "aliases": []},
    "Gore": {"primary": "Gore", "aliases": []},
    "Hastings": {"primary": "Hastings", "aliases": []},
    "Hawera": {"primary": "Hawera", "aliases": []},
    "Kumara": {"primary": "Kumara", "aliases": []},
    "Kurow": {"primary": "Kurow", "aliases": []},
    "Matamata": {"primary": "Matamata", "aliases": []},
    "New Plymouth Raceway": {"primary": "New Plymouth", "aliases": []},
    "Oamaru": {"primary": "Oamaru", "aliases": []},
    "Omoto": {"primary": "Greymouth", "aliases": ["Omoto"]},
    "Ōtaki": {"primary": "Ōtaki", "aliases": ["Otaki"]},
    "Phar Lap Raceway": {"primary": "Timaru", "aliases": []},
    "Pukekohe Park": {"primary": "Pukekohe", "aliases": []},
    "Rangiora": {"primary": "Rangiora", "aliases": ["Fernside"]},
    "Reefton": {"primary": "Reefton", "aliases": []},
    "Riccarton Park": {"primary": "Christchurch", "aliases": ["Riccarton"]},
    "Riverton": {"primary": "Riverton", "aliases": []},
    "Ruakaka": {"primary": "Ruakaka", "aliases": []},
    "Tauherenikau": {"primary": "Featherston", "aliases": ["Tauherenikau"]},
    "Taupo": {"primary": "Taupo", "aliases": []},
    "Tauranga": {"primary": "Tauranga", "aliases": ["Greerton"]},
    "Te Aroha": {"primary": "Te Aroha", "aliases": []},
    "Te Awamutu": {"primary": "Te Awamutu", "aliases": []},
    "Te Rapa": {"primary": "Hamilton", "aliases": ["Te Rapa"]},
    "Trentham": {"primary": "Upper Hutt", "aliases": ["Trentham"]},
    "Waipukurau": {"primary": "Waipukurau", "aliases": []},
    "Wanganui": {"primary": "Whanganui", "aliases": ["Whanganui"]},
    "Waverley": {"primary": "Waverley", "aliases": []},
    "Wingatui": {"primary": "Dunedin", "aliases": ["Wingatui"]},
    "Woodville": {"primary": "Woodville", "aliases": []},
}

IGNORED_TRACK_VALUES = {
    "RaceInfo",
    "Clubs & Courses",
    "Clubs & Courses Index",
    "Forgot Your Password?",
    "Login",
    "Join",
}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _match_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _title_name(value: str | None) -> str | None:
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


def snapshot_track_alias(item: TrackAlias) -> dict:
    return {
        "id": item.id,
        "alias": item.alias,
        "canonical_track": item.canonical_track,
        "course_name": item.course_name,
        "club_name": item.club_name,
        "location_name": item.location_name,
        "source": item.source,
    }


def _track_location_rule(course_name: str | None) -> dict[str, object] | None:
    if not course_name:
        return None
    return TRACK_LOCATION_RULES.get(_title_name(course_name) or "")


def _canonical_track_location(course_name: str | None) -> str | None:
    rule = _track_location_rule(course_name)
    if not rule:
        return None
    return _title_name(str(rule.get("primary") or ""))


def _accepted_track_location_aliases(course_name: str | None) -> list[str]:
    rule = _track_location_rule(course_name)
    if not rule:
        return []
    aliases = [_title_name(str(item)) for item in (rule.get("aliases") or [])]
    return [item for item in aliases if item]


def _all_track_location_values(course_name: str | None) -> list[str]:
    values: list[str] = []
    primary = _canonical_track_location(course_name)
    if primary:
        values.append(primary)
    for alias in _accepted_track_location_aliases(course_name):
        if alias not in values:
            values.append(alias)
    return values


def _saved_track_alias_values(course_name: str | None) -> list[str]:
    return [item for item in _accepted_track_location_aliases(course_name)]


def _other_known_course_names(course_name: str | None) -> set[str]:
    current = _title_name(course_name)
    return {name for name in TRACK_LOCATION_RULES.keys() if name != current}


def _looks_invalid_track_location(value: str | None, canonical_track: str | None) -> bool:
    cleaned = _clean(value)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    bad_tokens = [
        "http",
        "www.",
        ".co.nz",
        ".com",
        ".nz/",
        "@",
        "[email",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "youtube.com",
        "facebook:",
        "contact:",
    ]
    if any(token in lowered for token in bad_tokens):
        return True
    if re.fullmatch(r"[A-Za-z]:", cleaned):
        return True

    accepted_locations = set(_all_track_location_values(canonical_track))
    saved_aliases = set(_saved_track_alias_values(canonical_track))
    primary_location = _canonical_track_location(canonical_track)
    other_course_names = _other_known_course_names(canonical_track)
    if cleaned in other_course_names:
        return True
    if primary_location and cleaned == primary_location and cleaned != canonical_track and cleaned not in saved_aliases:
        return True
    if accepted_locations and cleaned not in accepted_locations:
        alias_tokens = {"club", "racing club", "jockey club", "thoroughbred racing"}
        if any(token in lowered for token in alias_tokens):
            return True
        if len(cleaned.split()) <= 3:
            return True
    return False


def normalize_track_alias_locations(db: Session) -> dict:
    rows = db.query(TrackAlias).all()
    updated_rows: list[dict] = []

    for row in rows:
        canonical_track = _title_name(row.canonical_track)
        primary_location = _canonical_track_location(canonical_track)
        if not primary_location:
            continue

        current_location = _clean(row.location_name)
        if current_location == primary_location:
            continue
        if current_location and not _looks_invalid_track_location(current_location, canonical_track):
            continue

        updated_rows.append(
            {
                "id": row.id,
                "alias": row.alias,
                "canonical_track": row.canonical_track,
                "before_location_name": row.location_name,
                "after_location_name": primary_location,
            }
        )
        row.location_name = primary_location

    db.commit()
    return {"status": "ok", "source": "track_alias_location_normalize", "updated": len(updated_rows), "updated_rows": updated_rows}


def _ensure_canonical_track_aliases(db: Session) -> dict:
    created_rows: list[dict] = []
    updated_rows: list[dict] = []

    for course_name in sorted(TRACK_LOCATION_RULES.keys()):
        canonical_track = _title_name(course_name)
        location_name = _canonical_track_location(course_name)
        existing = db.query(TrackAlias).filter(TrackAlias.alias == canonical_track).first()
        if existing:
            before = snapshot_track_alias(existing)
            existing.canonical_track = canonical_track
            existing.course_name = canonical_track
            existing.club_name = None
            existing.location_name = location_name
            existing.source = "manual"
            after = snapshot_track_alias(existing)
            if before != after:
                updated_rows.append(after)
            continue

        row = TrackAlias(
            alias=canonical_track,
            canonical_track=canonical_track,
            course_name=canonical_track,
            club_name=None,
            location_name=location_name,
            source="manual",
        )
        db.add(row)
        db.flush()
        created_rows.append(snapshot_track_alias(row))

    db.commit()
    return {"status": "ok", "source": "track_alias_canonical_repair", "created": len(created_rows), "updated": len(updated_rows), "created_rows": created_rows, "updated_rows": updated_rows}


def cleanup_obvious_bad_track_aliases(db: Session) -> dict:
    ignored_keys = {_match_key(value) for value in IGNORED_TRACK_VALUES}
    rows = db.query(TrackAlias).all()
    deleted_rows: list[dict] = []

    for row in rows:
        alias_key = _match_key(row.alias)
        canonical_key = _match_key(row.canonical_track)
        course_key = _match_key(row.course_name)
        if alias_key not in ignored_keys and canonical_key not in ignored_keys and course_key not in ignored_keys:
            continue
        deleted_rows.append(snapshot_track_alias(row))
        db.delete(row)

    db.commit()
    return {"status": "ok", "source": "track_alias_obvious_cleanup", "deleted": len(deleted_rows), "deleted_rows": deleted_rows}


def cleanup_address_track_aliases(db: Session) -> dict:
    address_tokens = (" road ", " street ", " highway ", " drive ", " avenue ", " lane ", " racecourse ")
    rows = db.query(TrackAlias).all()
    deleted_rows: list[dict] = []

    def looks_like_address(value: str | None) -> bool:
        text = f" {_clean(value or '') or ''} ".lower()
        return any(token in text for token in address_tokens)

    for row in rows:
        alias = _clean(row.alias)
        canonical_track = _clean(row.canonical_track)
        location_name = _clean(row.location_name)
        if not alias or not canonical_track or not location_name:
            continue
        if alias != location_name or alias == canonical_track or not looks_like_address(alias):
            continue
        deleted_rows.append(snapshot_track_alias(row))
        db.delete(row)

    db.commit()
    return {"status": "ok", "source": "track_alias_address_cleanup", "deleted": len(deleted_rows), "deleted_rows": deleted_rows}


def cleanup_invalid_location_track_aliases(db: Session) -> dict:
    rows = db.query(TrackAlias).all()
    deleted_rows: list[dict] = []

    for row in rows:
        alias = _clean(row.alias)
        if not alias:
            continue
        canonical_track = _title_name(row.canonical_track)
        if not _looks_invalid_track_location(alias, canonical_track):
            continue
        deleted_rows.append(snapshot_track_alias(row))
        db.delete(row)

    db.commit()
    return {"status": "ok", "source": "track_alias_invalid_location_cleanup", "deleted": len(deleted_rows), "deleted_rows": deleted_rows}


def cleanup_track_aliases(db: Session) -> dict:
    obvious = cleanup_obvious_bad_track_aliases(db)
    address = cleanup_address_track_aliases(db)
    invalid = cleanup_invalid_location_track_aliases(db)
    canonical = _ensure_canonical_track_aliases(db)
    normalized = normalize_track_alias_locations(db)
    return {
        "status": "ok",
        "source": "track_alias_cleanup_all",
        "deleted_obvious": obvious.get("deleted", 0),
        "deleted_address": address.get("deleted", 0),
        "deleted_invalid": invalid.get("deleted", 0),
        "canonical_created": canonical.get("created", 0),
        "canonical_updated": canonical.get("updated", 0),
        "normalized_locations": normalized.get("updated", 0),
        "deleted_total": obvious.get("deleted", 0) + address.get("deleted", 0) + invalid.get("deleted", 0),
    }


def resolve_canonical_track(db: Session, value: str | None) -> str | None:
    cleaned = _title_name(value)
    if not cleaned:
        return None

    exact = db.query(TrackAlias).filter(TrackAlias.alias == cleaned).first()
    if exact and exact.canonical_track:
        return _title_name(exact.canonical_track)

    wanted_key = _match_key(cleaned)
    for row in db.query(TrackAlias).all():
        if _match_key(row.alias) == wanted_key and row.canonical_track:
            return _title_name(row.canonical_track)

    return cleaned
