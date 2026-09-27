from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from database import Base


class Person(Base):
    __tablename__ = "people"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    notes = Column(Text, nullable=True)
    preferred_image_id = Column(Integer, ForeignKey("images.id"), nullable=True)
    source_region = Column(String(50), nullable=True, index=True)
    visible_regions = Column(Text, nullable=True)
    deleted_at = Column(DateTime, nullable=True, index=True)
    deleted_by_instance = Column(String(100), nullable=True)
    delete_reason = Column(Text, nullable=True)

    aliases = relationship("Alias", back_populates="person", cascade="all, delete-orphan")
    images = relationship(
        "Image",
        back_populates="person",
        cascade="all, delete-orphan",
        foreign_keys="Image.person_id",
        order_by="desc(Image.created_at)",
    )
    preferred_image = relationship("Image", foreign_keys=[preferred_image_id], post_update=True)
    role_assignments = relationship("PersonRole", back_populates="person", cascade="all, delete-orphan")
    stable_assignments = relationship("PersonStable", back_populates="person", cascade="all, delete-orphan")
    horse_links = relationship("HorsePerson", back_populates="person", cascade="all, delete-orphan")
    absences = relationship("Absence", back_populates="person", cascade="all, delete-orphan")
    manual_attendees = relationship("ManualAttendee", back_populates="person", cascade="all, delete-orphan")


class Alias(Base):
    __tablename__ = "aliases"
    __table_args__ = (UniqueConstraint("person_id", "alias", name="uq_aliases_person_alias"),)

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    alias = Column(String(255), nullable=False, index=True)

    person = relationship("Person", back_populates="aliases")


class Image(Base):
    __tablename__ = "images"

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    file_path = Column(String(500), nullable=False)
    thumbnail_path = Column(String(500), nullable=True)
    file_hash = Column(String(64), nullable=True, index=True)
    source_region = Column(String(50), nullable=True, index=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    is_preferred = Column(Boolean, default=False, nullable=False)
    location_taken = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    deleted_at = Column(DateTime, nullable=True, index=True)
    deleted_by_instance = Column(String(100), nullable=True)
    delete_reason = Column(Text, nullable=True)

    person = relationship("Person", back_populates="images", foreign_keys=[person_id])


class FramePullImport(Base):
    """A durable, idempotent link from a Frame Pull result to a People image."""

    __tablename__ = "frame_pull_imports"
    __table_args__ = (UniqueConstraint("source_kind", "source_id", name="uq_frame_pull_import_source"),)

    id = Column(Integer, primary_key=True)
    source_kind = Column(String(32), nullable=False, default="group")
    source_id = Column(Integer, nullable=False, index=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    image_id = Column(Integer, ForeignKey("images.id", ondelete="SET NULL"), nullable=True)
    original_name = Column(String(255), nullable=True)
    race_day = Column(String(64), nullable=True)
    track = Column(String(255), nullable=True)
    role_context = Column(Text, nullable=True)
    source_metadata = Column(Text, nullable=False, default="{}")
    imported_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    acknowledged_at = Column(DateTime, nullable=True)
    acknowledgement_error = Column(Text, nullable=True)

    person = relationship("Person", foreign_keys=[person_id])
    image = relationship("Image", foreign_keys=[image_id])


class PendingImage(Base):
    __tablename__ = "pending_images"

    id = Column(Integer, primary_key=True)
    original_filename = Column(String(500), nullable=False)
    file_path = Column(String(1000), nullable=False)
    file_hash = Column(String(64), nullable=False, index=True)
    uploading_instance_id = Column(String(100), nullable=True, index=True)
    uploading_instance_name = Column(String(255), nullable=True)
    uploading_region = Column(String(50), nullable=True, index=True)
    intended_region = Column(String(50), nullable=True, index=True)
    status = Column(String(50), default="pending", nullable=False, index=True)
    notes = Column(Text, nullable=True)
    processed_person_id = Column(Integer, ForeignKey("people.id"), nullable=True)
    processed_image_id = Column(Integer, ForeignKey("images.id"), nullable=True)
    claimed_by_instance = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False, index=True)

    assignments = relationship("PersonRole", back_populates="role", cascade="all, delete-orphan")


class PersonRole(Base):
    __tablename__ = "person_roles"

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)

    person = relationship("Person", back_populates="role_assignments")
    role = relationship("Role", back_populates="assignments")


