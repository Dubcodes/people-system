import hashlib
import re
import uuid
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image as PillowImage, ImageOps
from sqlalchemy.orm import Session

from models import Alias, Horse, HorsePerson, Image, Person, PersonRole, PersonStable, Role, Stable
from schemas import IngestResponse
from services.audit_service import record_audit_event
from services.region_service import default_region, normalize_region


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "images"
DEFAULT_WIDTH = 1400
THUMBNAIL_WIDTH = 420
CROP_RATIOS = {
    "4:3": (4, 3),
    "1:1": (1, 1),
    "16:9": (16, 9),
}


def _normalize_name(value: str) -> str:
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


def _normalize_alias(value: str) -> str:
    value = re.sub(r"[^a-z0-9\s]", " ", value.lower())
    return " ".join(value.split())


def _match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _ensure_person(db: Session, name: str, notes: Optional[str], source_region: Optional[str]) -> Person:
    normalized_name = _normalize_name(name)
    normalized_region = normalize_region(source_region) or default_region()
    person = db.query(Person).filter(Person.name == normalized_name, Person.deleted_at.is_(None)).first()
    if not person:
        alias = db.query(Alias).filter(Alias.alias == _normalize_alias(normalized_name)).first()
        person = alias.person if alias and alias.person and not alias.person.deleted_at else None
    if not person:
        person = next((item for item in db.query(Person).filter(Person.deleted_at.is_(None)).all() if _match_key(item.name) == _match_key(normalized_name)), None)
    if not person:
        deleted_person = db.query(Person).filter(Person.name == normalized_name, Person.deleted_at.isnot(None)).first()
        if deleted_person:
            deleted_person.deleted_at = None
            deleted_person.deleted_by_instance = None
            deleted_person.delete_reason = None
            record_audit_event(
                db,
                action="restore",
                entity_type="person",
                entity_id=deleted_person.id,
                notes="Person restored automatically because a new image was ingested for the same name.",
            )
            person = deleted_person
    if person:
        if notes and not person.notes:
            person.notes = notes
        if not person.source_region:
            person.source_region = normalized_region
        return person

    person = Person(name=normalized_name, notes=notes, source_region=normalized_region)
    db.add(person)
    db.flush()
    return person


def _ensure_role(db: Session, person: Person, role_name: Optional[str]) -> None:
    if not role_name:
        return

    role_name = _normalize_name(role_name)
    role = db.query(Role).filter(Role.name == role_name).first()
    if not role:
        role = Role(name=role_name)
        db.add(role)
        db.flush()

    existing = next((item for item in person.role_assignments if item.role_id == role.id and item.end_date is None), None)
    if not existing:
        db.add(PersonRole(person_id=person.id, role_id=role.id))


def _ensure_stable(db: Session, person: Person, stable_name: Optional[str]) -> Optional[Stable]:
    if not stable_name:
        return None

    stable_name = _normalize_name(stable_name)
    stable = db.query(Stable).filter(Stable.name == stable_name).first()
    if not stable:
        stable = Stable(name=stable_name)
        db.add(stable)
        db.flush()

    existing = next((item for item in person.stable_assignments if item.stable_id == stable.id and item.end_date is None), None)
    if not existing:
        db.add(PersonStable(person_id=person.id, stable_id=stable.id))

    return stable


def _ensure_horse_link(db: Session, person: Person, horse_name: Optional[str], fallback_role: Optional[str], stable: Optional[Stable]) -> None:
    if not horse_name:
        return

    horse_name = _normalize_name(horse_name)
    horse = db.query(Horse).filter(Horse.name == horse_name).first()
    if not horse:
        horse = next((item for item in db.query(Horse).all() if _match_key(item.name) == _match_key(horse_name)), None)
    if not horse:
        horse = Horse(name=horse_name, stable_id=stable.id if stable else None)
        db.add(horse)
        db.flush()
    elif stable and not horse.stable_id:
        horse.stable_id = stable.id

    resolved_role = _normalize_name(fallback_role or "associated")
    existing = next(
        (item for item in person.horse_links if item.horse_id == horse.id and item.role.lower() == resolved_role.lower() and item.end_date is None),
        None,
    )
    if not existing:
        db.add(HorsePerson(horse_id=horse.id, person_id=person.id, role=resolved_role))


def _ensure_aliases(db: Session, person: Person, name: str, alias_text: Optional[str]) -> None:
    aliases = {_normalize_alias(name)}
    parts = name.split()
    if len(parts) > 1:
        aliases.add(_normalize_alias(f"{parts[0][0]} {' '.join(parts[1:])}"))

    if alias_text:
        for candidate in alias_text.split(","):
            normalized = _normalize_alias(candidate)
            if normalized:
                aliases.add(normalized)

    existing = {item.alias for item in person.aliases}
    for alias in aliases:
        if alias and alias not in existing:
            db.add(Alias(person_id=person.id, alias=alias))


