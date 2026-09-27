import faulthandler
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from database import Base, SessionLocal, engine
from loveracing_importer.services import cleanup_loveracing_import_artifacts, maybe_run_due_auto_load
from models import Alias, Absence, AuditEvent, FramePullImport, Horse, HorsePerson, Image, LoveracingImportRun, ManualAttendee, PendingImage, Person, PersonRole, PersonStable, Race, RaceCourse, RaceEntry, RaceMeeting, Role, Stable, SystemSetting, TrackAlias
from routers import admin, frame_pull, horses, ingest, loveracing, people, race, state
from services.backup_service import maybe_create_weekly_auto_backup
from services.ingest_service import backfill_image_hashes
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
IMAGES_DIR = BASE_DIR / "images"
PENDING_IMAGES_DIR = BASE_DIR / "pending_images"
HELP_MEDIA_DIR = BASE_DIR / "help"

app = FastAPI(title="Race Day People Intelligence System")
faulthandler.enable()


def ensure_schema_updates():
    inspector = inspect(engine)
    if "race_entries" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("race_entries")}
    with engine.begin() as connection:
        if "horse_number" not in columns:
            connection.execute(text("ALTER TABLE race_entries ADD COLUMN horse_number INTEGER"))
        if "additional_trainer_ids" not in columns:
            connection.execute(text("ALTER TABLE race_entries ADD COLUMN additional_trainer_ids TEXT"))
    if "absences" in inspector.get_table_names():
        absence_columns = {column["name"] for column in inspector.get_columns("absences")}
        with engine.begin() as connection:
            if "track" not in absence_columns:
                connection.execute(text("ALTER TABLE absences ADD COLUMN track VARCHAR(255)"))
            if engine.dialect.name == "postgresql":
                connection.execute(text("ALTER TABLE absences DROP CONSTRAINT IF EXISTS uq_absences_person_date"))
                connection.execute(text("ALTER TABLE absences DROP CONSTRAINT IF EXISTS uq_absences_person_date_track"))
                connection.execute(text("ALTER TABLE absences ADD CONSTRAINT uq_absences_person_date_track UNIQUE (person_id, date, track)"))
    if "images" in inspector.get_table_names():
        image_columns = {column["name"] for column in inspector.get_columns("images")}
        with engine.begin() as connection:
            if "thumbnail_path" not in image_columns:
                connection.execute(text("ALTER TABLE images ADD COLUMN thumbnail_path VARCHAR(500)"))
            if "file_hash" not in image_columns:
                connection.execute(text("ALTER TABLE images ADD COLUMN file_hash VARCHAR(64)"))
            if "source_region" not in image_columns:
                connection.execute(text("ALTER TABLE images ADD COLUMN source_region VARCHAR(50)"))
            if "deleted_at" not in image_columns:
                connection.execute(text("ALTER TABLE images ADD COLUMN deleted_at TIMESTAMP"))
            if "deleted_by_instance" not in image_columns:
                connection.execute(text("ALTER TABLE images ADD COLUMN deleted_by_instance VARCHAR(100)"))
            if "delete_reason" not in image_columns:
                connection.execute(text("ALTER TABLE images ADD COLUMN delete_reason TEXT"))
    if "people" in inspector.get_table_names():
        person_columns = {column["name"] for column in inspector.get_columns("people")}
        with engine.begin() as connection:
            if "source_region" not in person_columns:
                connection.execute(text("ALTER TABLE people ADD COLUMN source_region VARCHAR(50)"))
            if "visible_regions" not in person_columns:
                connection.execute(text("ALTER TABLE people ADD COLUMN visible_regions TEXT"))
            if "deleted_at" not in person_columns:
                connection.execute(text("ALTER TABLE people ADD COLUMN deleted_at TIMESTAMP"))
            if "deleted_by_instance" not in person_columns:
                connection.execute(text("ALTER TABLE people ADD COLUMN deleted_by_instance VARCHAR(100)"))
            if "delete_reason" not in person_columns:
                connection.execute(text("ALTER TABLE people ADD COLUMN delete_reason TEXT"))
            connection.execute(
                text("UPDATE people SET visible_regions = source_region WHERE visible_regions IS NULL AND source_region IS NOT NULL")
            )
    if "races" in inspector.get_table_names():
        race_columns = {column["name"] for column in inspector.get_columns("races")}
        with engine.begin() as connection:
            if "meeting_id" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN meeting_id INTEGER"))
            if "source" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN source VARCHAR(100)"))
            if "source_hash" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN source_hash VARCHAR(64)"))
            if "source_region" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN source_region VARCHAR(50)"))
            if "start_time" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN start_time VARCHAR(20)"))
            if "race_name" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN race_name VARCHAR(255)"))
            if "conditions" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN conditions VARCHAR(255)"))
            if "distance_m" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN distance_m INTEGER"))
            if "stake_text" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN stake_text VARCHAR(100)"))
            if "status" not in race_columns:
                connection.execute(text("ALTER TABLE races ADD COLUMN status VARCHAR(100)"))
    if "race_entries" in inspector.get_table_names():
        entry_columns = {column["name"] for column in inspector.get_columns("race_entries")}
        with engine.begin() as connection:
            entry_updates = {
                "source_hash": "VARCHAR(64)",
                "horse_age_sex_breeding_text": "TEXT",
                "form": "VARCHAR(100)",
                "barrier": "INTEGER",
                "rating": "INTEGER",
                "weight": "VARCHAR(50)",
                "jockey_claim_text": "VARCHAR(100)",
                "win_odds": "VARCHAR(50)",
                "place_odds": "VARCHAR(50)",
                "status": "VARCHAR(100)",
                "is_scratched": "BOOLEAN DEFAULT FALSE NOT NULL",
                "is_ballot": "BOOLEAN DEFAULT FALSE NOT NULL",
                "silk_image_url": "VARCHAR(1000)",
                "silk_description": "VARCHAR(500)",
            }
            for column_name, column_type in entry_updates.items():
                if column_name not in entry_columns:
                    connection.execute(text(f"ALTER TABLE race_entries ADD COLUMN {column_name} {column_type}"))
    if "manual_attendees" in inspector.get_table_names():
        manual_columns = {column["name"] for column in inspector.get_columns("manual_attendees")}
        with engine.begin() as connection:
            if "source_region" not in manual_columns:
                connection.execute(text("ALTER TABLE manual_attendees ADD COLUMN source_region VARCHAR(50)"))
    if "pending_images" in inspector.get_table_names():
        pending_columns = {column["name"] for column in inspector.get_columns("pending_images")}
        with engine.begin() as connection:
            pending_updates = {
                "original_filename": "VARCHAR(500)",
                "file_path": "VARCHAR(1000)",
                "file_hash": "VARCHAR(64)",
                "uploading_instance_id": "VARCHAR(100)",
                "uploading_instance_name": "VARCHAR(255)",
                "uploading_region": "VARCHAR(50)",
                "intended_region": "VARCHAR(50)",
                "status": "VARCHAR(50) DEFAULT 'pending' NOT NULL",
                "notes": "TEXT",
                "processed_person_id": "INTEGER",
                "processed_image_id": "INTEGER",
                "claimed_by_instance": "VARCHAR(100)",
                "created_at": "TIMESTAMP DEFAULT NOW() NOT NULL",
                "updated_at": "TIMESTAMP DEFAULT NOW() NOT NULL",
            }
            for column_name, column_type in pending_updates.items():
                if column_name not in pending_columns:
                    connection.execute(text(f"ALTER TABLE pending_images ADD COLUMN {column_name} {column_type}"))
    if "racecourses" in inspector.get_table_names():
        course_columns = {column["name"] for column in inspector.get_columns("racecourses")}
        with engine.begin() as connection:
            if "source_region" not in course_columns:
                connection.execute(text("ALTER TABLE racecourses ADD COLUMN source_region VARCHAR(50)"))
            if engine.dialect.name == "postgresql":
                seed_courses = ["Cambridge Synthetic", "Te Aroha", "Matamata", "Te Rapa", "Tauranga", "Rotorua", "Ellerslie", "Pukekohe"]
                for course in seed_courses:
                    connection.execute(
                        text(
                            "INSERT INTO racecourses (name, loveracing_name, source_region, is_local_tagged, active, created_at, updated_at) "
                            "VALUES (:name, :loveracing_name, 'Northern', TRUE, TRUE, NOW(), NOW()) "
                            "ON CONFLICT (name) DO NOTHING"
                        ),
                        {"name": course, "loveracing_name": course},
                    )