class Stable(Base):
    __tablename__ = "stables"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False, index=True)

    people = relationship("PersonStable", back_populates="stable", cascade="all, delete-orphan")
    horses = relationship("Horse", back_populates="stable")


class PersonStable(Base):
    __tablename__ = "person_stables"

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    stable_id = Column(Integer, ForeignKey("stables.id", ondelete="CASCADE"), nullable=False, index=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)

    person = relationship("Person", back_populates="stable_assignments")
    stable = relationship("Stable", back_populates="people")


class Horse(Base):
    __tablename__ = "horses"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    stable_id = Column(Integer, ForeignKey("stables.id"), nullable=True)
    notes = Column(Text, nullable=True)

    stable = relationship("Stable", back_populates="horses")
    people = relationship("HorsePerson", back_populates="horse", cascade="all, delete-orphan")
    race_entries = relationship("RaceEntry", back_populates="horse")


class HorsePerson(Base):
    __tablename__ = "horse_people"

    id = Column(Integer, primary_key=True)
    horse_id = Column(Integer, ForeignKey("horses.id", ondelete="CASCADE"), nullable=False, index=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(100), nullable=False)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)

    horse = relationship("Horse", back_populates="people")
    person = relationship("Person", back_populates="horse_links")


class Race(Base):
    __tablename__ = "races"
    __table_args__ = (UniqueConstraint("track", "race_number", "date", name="uq_races_track_number_date"),)

    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("race_meetings.id", ondelete="SET NULL"), nullable=True, index=True)
    source = Column(String(100), nullable=True, index=True)
    source_hash = Column(String(64), nullable=True)
    track = Column(String(255), nullable=False, index=True)
    source_region = Column(String(50), nullable=True, index=True)
    race_number = Column(Integer, nullable=False)
    date = Column(Date, nullable=False, index=True)
    start_time = Column(String(20), nullable=True)
    race_name = Column(String(255), nullable=True)
    conditions = Column(String(255), nullable=True)
    distance_m = Column(Integer, nullable=True)
    stake_text = Column(String(100), nullable=True)
    status = Column(String(100), nullable=True)

    meeting = relationship("RaceMeeting", back_populates="races")
    entries = relationship("RaceEntry", back_populates="race", cascade="all, delete-orphan")


class RaceEntry(Base):
    __tablename__ = "race_entries"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id", ondelete="CASCADE"), nullable=False, index=True)
    horse_id = Column(Integer, ForeignKey("horses.id"), nullable=False, index=True)
    jockey_id = Column(Integer, ForeignKey("people.id"), nullable=True, index=True)
    trainer_id = Column(Integer, ForeignKey("people.id"), nullable=True, index=True)
    additional_trainer_ids = Column(Text, nullable=True)
    horse_number = Column(Integer, nullable=True)
    source_hash = Column(String(64), nullable=True)
    horse_age_sex_breeding_text = Column(Text, nullable=True)
    form = Column(String(100), nullable=True)
    barrier = Column(Integer, nullable=True)
    rating = Column(Integer, nullable=True)
    weight = Column(String(50), nullable=True)
    jockey_claim_text = Column(String(100), nullable=True)
    win_odds = Column(String(50), nullable=True)
    place_odds = Column(String(50), nullable=True)
    status = Column(String(100), nullable=True)
    is_scratched = Column(Boolean, default=False, nullable=False)
    is_ballot = Column(Boolean, default=False, nullable=False)
    silk_image_url = Column(String(1000), nullable=True)
    silk_description = Column(String(500), nullable=True)

    race = relationship("Race", back_populates="entries")
    horse = relationship("Horse", back_populates="race_entries")
    jockey = relationship("Person", foreign_keys=[jockey_id])
    trainer = relationship("Person", foreign_keys=[trainer_id])


