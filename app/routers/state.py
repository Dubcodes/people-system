from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel

from database import SessionLocal
from services.system_settings_service import get_setting_value, set_setting_value

router = APIRouter(prefix="/api/viewer-state", tags=["viewer-state"])

SHOW_PEOPLE_WITHOUT_IMAGES_KEY = "viewer_show_people_without_images"


class ViewerStateUpdate(BaseModel):
    selected_person_id: int | None = None
    operator_highlight_person_id: int | None = None
    selected_track: str | None = None
    selected_race_id: int | None = None
    pre_mode: bool | None = None
    race_date: str | None = None
    clear_selected: bool = False
    clear_operator_highlight: bool = False


class ViewerPreferencesUpdate(BaseModel):
    show_people_without_images: bool | None = None


_state = {
    "selected_person_id": None,
    "operator_highlight_person_id": None,
    "operator_highlight_expires_at": None,
    "selected_track": None,
    "selected_race_id": None,
    "pre_mode": True,
    "race_date": None,
}
_state_revision = 0
_state_runtime_id = uuid4().hex


def _setting_as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _viewer_preferences_payload(db) -> dict:
    return {
        "show_people_without_images": _setting_as_bool(
            get_setting_value(db, SHOW_PEOPLE_WITHOUT_IMAGES_KEY),
            default=False,
        )
    }


def _prune_expired_highlight() -> None:
    global _state_revision
    expires_at = _state.get("operator_highlight_expires_at")
    if expires_at and datetime.now(timezone.utc) >= expires_at:
        _state["operator_highlight_person_id"] = None
        _state["operator_highlight_expires_at"] = None
        _state_revision += 1


def _serialise_state() -> dict:
    _prune_expired_highlight()
    expires_at = _state.get("operator_highlight_expires_at")
    return {
        **_state,
        "operator_highlight_expires_at": expires_at.isoformat() if expires_at else None,
        "revision": _state_revision,
        "runtime_id": _state_runtime_id,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("")
def get_viewer_state():
    return _serialise_state()


@router.get("/preferences")
def get_viewer_preferences():
    db = SessionLocal()
    try:
        return _viewer_preferences_payload(db)
    finally:
        db.close()


@router.post("/preferences")
def update_viewer_preferences(payload: ViewerPreferencesUpdate):
    db = SessionLocal()
    try:
        if payload.show_people_without_images is not None:
            set_setting_value(
                db,
                SHOW_PEOPLE_WITHOUT_IMAGES_KEY,
                "true" if payload.show_people_without_images else "false",
            )
        db.commit()
        return _viewer_preferences_payload(db)
    finally:
        db.close()


@router.post("")
def update_viewer_state(payload: ViewerStateUpdate):
    global _state_revision
    before = _serialise_state()
    if payload.clear_selected:
        _state["selected_person_id"] = None
    elif payload.selected_person_id is not None:
        _state["selected_person_id"] = payload.selected_person_id

    if payload.clear_operator_highlight:
        _state["operator_highlight_person_id"] = None
        _state["operator_highlight_expires_at"] = None
    elif payload.operator_highlight_person_id is not None:
        _state["operator_highlight_person_id"] = payload.operator_highlight_person_id
        _state["operator_highlight_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=30)

    fields_set = getattr(payload, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(payload, "__fields_set__", set())
    for key in ("selected_track", "selected_race_id", "pre_mode", "race_date"):
        if key in fields_set:
            _state[key] = getattr(payload, key)

    after = _serialise_state()
    state_keys = (
        "selected_person_id",
        "operator_highlight_person_id",
        "operator_highlight_expires_at",
        "selected_track",
        "selected_race_id",
        "pre_mode",
        "race_date",
    )
    if any(before[key] != after[key] for key in state_keys):
        _state_revision += 1

    return _serialise_state()