Base.metadata.create_all(bind=engine)
ensure_schema_updates()

with SessionLocal() as cleanup_db:
    try:
        cleanup_loveracing_import_artifacts(cleanup_db)
        backfill_image_hashes(cleanup_db)
        cleanup_db.commit()
    except Exception:
        cleanup_db.rollback()

IMAGES_DIR.mkdir(exist_ok=True)
PENDING_IMAGES_DIR.mkdir(exist_ok=True)
HELP_MEDIA_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)


def loveracing_auto_loader_loop():
    while True:
        db = SessionLocal()
        try:
            maybe_run_due_auto_load(db)
        except Exception:
            db.rollback()
        finally:
            db.close()
        time.sleep(600)


def weekly_backup_loop():
    while True:
        db = SessionLocal()
        try:
            maybe_create_weekly_auto_backup(db)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        time.sleep(3600)


@app.on_event("startup")
def start_background_workers():
    loveracing_thread = threading.Thread(target=loveracing_auto_loader_loop, daemon=True)
    loveracing_thread.start()
    backup_thread = threading.Thread(target=weekly_backup_loop, daemon=True)
    backup_thread.start()


@app.get("/ingest", include_in_schema=False)
def ingest_page():
    return FileResponse(STATIC_DIR / "ingest.html")


