import os


def _clean_env(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def instance_id() -> str:
    return _clean_env(os.getenv("PEOPLE_INSTANCE_ID")) or "default"


def instance_name() -> str:
    return _clean_env(os.getenv("PEOPLE_INSTANCE_NAME")) or instance_id().replace("-", " ").title()
