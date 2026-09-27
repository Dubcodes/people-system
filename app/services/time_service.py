from datetime import date, datetime
from zoneinfo import ZoneInfo


APP_TIMEZONE = ZoneInfo("Pacific/Auckland")


def local_today() -> date:
    return local_now().date()


def local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)
