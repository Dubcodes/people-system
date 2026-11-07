import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image as PillowImage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import FramePullImport, Image, Person
from routers import frame_pull
from services import ingest_service
from services.frame_pull_client import DownloadedPortrait, FramePullClient, FramePullOffline


def jpeg_bytes():
    output = BytesIO()
    PillowImage.new("RGB", (640, 360), "white").save(output, format="JPEG")
    return output.getvalue()


class FakeFramePullClient:
    configured = True

    def __init__(self, fail_ack=False):
        self.fail_ack = fail_ack
        self.calls = []
        self.group = {
            "id": 7, "name": "Ryan Foote", "review_status": "approved", "preferred_candidate_id": 33,
            "track": "Cambridge", "roles": ["Trainer"], "interview_count": 2, "candidate_count": 6,
            "interview_ids": [4, 5], "interviews": [{"id": 4, "role": "Trainer", "track": "Cambridge"}],
            "location_confidence": 0.9, "export_state": "pending",
        }

    def health(self):
        return {"service": "frame-pull", "version": 1}

    def list_people(self, **_kwargs):
        return [self.group]

    def list_interviews(self, **_kwargs):
        return []

    def get_person_group(self, group_id):
        self.calls.append(("group", group_id))
        return self.group

    def download_portrait(self, group_id):
        self.calls.append(("portrait", group_id))
        return DownloadedPortrait(jpeg_bytes(), "image/jpeg")

    def acknowledge_export(self, group_id):
        self.calls.append(("ack", group_id))
        if self.fail_ack:
            raise FramePullOffline("offline")
        return {"export_state": "exported"}

    def get_interview(self, interview_id):
        return {"id": interview_id, "review_status": "approved", "has_portrait": True, "final_name": "Ryan Foote"}

    def download_interview_portrait(self, interview_id):
        self.calls.append(("interview_portrait", interview_id))
        return DownloadedPortrait(jpeg_bytes(), "image/jpeg")

    def acknowledge_interview_export(self, interview_id):
        self.calls.append(("interview_ack", interview_id))
        return {"export_state": "exported"}


class FramePullInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name, 'people.sqlite').as_posix()}", future=True, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        self.db = self.Session()
        self.person = Person(name="Ryan Foote")
        self.db.add(self.person)
        self.db.commit()
        self.client_service = FakeFramePullClient()
        self.original_upload_dir = ingest_service.UPLOAD_DIR
        ingest_service.UPLOAD_DIR = Path(self.temp.name) / "images"
        app = FastAPI()
        app.include_router(frame_pull.router)
        def test_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()
        app.dependency_overrides[frame_pull.get_db] = test_db
        app.dependency_overrides[frame_pull.get_client] = lambda: self.client_service
        app.dependency_overrides[frame_pull.require_edit_pin] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        ingest_service.UPLOAD_DIR = self.original_upload_dir
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_inbox_suggests_but_does_not_auto_attach(self):
        response = self.client.get("/api/frame-pull/inbox")
        self.assertEqual(response.status_code, 200)
        row = response.json()["items"][0]
        self.assertEqual(row["import_state"], "ready")
        self.assertEqual(row["suggested_matches"], [{"id": self.person.id, "name": "Ryan Foote"}])
        self.assertEqual(self.db.query(FramePullImport).count(), 0)

    def test_inbox_query_filters_support_frame_pull_deep_link(self):
        self.assertEqual(len(self.client.get('/api/frame-pull/inbox?name=Ryan%20Foote&track=Cambridge&state=ready').json()['items']), 1)
        self.assertEqual(self.client.get('/api/frame-pull/inbox?name=Someone%20Else&state=ready').json()['items'], [])

    def test_inbox_ui_initializes_query_filters_and_preselects_one_exact_match(self):
        page=(Path(__file__).resolve().parents[1]/'static'/'frame_pull.html').read_text(encoding='utf-8')
        self.assertIn("new URLSearchParams(location.search)", page)
        self.assertIn("matches.length===1?'selected':''", page)
        self.assertIn('Preferred image', page)

    def test_import_commits_then_acknowledges_and_is_idempotent(self):
        response = self.client.post("/api/frame-pull/groups/7/import", json={"person_id": self.person.id})
        self.assertEqual(response.status_code, 200)
        saved = self.db.query(FramePullImport).one()
        self.assertEqual(saved.person_id, self.person.id)
        self.assertIsNotNone(saved.image_id)
        self.assertIsNotNone(saved.acknowledged_at)
        image = self.db.get(Image, saved.image_id)
        self.assertTrue((ingest_service.UPLOAD_DIR / str(self.person.id) / Path(image.file_path).name).exists())
        self.assertEqual([kind for kind, _ in self.client_service.calls], ["group", "portrait", "ack"])
        again = self.client.post("/api/frame-pull/groups/7/import", json={"person_id": self.person.id})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.db.query(FramePullImport).count(), 1)
        self.assertEqual([kind for kind, _ in self.client_service.calls].count("ack"), 1)

    def test_ack_failure_is_retryable_after_people_commit(self):
        self.client_service.fail_ack = True
        response = self.client.post("/api/frame-pull/groups/7/import", json={"person_id": self.person.id})
        self.assertEqual(response.status_code, 200)
        saved = self.db.query(FramePullImport).one()
        self.assertIsNone(saved.acknowledged_at)
        self.assertIsNotNone(saved.acknowledgement_error)
        self.assertEqual(self.client.get("/api/frame-pull/inbox?state=all").json()["items"][0]["import"]["acknowledgement_state"], "ack_failed")
        self.client_service.fail_ack = False
        retry = self.client.post(f"/api/frame-pull/imports/{saved.id}/retry-acknowledgement")
        self.assertEqual(retry.status_code, 200)
        self.db.expire_all()
        self.assertIsNotNone(self.db.query(FramePullImport).one().acknowledged_at)
        self.assertEqual(retry.json()["import"]["acknowledgement_state"], "acknowledged")

    def test_missing_person_never_downloads_or_acknowledges(self):
        response = self.client.post("/api/frame-pull/groups/7/import", json={"person_id": 9999})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client_service.calls, [])
        self.assertEqual(self.db.query(FramePullImport).count(), 0)

    def test_portrait_proxy_only_accepts_a_stable_group_id(self):
        response = self.client.get("/api/frame-pull/groups/7/portrait")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertIn(("portrait", 7), self.client_service.calls)

    def test_inbox_includes_an_approved_ungrouped_interview(self):
        self.client_service.list_interviews = lambda **_kwargs: [{
            "id": 9, "final_name": "Casey Jones", "trusted_name": None, "final_role": "Stable Representative",
            "final_track": "Cambridge", "review_status": "approved", "export_state": "pending", "has_portrait": True,
        }]
        response = self.client.get("/api/frame-pull/inbox?state=all")
        self.assertEqual(response.status_code, 200)
        standalone = next(item for item in response.json()["items"] if item["source_kind"] == "interview")
        self.assertEqual(standalone["id"], 9)
        self.assertEqual(standalone["import_url"], "/api/frame-pull/interviews/9/import")


class FramePullClientTests(unittest.TestCase):
    def test_client_keeps_bearer_token_server_side(self):
        class Headers:
            @staticmethod
            def get_content_type():
                return "application/json"

        class Response:
            headers = Headers()
            def read(self): return b'{"status":"ok"}'
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        client = FramePullClient(base_url="https://frame-pull.internal", token="private-token", timeout_seconds=3)
        with patch("services.frame_pull_client.urlopen", return_value=Response()) as request:
            self.assertEqual(client.health()["status"], "ok")
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_header("Authorization"), "Bearer private-token")
        self.assertNotIn("private-token", sent.full_url)


if __name__ == "__main__":
    unittest.main()