def _crop_and_resize(raw_bytes: bytes, ratio_key: str):
    ratio = CROP_RATIOS.get(ratio_key, CROP_RATIOS["4:3"])
    ratio_width, ratio_height = ratio

    with PillowImage.open(BytesIO(raw_bytes)) as image:
        prepared = ImageOps.exif_transpose(image).convert("RGB")
        source_width, source_height = prepared.size
        source_ratio = source_width / source_height
        target_ratio = ratio_width / ratio_height

        if source_ratio > target_ratio:
            crop_width = int(source_height * target_ratio)
            left = int((source_width - crop_width) / 2)
            crop_box = (left, 0, left + crop_width, source_height)
        else:
            crop_height = int(source_width / target_ratio)
            top = int((source_height - crop_height) / 2)
            crop_box = (0, top, source_width, top + crop_height)

        cropped = prepared.crop(crop_box)
        if cropped.width > DEFAULT_WIDTH:
            target_height = int(DEFAULT_WIDTH * ratio_height / ratio_width)
            cropped = cropped.resize((DEFAULT_WIDTH, target_height), PillowImage.Resampling.LANCZOS)

        output = BytesIO()
        cropped.save(output, format="JPEG", quality=95, optimize=True)

        thumbnail = cropped.copy()
        if thumbnail.width > THUMBNAIL_WIDTH:
            thumbnail_height = int(THUMBNAIL_WIDTH * ratio_height / ratio_width)
            thumbnail = thumbnail.resize((THUMBNAIL_WIDTH, thumbnail_height), PillowImage.Resampling.LANCZOS)
        thumbnail_output = BytesIO()
        thumbnail.save(thumbnail_output, format="JPEG", quality=88, optimize=True)
        return output.getvalue(), thumbnail_output.getvalue(), cropped.width, cropped.height


def _thumbnail_for_existing_image(disk_path: Path) -> tuple[bytes, int, int]:
    with PillowImage.open(disk_path) as image:
        prepared = ImageOps.exif_transpose(image).convert("RGB")
        thumbnail = prepared.copy()
        if thumbnail.width > THUMBNAIL_WIDTH:
            target_height = int(THUMBNAIL_WIDTH * thumbnail.height / thumbnail.width)
            thumbnail = thumbnail.resize((THUMBNAIL_WIDTH, target_height), PillowImage.Resampling.LANCZOS)
        output = BytesIO()
        thumbnail.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue(), prepared.width, prepared.height


def _image_url(image: Image) -> str:
    return f"/{image.file_path}"


def _duplicate_response(image: Image, message: str) -> IngestResponse:
    return IngestResponse(
        status="duplicate",
        person_id=image.person_id,
        image_id=image.id,
        image_url=_image_url(image),
        source_region=image.source_region,
        message=message,
    )


def _find_existing_image_by_hash(db: Session, file_hash: str) -> Image | None:
    if not file_hash:
        return None
    return (
        db.query(Image)
        .filter(Image.file_hash == file_hash, Image.deleted_at.is_(None))
        .order_by(Image.id.asc())
        .first()
    )


def backfill_image_hashes(db: Session, limit: int = 2000) -> dict[str, int]:
    updated = 0
    skipped = 0
    images = (
        db.query(Image)
        .filter(Image.file_hash.is_(None), Image.deleted_at.is_(None))
        .order_by(Image.id.asc())
        .limit(limit)
        .all()
    )
    for image in images:
        disk_path = BASE_DIR / image.file_path
        if not disk_path.exists():
            skipped += 1
            continue
        try:
            image.file_hash = hashlib.sha256(disk_path.read_bytes()).hexdigest()
            updated += 1
        except OSError:
            skipped += 1
    return {"updated": updated, "skipped": skipped}


def generate_missing_thumbnails(db: Session) -> dict[str, int]:
    created = 0
    skipped = 0
    failed = 0
    images = db.query(Image).filter(Image.thumbnail_path.is_(None), Image.deleted_at.is_(None)).all()

    for image in images:
        source_path = BASE_DIR / image.file_path
        if not source_path.exists():
            skipped += 1
            continue

        thumbnail_path = source_path.with_name(f"{source_path.stem}_thumb.jpg")
        try:
            thumbnail_bytes, width, height = _thumbnail_for_existing_image(source_path)
            with thumbnail_path.open("wb") as handle:
                handle.write(thumbnail_bytes)
            image.thumbnail_path = str(thumbnail_path.relative_to(BASE_DIR)).replace("\\", "/")
            image.width = image.width or width
            image.height = image.height or height
            created += 1
        except Exception:
            failed += 1

    db.commit()
    return {"created": created, "skipped": skipped, "failed": failed}


