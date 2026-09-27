from dataclasses import dataclass, field
from datetime import date as dt_date, datetime


@dataclass
class ParsedRunner:
    runner_number: int | None = None
    horse_name: str | None = None
    horse_age_sex_breeding_text: str | None = None
    form: str | None = None
    barrier: int | None = None
    rating: int | None = None
    weight: str | None = None
    jockey_name: str | None = None
    jockey_claim_text: str | None = None
    trainer_name: str | None = None
    win_odds: str | None = None
    place_odds: str | None = None
    status: str | None = None
    is_scratched: bool = False
    is_ballot: bool = False
    silk_image_url: str | None = None
    silk_description: str | None = None


@dataclass
class ParsedRace:
    race_number: int
    source_detail_url: str | None = None
    start_time: str | None = None
    race_name: str | None = None
    conditions: str | None = None
    distance_m: int | None = None
    stake_text: str | None = None
    status: str | None = None
    runners: list[ParsedRunner] = field(default_factory=list)
    scratched_horse_names: list[str] = field(default_factory=list)


@dataclass
class ParsedMeeting:
    source: str = "loveracing"
    stage: str = "fields"
    source_url: str | None = None
    source_meeting_id: str | None = None
    date: dt_date | None = None
    venue_name: str | None = None
    club_name: str | None = None
    meeting_name: str | None = None
    last_updated: datetime | None = None
    going: str | None = None
    weather: str | None = None
    rail: str | None = None
    track_direction: str | None = None
    straight_length: str | None = None
    races: list[ParsedRace] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    success: bool
    source_url: str
    status: int | None = None
    html: str | None = None
    content_hash: str | None = None
    raw_html_path: str | None = None
    error_message: str | None = None
