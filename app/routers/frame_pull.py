from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from database import SessionLocal
from models import FramePullImport, Person
from services.audit_service import record_audit_event
from services.auth_service import require_edit_pin
from services.frame_pull_client import FramePullAuthenticationError, FramePullClient, FramePullError, FramePullOffline
from services.ingest_service import save_frame_pull_image

router = APIRouter(prefix="/api/frame-pull", tags=["frame-pull"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_client() -> FramePullClient:
    return FramePullClient()


def _normalise_name(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _import_payload(row: FramePullImport) -> dict:
    acknowledgement_state = "acknowledged" if row.acknowledged_at else ("ack_failed" if row.acknowledgement_error else "ack_pending")
    return {
        "id": row.id, "source_kind": row.source_kind, "source_id": row.source_id,
        "person_id": row.person_id, "image_id": row.image_id, "acknowledged": bool(row.acknowledged_at),
        "acknowledgement_state": acknowledgement_state,
        "acknowledgement_error": row.acknowledgement_error,
        "imported_at": row.imported_at.isoformat() if row.imported_at else None,
    }


def _suggest_matches(db: Session, name: str | None) -> list[dict]:
    key = _normalise_name(name)
    if not key:
        return []
    return [{"id": item.id, "name": item.name} for item in db.query(Person).filter(Person.deleted_at.is_(None)).all() if _normalise_name(item.name) == key]


def _bridge_error(exc: FramePullError) -> HTTPException:
    if isinstance(exc, FramePullAuthenticationError):
        return HTTPException(502, "Frame Pull authentication failed.")
    if isinstance(exc, FramePullOffline):
        return HTTPException(503, "Frame Pull is offline or not configured.")
    return HTTPException(502, "Frame Pull request failed.")


@router.get("/status")
def status(client: FramePullClient = Depends(get_client)) -> dict:
    if not client.configured:
        return {"state": "unconfigured", "message": "Frame Pull connection is not configured."}
    try:
        health = client.health()
        return {"state": "connected", "service": health.get("service"), "version": health.get("version")}
    except FramePullAuthenticationError:
        return {"state": "auth_failed", "message": "Frame Pull authentication failed."}
    except FramePullError:
        return {"state": "offline", "message": "Frame Pull is offline or unreachable."}


@router.get("/inbox")
def inbox(
    state: str = "ready",
    race_day_id: int | None = None,
    track: str | None = None,
    name: str | None = None,
    db: Session = Depends(get_db),
    client: FramePullClient = Depends(get_client),
) -> dict:
    try:
        groups = client.list_people(race_day_id=race_day_id, review_status="approved")
        interviews = client.list_interviews(review_status="approved", ungrouped_only=True)
    except FramePullError as exc:
        raise _bridge_error(exc)
    imports = {(item.source_kind, item.source_id): item for item in db.query(FramePullImport).all()}
    rows = []
    for group in groups:
        imported = imports.get(("group", int(group["id"])))
        if state == "ready" and (imported or group.get("export_state") == "exported"):
            continue
        if state == "imported" and not imported:
            continue
        if track and _normalise_name(track) not in _normalise_name(group.get("track")):
            continue
        if name and _normalise_name(name) not in _normalise_name(group.get("name")):
            continue
        rows.append({**group, "source_kind": "group", "import_url": f"/api/frame-pull/groups/{int(group['id'])}/import", "import_state": "imported" if imported else "ready", "import": _import_payload(imported) if imported else None,
                     "suggested_matches": _suggest_matches(db, group.get("name")),
                     "portrait_proxy_url": f"/api/frame-pull/groups/{int(group['id'])}/portrait"})
    for interview in interviews:
        imported = imports.get(("interview", int(interview["id"])))
        if state == "ready" and (imported or interview.get("export_state") == "exported"):
            continue
        if state == "imported" and not imported:
            continue
        identity = interview.get("final_name") or interview.get("trusted_name")
        if name and _normalise_name(name) not in _normalise_name(identity):
            continue
        if track and _normalise_name(track) not in _normalise_name(interview.get("final_track")):
            continue
        rows.append({"id": interview["id"], "source_kind": "interview", "import_url": f"/api/frame-pull/interviews/{int(interview['id'])}/import",
                     "name": identity, "track": interview.get("final_track"), "roles": [interview["final_role"]] if interview.get("final_role") else [],
                     "interview_ids": [interview["id"]], "interview_count": 1, "candidate_count": 1 if interview.get("has_portrait") else 0,
                     "review_status": interview.get("review_status"), "export_state": interview.get("export_state"),
                     "import_state": "imported" if imported else "ready", "import": _import_payload(imported) if imported else None,
                     "suggested_matches": _suggest_matches(db, identity), "portrait_proxy_url": f"/api/frame-pull/interviews/{int(interview['id'])}/portrait"})
    return {"items": rows}


@router.get("/groups/{group_id}")
def group_detail(group_id: int, db: Session = Depends(get_db), client: FramePullClient = Depends(get_client)) -> dict:
    try:
        group = client.get_person_group(group_id)
    except FramePullError as exc:
        raise _bridge_error(exc)
    imported = db.query(FramePullImport).filter_by(source_kind="group", source_id=group_id).one_or_none()
    return {**group, "import_state": "imported" if imported else "ready", "import": _import_payload(imported) if imported else None,
            "suggested_matches": _suggest_matches(db, group.get("name")), "portrait_proxy_url": f"/api/frame-pull/groups/{group_id}/portrait"}


@router.get("/groups/{group_id}/portrait")
def portrait_proxy(group_id: int, client: FramePullClient = Depends(get_client)) -> Response:
    try:
        portrait = client.download_portrait(group_id)
    except FramePullError as exc:
        raise _bridge_error(exc)
    return Response(content=portrait.data, media_type=portrait.content_type, headers={"Cache-Control": "private, max-age=300"})


@router.post("/groups/{group_id}/import")
def import_group(group_id: int, body: dict, db: Session = Depends(get_db), client: FramePullClient = Depends(get_client), _: None = Depends(require_edit_pin)) -> dict:
    person_id = int(body.get("person_id") or 0)
    person = db.get(Person, person_id)
    if not person or person.deleted_at:
        raise HTTPException(422, "Choose an active existing person before importing.")
    existing = db.query(FramePullImport).filter_by(source_kind="group", source_id=group_id).one_or_none()
    if existing:
        return _acknowledge_existing(existing, db, client)
    try:
        group = client.get_person_group(group_id)
        if group.get("review_status") != "approved" or not group.get("preferred_candidate_id"):
            raise HTTPException(409, "Only an approved Frame Pull portrait can be imported.")
        portrait = client.download_portrait(group_id)
    except FramePullError as exc:
        raise _bridge_error(exc)
    created_paths: list[Path] = []
    try:
        roles = ", ".join(group.get("roles") or []) or None
        image, duplicate, created_paths = save_frame_pull_image(
            db, person=person, file_bytes=portrait.data, source_id=group_id,
            location_taken=group.get("track"),
            notes=f"Frame Pull: {group.get('name') or 'unresolved'}; roles: {roles or 'not supplied'}.",
            make_preferred=bool(body.get("make_preferred", False)),
        )
        imported = FramePullImport(
            source_kind="group", source_id=group_id, person_id=person.id, image_id=image.id,
            original_name=group.get("name"), race_day=str(group.get("race_day_id") or "") or None,
            track=group.get("track"), role_context=roles,
            source_metadata=json.dumps({"interview_ids": group.get("interview_ids", []), "interviews": group.get("interviews", []), "location_confidence": group.get("location_confidence")}),
        )
        db.add(imported)
        record_audit_event(db, "frame_pull_import", "frame_pull_import", group_id, after={"person_id": person.id, "image_id": image.id, "duplicate_image": duplicate}, notes="Frame Pull approved portrait imported after explicit person selection.", actor="edit")
        db.commit()
        db.refresh(imported)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise HTTPException(422, f"Frame Pull portrait could not be imported: {exc}")
    return _acknowledge_existing(imported, db, client)


@router.get("/interviews/{interview_id}/portrait")
def interview_portrait_proxy(interview_id: int, client: FramePullClient = Depends(get_client)) -> Response:
    try:
        portrait = client.download_interview_portrait(interview_id)
    except FramePullError as exc:
        raise _bridge_error(exc)
    return Response(content=portrait.data, media_type=portrait.content_type, headers={"Cache-Control": "private, max-age=300"})


@router.post("/interviews/{interview_id}/import")
def import_interview(interview_id: int, body: dict, db: Session = Depends(get_db), client: FramePullClient = Depends(get_client), _: None = Depends(require_edit_pin)) -> dict:
    person_id = int(body.get("person_id") or 0)
    person = db.get(Person, person_id)
    if not person or person.deleted_at:
        raise HTTPException(422, "Choose an active existing person before importing.")
    existing = db.query(FramePullImport).filter_by(source_kind="interview", source_id=interview_id).one_or_none()
    if existing:
        return _acknowledge_existing(existing, db, client)
    try:
        interview = client.get_interview(interview_id)
        if interview.get("review_status") != "approved" or not interview.get("has_portrait"):
            raise HTTPException(409, "Only an approved Frame Pull portrait can be imported.")
        portrait = client.download_interview_portrait(interview_id)
    except FramePullError as exc:
        raise _bridge_error(exc)
    created_paths: list[Path] = []
    try:
        identity = interview.get("final_name") or interview.get("trusted_name")
        image, duplicate, created_paths = save_frame_pull_image(
            db, person=person, file_bytes=portrait.data, source_id=interview_id,
            location_taken=interview.get("final_track"), notes=f"Frame Pull interview: {identity or 'unresolved'}; role: {interview.get('final_role') or 'not supplied'}.",
            make_preferred=bool(body.get("make_preferred", False)),
        )
        imported = FramePullImport(source_kind="interview", source_id=interview_id, person_id=person.id, image_id=image.id,
                                   original_name=identity, track=interview.get("final_track"), role_context=interview.get("final_role"),
                                   source_metadata=json.dumps({"interview_id": interview_id, "source_start": interview.get("source_start"), "source_end": interview.get("source_end")}))
        db.add(imported)
        record_audit_event(db, "frame_pull_import", "frame_pull_import", interview_id, after={"person_id": person.id, "image_id": image.id, "duplicate_image": duplicate}, notes="Frame Pull approved standalone interview imported after explicit person selection.", actor="edit")
        db.commit()
        db.refresh(imported)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise HTTPException(422, f"Frame Pull portrait could not be imported: {exc}")
    return _acknowledge_existing(imported, db, client)


def _acknowledge_existing(imported: FramePullImport, db: Session, client: FramePullClient) -> dict:
    if not imported.acknowledged_at:
        try:
            if imported.source_kind == "interview":
                client.acknowledge_interview_export(imported.source_id)
            else:
                client.acknowledge_export(imported.source_id)
            imported.acknowledged_at = datetime.utcnow()
            imported.acknowledgement_error = None
            db.commit()
        except FramePullError:
            imported.acknowledgement_error = "People import committed; Frame Pull acknowledgement will retry on the next import request."
            db.commit()
    return {"status": "imported", "import": _import_payload(imported)}


@router.post("/imports/{import_id}/retry-acknowledgement")
def retry_acknowledgement(import_id: int, db: Session = Depends(get_db), client: FramePullClient = Depends(get_client), _: None = Depends(require_edit_pin)) -> dict:
    imported = db.get(FramePullImport, import_id)
    if not imported:
        raise HTTPException(404, "Frame Pull import was not found.")
    return _acknowledge_existing(imported, db, client)