def save_ingested_image(
    db: Session,
    *,
    file_bytes: bytes,
    original_filename: str,
    name: str,
    role: Optional[str],
    stable: Optional[str],
    horse_name: Optional[str],
    location_taken: Optional[str],
    notes: Optional[str],
    alias_text: Optional[str],
    aspect_ratio: str,
    source_region: Optional[str],
) -> IngestResponse:
    file_path = None
    try:
        normalized_region = normalize_region(source_region) or default_region()
        processed_bytes, thumbnail_bytes, width, height = _crop_and_resize(file_bytes, aspect_ratio)
        file_hash = hashlib.sha256(processed_bytes).hexdigest()
        existing_image = _find_existing_image_by_hash(db, file_hash)
        if existing_image:
            return _duplicate_response(existing_image, "This exact image already exists, so it was not saved again.")

        created_person = False
        person = _ensure_person(db, name, notes, normalized_region)
        created_person = not bool(person.images) and not bool(person.aliases) and not bool(person.role_assignments) and not bool(person.stable_assignments) and not bool(person.horse_links)
        _ensure_role(db, person, role)
        stable_record = _ensure_stable(db, person, stable)
        _ensure_horse_link(db, person, horse_name, role, stable_record)
        _ensure_aliases(db, person, name, alias_text)

        image_key = uuid.uuid4().hex
        person_dir = UPLOAD_DIR / str(person.id)
        person_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{image_key}.jpg"
        thumbnail_filename = f"{image_key}_thumb.jpg"
        disk_path = person_dir / filename
        thumbnail_disk_path = person_dir / thumbnail_filename
        file_path = f"images/{person.id}/{filename}"
        thumbnail_path = f"images/{person.id}/{thumbnail_filename}"
        with disk_path.open("wb") as handle:
            handle.write(processed_bytes)
        with thumbnail_disk_path.open("wb") as handle:
            handle.write(thumbnail_bytes)

        for existing in person.images:
            existing.is_preferred = False

        image = Image(
            person_id=person.id,
            file_path=file_path,
            thumbnail_path=thumbnail_path,
            file_hash=file_hash,
            source_region=normalized_region,
            width=width,
            height=height,
            is_preferred=True,
            location_taken=location_taken,
            notes=notes,
        )
        db.add(image)
        db.flush()

        person.preferred_image_id = image.id
        record_audit_event(
            db,
            action="ingest_image",
            entity_type="image",
            entity_id=image.id,
            after={
                "person_id": person.id,
                "person_name": person.name,
                "image_path": file_path,
                "thumbnail_path": thumbnail_path,
                "created_person_guess": created_person,
                "location_taken": location_taken,
                "source_region": normalized_region,
                "width": width,
                "height": height,
                "original_filename": original_filename,
            },
            notes="Image ingested through Image Ingest.",
        )
        db.commit()
        db.refresh(image)

        return IngestResponse(
            status="ok",
            person_id=person.id,
            image_id=image.id,
            image_url=f"/{file_path}",
            source_region=normalized_region,
            message="Image saved.",
        )
    except Exception:
        db.rollback()
        if file_path:
            disk_path = BASE_DIR / file_path
            if disk_path.exists():
                try:
                    disk_path.unlink()
                except OSError:
                    pass
        raise


def save_frame_pull_image(
    db: Session,
    *,
    person: Person,
    file_bytes: bytes,
    source_id: int,
    location_taken: Optional[str],
    notes: Optional[str],
    make_preferred: bool = False,
) -> tuple[Image, bool, list[Path]]:
    """Store a validated remote portrait for an explicitly selected person.

    This deliberately does not call _ensure_person: Frame Pull names are a
    suggestion for an operator, never authority to create or match a person.
    The caller owns the transaction and removes returned files on rollback.
    """
    processed_bytes, thumbnail_bytes, width, height = _crop_and_resize(file_bytes, "16:9")
    file_hash = hashlib.sha256(processed_bytes).hexdigest()
    existing = _find_existing_image_by_hash(db, file_hash)
    if existing:
        if existing.person_id != person.id:
            raise ValueError("This portrait already belongs to a different person record.")
        return existing, True, []

    image_key = uuid.uuid4().hex
    person_dir = UPLOAD_DIR / str(person.id)
    person_dir.mkdir(parents=True, exist_ok=True)
    disk_path = person_dir / f"{image_key}.jpg"
    thumbnail_disk_path = person_dir / f"{image_key}_thumb.jpg"
    try:
        disk_path.write_bytes(processed_bytes)
        thumbnail_disk_path.write_bytes(thumbnail_bytes)
        image = Image(
            person_id=person.id,
            file_path=f"images/{person.id}/{disk_path.name}",
            thumbnail_path=f"images/{person.id}/{thumbnail_disk_path.name}",
            file_hash=file_hash,
            source_region=person.source_region,
            width=width,
            height=height,
            is_preferred=make_preferred,
            location_taken=location_taken,
            notes=notes or f"Imported from Frame Pull result {source_id}.",
        )
        if make_preferred:
            for prior in person.images:
                prior.is_preferred = False
        db.add(image)
        db.flush()
        if make_preferred:
            person.preferred_image_id = image.id
        return image, False, [disk_path, thumbnail_disk_path]
    except Exception:
        for path in (disk_path, thumbnail_disk_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise





