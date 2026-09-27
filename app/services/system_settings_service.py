from datetime import datetime

from sqlalchemy.orm import Session

from database import SessionLocal
from models import SystemSetting


def get_setting_value(db: Session, key: str) -> str | None:
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return setting.value if setting else None


def set_setting_value(db: Session, key: str, value: str | None) -> SystemSetting:
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if not setting:
        setting = SystemSetting(key=key)
        db.add(setting)
    setting.value = value
    setting.updated_at = datetime.utcnow()
    db.flush()
    return setting


def get_setting_value_safe(key: str) -> str | None:
    db = SessionLocal()
    try:
        return get_setting_value(db, key)
    except Exception:
        return None
    finally:
        db.close()
