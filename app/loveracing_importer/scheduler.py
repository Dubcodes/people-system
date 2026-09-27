from dataclasses import dataclass
from datetime import datetime, time, timedelta


@dataclass(frozen=True)
class RefreshRequest:
    meeting_id: int
    due_at: datetime
    priority: str
    reason: str


def planned_refreshes(meeting_id: int, race_times: list[datetime]) -> list[RefreshRequest]:
    if not race_times:
        return []
    race_times = sorted(race_times)
    first_race = race_times[0]
    last_race = race_times[-1]
    race_day = first_race.date()
    local_tz = first_race.tzinfo
    morning_scratching_check = datetime.combine(race_day, time(7, 0), tzinfo=local_tz)
    requests = [
        RefreshRequest(meeting_id, morning_scratching_check, "medium", "7am_scratching_check"),
        RefreshRequest(meeting_id, first_race - timedelta(hours=2), "medium", "2h_before_first_race"),
        RefreshRequest(meeting_id, first_race - timedelta(hours=1), "medium", "1h_before_first_race"),
    ]
    requests.extend(
        RefreshRequest(meeting_id, race_time - timedelta(minutes=15), "high", "15m_before_race")
        for race_time in race_times
    )

    hourly_start = morning_scratching_check + timedelta(hours=1)
    hourly_end = last_race + timedelta(minutes=30)
    hourly_due = hourly_start
    while hourly_due <= hourly_end:
        requests.append(RefreshRequest(meeting_id, hourly_due, "medium", "race_day_hourly_refresh"))
        hourly_due += timedelta(hours=1)
    return requests


def coalesce_refreshes(
    requests: list[RefreshRequest],
    medium_window_minutes: int = 45,
    medium_max_shift_minutes: int = 20,
    high_window_minutes: int = 10,
) -> list[dict]:
    remaining = sorted(requests, key=lambda item: item.due_at)
    batches = []
    while remaining:
        current = remaining.pop(0)
        window = high_window_minutes if current.priority == "high" else medium_window_minutes
        group = [current]
        keep = []
        for request in remaining:
            if request.priority == current.priority and abs((request.due_at - current.due_at).total_seconds()) <= window * 60:
                group.append(request)
            else:
                keep.append(request)
        remaining = keep
        if current.priority == "high":
            run_at = min(item.due_at for item in group)
        else:
            earliest = min(item.due_at for item in group)
            latest = max(item.due_at for item in group)
            midpoint = earliest + (latest - earliest) / 2
            max_shift = timedelta(minutes=medium_max_shift_minutes)
            run_at = max(earliest - max_shift, min(latest + max_shift, midpoint))
        batches.append(
            {
                "run_at": run_at,
                "priority": current.priority,
                "meeting_ids": sorted({item.meeting_id for item in group}),
                "reasons": sorted({item.reason for item in group}),
            }
        )
    return sorted(batches, key=lambda item: item["run_at"])


def respects_minimum_interval(last_refresh_at: datetime | None, now: datetime, minimum_minutes: int) -> bool:
    return last_refresh_at is None or now - last_refresh_at >= timedelta(minutes=minimum_minutes)
