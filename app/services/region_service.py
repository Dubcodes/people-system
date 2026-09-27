import os

from services.instance_service import instance_name


REGIONS = ["Central", "Northern", "Christchurch", "Dunedin"]
REGION_KEYS = {item.lower(): item for item in REGIONS}


def normalize_region(value: str | None) -> str | None:
    cleaned = " ".join((value or "").split()).strip()
    if not cleaned:
        return None
    return REGION_KEYS.get(cleaned.lower())


def default_region() -> str:
    for candidate in (
        os.getenv("PEOPLE_DEFAULT_REGION"),
        os.getenv("PEOPLE_REGION"),
        instance_name(),
    ):
        normalized = normalize_region(candidate)
        if normalized:
            return normalized
    return "Northern"


def allowed_regions() -> list[str]:
    return REGIONS.copy()