@app.get("/bulk-ingest", include_in_schema=False)
def bulk_ingest_page():
    return FileResponse(STATIC_DIR / "bulk_ingest.html")


@app.get("/records", include_in_schema=False)
def records_page():
    return FileResponse(STATIC_DIR / "records.html")


@app.get("/frame-pull", include_in_schema=False)
def frame_pull_page():
    return FileResponse(STATIC_DIR / "frame_pull.html")


@app.get("/race-ingest", include_in_schema=False)
def race_ingest_page():
    return FileResponse(STATIC_DIR / "race_ingest.html")


@app.get("/settings", include_in_schema=False)
def settings_page():
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/loveracing", include_in_schema=False)
def loveracing_page():
    return FileResponse(STATIC_DIR / "loveracing.html")


@app.get("/help", include_in_schema=False)
def help_page():
    return FileResponse(STATIC_DIR / "help.html")


@app.get("/master-admin", include_in_schema=False)
def master_admin_page():
    return FileResponse(STATIC_DIR / "master_admin.html")


@app.get("/operator", include_in_schema=False)
def operator_page():
    return FileResponse(STATIC_DIR / "operator.html")


@app.get("/horses", include_in_schema=False)
def horses_page():
    return FileResponse(STATIC_DIR / "horses.html")


@app.get("/race-management", include_in_schema=False)
def race_management_page():
    return FileResponse(STATIC_DIR / "race_management.html")


@app.get("/api/debug/routes")
def debug_routes():
    return {"routes": sorted(route.path for route in app.routes)}


app.include_router(people.router)
app.include_router(ingest.router)
app.include_router(frame_pull.router)
app.include_router(race.router)
app.include_router(loveracing.router)
app.include_router(state.router)
app.include_router(horses.router)
app.include_router(admin.router)

app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")
app.mount("/pending_images", StaticFiles(directory=str(PENDING_IMAGES_DIR)), name="pending-images")
app.mount("/help-media", StaticFiles(directory=str(HELP_MEDIA_DIR)), name="help-media")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static-root")




