from datetime import date

from sqlalchemy import or_

from models import Race, RaceMeeting


INACTIVE_MEETING_STATUSES = frozenset({"abandoned", "cancelled", "postponed"})


def meeting_is_currently_eligible(import_status: str | None) -> bool:
    return (import_status or "").strip().lower() not in INACTIVE_MEETING_STATUSES


def races_for_race_day(db, race_day: date, source_region: str, include_inactive: bool = False):
    query = db.query(Race).filter(Race.date == race_day, Race.source_region == source_region)
    if include_inactive:
        return query
    return query.outerjoin(RaceMeeting, Race.meeting_id == RaceMeeting.id).filter(
        or_(
            Race.meeting_id.is_(None),
            RaceMeeting.import_status.is_(None),
            ~RaceMeeting.import_status.in_(INACTIVE_MEETING_STATUSES),
        )
    )


def resolve_current_race_day(db, requested_date: date | None, source_region: str, today: date) -> date | None:
    if requested_date:
        return requested_date if races_for_race_day(db, requested_date, source_region, include_inactive=True).first() else None
    return today if races_for_race_day(db, today, source_region).first() else None