class ManualAttendee(Base):
    __tablename__ = "manual_attendees"
    __table_args__ = (UniqueConstraint("person_id", "date", "track", "race_id", name="uq_manual_attendees_scope"),)

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    track = Column(String(255), nullable=False, index=True)
    source_region = Column(String(50), nullable=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id", ondelete="CASCADE"), nullable=True, index=True)
    note = Column(String(255), nullable=True)

    person = relationship("Person", back_populates="manual_attendees")
    race = relationship("Race")


class Absence(Base):
    __tablename__ = "absences"
    __table_args__ = (UniqueConstraint("person_id", "date", "track", name="uq_absences_person_date_track"),)

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    track = Column(String(255), nullable=True, index=True)
    reason = Column(String(255), nullable=True)

    person = relationship("Person", back_populates="absences")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    instance_id = Column(String(100), nullable=True, index=True)
    instance_name = Column(String(255), nullable=True)
    actor = Column(String(100), nullable=True)
    action = Column(String(100), nullable=False, index=True)
    entity_type = Column(String(100), nullable=False, index=True)
    entity_id = Column(Integer, nullable=True, index=True)
    before_json = Column(Text, nullable=True)
    after_json = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)


class TrackAlias(Base):
    __tablename__ = "track_aliases"

    id = Column(Integer, primary_key=True)
    alias = Column(String(255), unique=True, nullable=False, index=True)
    canonical_track = Column(String(255), nullable=False, index=True)
    course_name = Column(String(255), nullable=True)
    club_name = Column(String(255), nullable=True)
    location_name = Column(String(255), nullable=True)
    source = Column(String(100), nullable=True)


class RaceCourse(Base):
    __tablename__ = "racecourses"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    loveracing_name = Column(String(255), nullable=True, index=True)
    short_code = Column(String(50), nullable=True)
    source_region = Column(String(50), nullable=True, index=True)
    is_local_tagged = Column(Boolean, default=False, nullable=False, index=True)
    active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class RaceMeeting(Base):
    __tablename__ = "race_meetings"
    __table_args__ = (UniqueConstraint("source", "source_meeting_id", name="uq_race_meetings_source_id"),)

    id = Column(Integer, primary_key=True)
    source = Column(String(100), nullable=False, default="loveracing", index=True)
    source_meeting_id = Column(String(100), nullable=True, index=True)
    source_url = Column(String(1000), nullable=False)
    date = Column(Date, nullable=True, index=True)
    venue_name = Column(String(255), nullable=True, index=True)
    club_name = Column(String(255), nullable=True)
    meeting_name = Column(String(255), nullable=True)
    race_count = Column(Integer, nullable=True)
    going = Column(String(100), nullable=True)
    weather = Column(String(100), nullable=True)
    rail = Column(Text, nullable=True)
    track_direction = Column(String(100), nullable=True)
    straight_length = Column(String(100), nullable=True)
    loveracing_last_updated_at = Column(DateTime, nullable=True)
    import_status = Column(String(100), nullable=True, index=True)
    content_hash = Column(String(64), nullable=True)
    raw_html_path = Column(String(1000), nullable=True)
    warnings_json = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    next_scheduled_refresh_at = Column(DateTime, nullable=True)
    backoff_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    races = relationship("Race", back_populates="meeting")
    import_runs = relationship("LoveracingImportRun", back_populates="meeting")


class LoveracingImportRun(Base):
    __tablename__ = "loveracing_import_runs"

    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("race_meetings.id", ondelete="SET NULL"), nullable=True, index=True)
    source_url = Column(String(1000), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    success = Column(Boolean, default=False, nullable=False, index=True)
    http_status = Column(Integer, nullable=True)
    content_hash = Column(String(64), nullable=True)
    raw_html_path = Column(String(1000), nullable=True)
    parser_version = Column(String(50), nullable=True)
    races_found = Column(Integer, nullable=True)
    runners_found = Column(Integer, nullable=True)
    warnings_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    meeting = relationship("RaceMeeting", back_populates="import_runs")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


