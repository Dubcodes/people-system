import os

from fastapi import Header, HTTPException

from services.system_settings_service import get_setting_value_safe
from services.region_service import default_region, normalize_region


def _pin_region(region: str | None = None) -> str:
    return normalize_region(region) or default_region()


def regional_pin_key(base_key: str, region: str | None = None) -> str:
    normalized = _pin_region(region).lower().replace(" ", "_")
    return f"{base_key}:{normalized}"


def _regional_setting_value(base_key: str) -> str | None:
    return get_setting_value_safe(regional_pin_key(base_key)) or get_setting_value_safe(base_key)


def admin_pin() -> str:
    return _regional_setting_value("admin_pin") or os.getenv("ADMIN_PIN", "4321")


def edit_pin() -> str:
    return _regional_setting_value("edit_pin") or os.getenv("EDIT_PIN") or admin_pin()


def master_admin_pin() -> str:
    return _regional_setting_value("master_admin_pin") or os.getenv("MASTER_ADMIN_PIN", "54321")


def require_edit_pin(x_admin_pin: str | None = Header(default=None)) -> None:
    accepted = {
        edit_pin(),
        admin_pin(),
        master_admin_pin(),
        os.getenv("EDIT_PIN") or "",
        os.getenv("ADMIN_PIN", "4321"),
        os.getenv("MASTER_ADMIN_PIN", "54321"),
    }
    if x_admin_pin not in accepted:
        raise HTTPException(status_code=401, detail="Edit PIN required")


def require_admin_pin(x_admin_pin: str | None = Header(default=None)) -> None:
    require_edit_pin(x_admin_pin)


def require_master_admin_pin(x_admin_pin: str | None = Header(default=None)) -> None:
    accepted = {
        master_admin_pin(),
        os.getenv("MASTER_ADMIN_PIN", "54321"),
    }
    if x_admin_pin not in accepted:
        raise HTTPException(status_code=401, detail="Master PIN required")
