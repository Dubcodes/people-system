from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ImageSummary(BaseModel):
    id: int
    url: str
    thumbnail_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    is_preferred: bool
    source_region: Optional[str] = None
    location_taken: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ViewerPerson(BaseModel):
    id: int
    name: str
    notes: Optional[str] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    source_region: Optional[str] = None
    visible_regions: List[str] = Field(default_factory=list)
    detected_regions: List[str] = Field(default_factory=list)
    regions: List[str] = Field(default_factory=list)
    roles: List[str]
    stables: List[str]
    aliases: List[str]
    locations: List[str] = Field(default_factory=list)
    is_absent_today: bool = False
    last_seen_date: Optional[date] = None
    is_active: bool = True


class HorseLinkSummary(BaseModel):
    id: int
    horse_id: int
    horse_name: str
    role: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    last_seen_date: Optional[date] = None
    is_active: bool = True


class TimelineItem(BaseModel):
    id: int
    name: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class PersonDetail(ViewerPerson):
    images: List[ImageSummary]
    horse_links: List[HorseLinkSummary]
    role_timeline: List[TimelineItem]
    stable_timeline: List[TimelineItem]
    absence_reason: Optional[str] = None


class RacePersonSummary(BaseModel):
    person_id: int
    name: str


class RaceEntrySummary(BaseModel):
    horse_id: int
    horse_name: str
    horse_number: Optional[int] = None
    jockey_person_id: Optional[int] = None
    jockey_name: Optional[str] = None
    trainer_name: Optional[str] = None
    trainer_people: List[RacePersonSummary] = Field(default_factory=list)
    associated_person_ids: List[int] = Field(default_factory=list)
    status: Optional[str] = None
    is_scratched: bool = False
    is_ballot: bool = False
    silk_image_url: Optional[str] = None
    silk_description: Optional[str] = None


class RaceSummary(BaseModel):
    id: int
    label: str
    track: str
    race_number: int
    date: date
    entries: List[RaceEntrySummary]


class RacePickerItem(BaseModel):
    id: int
    label: str
    race_number: int
    track: str


class ViewerOverview(BaseModel):
    pre_mode: bool = True
    selected_race_id: Optional[int] = None
    selected_track: Optional[str] = None
    race_day: Optional[date] = None
    is_fallback_day: bool = False
    current_region: str
    region_scope: str = "current"
    import_status: Optional[str] = None
    import_warning: Optional[str] = None
    loveracing_last_updated_at: Optional[datetime] = None
    tracks: List[str] = Field(default_factory=list)
    races: List[RacePickerItem]
    active_race: List[ViewerPerson]
    active_race_meta: Optional[RaceSummary] = None
    priority_people: List[ViewerPerson]
    all_people: List[ViewerPerson]


class IngestResponse(BaseModel):
    status: str
    person_id: int
    image_id: int
    image_url: str
    source_region: Optional[str] = None
    message: Optional[str] = None


class AbsenceUpdate(BaseModel):
    is_absent: bool
    reason: Optional[str] = None
    race_date: Optional[date] = None
    track: Optional[str] = None


class PersonUpdate(BaseModel):
    name: str
    notes: Optional[str] = None
    role: Optional[str] = None
    stable: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    horse_name: Optional[str] = None
    visible_regions: List[str] = Field(default_factory=list)


class PersonMergeRequest(BaseModel):
    source_person_id: int
    target_person_id: int
    note: Optional[str] = None


class ImageUpdate(BaseModel):
    notes: Optional[str] = None
    location_taken: Optional[str] = None
    is_preferred: bool = False


class ActionResponse(BaseModel):
    status: str



class TimelineUpdate(BaseModel):
    name: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class HorseLinkUpdate(BaseModel):
    horse_name: str
    role: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class RaceImportRequest(BaseModel):
    track: str
    race_number: int
    race_date: date
    raw_text: str
    source_region: Optional[str] = None


class RaceImportResponse(BaseModel):
    status: str
    race_id: int
    source_region: Optional[str] = None
    entries_created: int
    people_created: int
    horses_created: int
    scratched: List[str] = Field(default_factory=list)


class RaceParseRunner(BaseModel):
    number: int
    horse: str
    jockey: Optional[str] = None
    trainers: List[str] = Field(default_factory=list)


class RaceParsePreviewResponse(BaseModel):
    runners: List[RaceParseRunner]
    scratched: List[str] = Field(default_factory=list)
    runner_count: int
    scratched_count: int


class HorseSummary(BaseModel):
    id: int
    name: str
    stable_name: Optional[str] = None
    notes: Optional[str] = None
    race_count: int = 0
    last_seen_date: Optional[date] = None
    is_active: bool = True


class HorseRaceHistoryItem(BaseModel):
    race_id: int
    track: str
    race_number: int
    date: date
    horse_number: Optional[int] = None
    jockey_name: Optional[str] = None
    trainer_name: Optional[str] = None


class HorseAssociatedPerson(BaseModel):
    id: int
    person_id: int
    person_name: str
    role: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class HorseDetail(HorseSummary):
    race_history: List[HorseRaceHistoryItem] = Field(default_factory=list)
    associated_people: List[HorseAssociatedPerson] = Field(default_factory=list)


class HorseUpdate(BaseModel):
    name: str
    stable_name: Optional[str] = None
    notes: Optional[str] = None


class ManualAttendeeCreate(BaseModel):
    person_id: int
    track: str
    race_date: date
    race_id: Optional[int] = None
    note: Optional[str] = None
    source_region: Optional[str] = None


class ManualAttendeeSummary(BaseModel):
    id: int
    person_id: int
    person_name: str
    track: str
    race_date: date
    race_id: Optional[int] = None
    race_label: Optional[str] = None
    note: Optional[str] = None
    source_region: Optional[str] = None
