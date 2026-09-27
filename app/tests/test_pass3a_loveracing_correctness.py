import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = "sqlite://"

from loveracing_importer.models import FetchResult, ParsedMeeting, ParsedRace, ParsedRunner
from loveracing_importer.parser import parse_meeting_html
from loveracing_importer import services


NOMINATIONS_SNAPSHOT = ROOT / "loveracing_snapshots" / "20260823-170221-55925-e8d8fdcfcefc8ff7.html"
MEETING_URL = "https://loveracing.nz/RaceInfo/55925/Meeting-Overview.aspx"


def course(source_region=None):
    return types.SimpleNamespace(
        id=7,
        name="Riccarton Park Synthetic",
        loveracing_name="Riccarton Park Synthetic",
        short_code=None,
        source_region=source_region,
        is_local_tagged=True,
        active=True,
    )


class _Query:
    def __init__(self, first=None, rows=None):
        self._first = first
        self._rows = rows or []

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class _Db:
    def __init__(self, first=None, rows=None):
        self.first = first
        self.rows = rows or []
        self.commits = 0
        self.rollbacks = 0

    def query(self, *_args):
        return _Query(self.first, self.rows)

    def add(self, *_args):
        pass

    def flush(self):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class RegionStabilityTest(unittest.TestCase):
    def test_null_course_is_unassigned_on_northern_and_christchurch_instances(self):
        item = course(None)
        with patch.object(services, "default_region", return_value="Northern"):
            northern = services._racecourse_status_payload(item)
        with patch.object(services, "default_region", return_value="Christchurch"):
            christchurch = services._racecourse_status_payload(item)

        self.assertEqual(northern, christchurch)
        self.assertIsNone(northern["source_region"])
        self.assertEqual(northern["region_state"], "unassigned")

    def test_explicit_course_region_is_stable_on_every_instance(self):
        item = course("Central")
        with patch.object(services, "default_region", return_value="Northern"):
            northern = services._racecourse_status_payload(item)
        with patch.object(services, "default_region", return_value="Dunedin"):
            dunedin = services._racecourse_status_payload(item)

        self.assertEqual(northern["source_region"], "Central")
        self.assertEqual(northern, dunedin)

    def test_null_region_is_persisted_without_defaulting(self):
        item = course("Northern")
        db = _Db(first=item)
        fake_model = types.SimpleNamespace(id=object(), name=object())
        with patch.object(services, "RaceCourse", fake_model), patch.object(services, "default_region", return_value="Christchurch"):
            services.upsert_racecourse(db, {"id": item.id, "name": item.name, "source_region": None})
        self.assertIsNone(item.source_region)

    def test_seeded_course_is_created_unassigned_not_from_instance_default(self):
        created = []
        db = _Db(first=None)
        db.add = created.append
        fake_model = type("FakeRaceCourse", (), {"name": object(), "__init__": lambda self, **values: self.__dict__.update(values)})
        with patch.object(services, "RaceCourse", fake_model), patch.object(services, "LOCAL_COURSE_SEEDS", ["Matamata"]), patch.object(
            services, "default_region", return_value="Christchurch"
        ):
            services.seed_local_racecourses(db)
        self.assertEqual(len(created), 1)
        self.assertIsNone(created[0].source_region)

    def test_unassigned_candidate_cannot_enter_auto_import(self):
        candidate = {"source_url": MEETING_URL, "is_tagged": False, "is_assigned": False, "status": "unassigned"}
        discovery = {"status": "ok", "candidates": [candidate], "candidate_count": 1, "matched_count": 0}
        with patch.object(services, "discover_calendar_candidates", return_value=discovery), patch.object(services, "import_meeting_url") as importer:
            result = services.discover_and_import(object(), auto_import=True)
        importer.assert_not_called()
        self.assertEqual(result["imports"], [])

    def test_candidate_region_does_not_depend_on_instance_default(self):
        parsed = ParsedMeeting(stage="fields", meeting_name="Canterbury JC @ Riccarton Park Synthetic")
        meeting = types.SimpleNamespace(label="Riccarton", source_url=MEETING_URL)
        db = object()
        with patch.object(services, "_inspect_discovered_meeting", return_value=(parsed, [])), patch.object(services, "_existing_meeting_payload", return_value=None):
            with patch.object(services, "default_region", return_value="Northern"):
                northern = services._calendar_candidate(db, meeting, [course(None)])
            with patch.object(services, "default_region", return_value="Christchurch"):
                christchurch = services._calendar_candidate(db, meeting, [course(None)])
        self.assertEqual(northern, christchurch)
        self.assertEqual(northern["status"], "unassigned")
        self.assertFalse(northern["is_tagged"])
        self.assertIsNone(northern["region"])

    def test_remediation_suggests_only_unanimous_historical_region(self):
        item = course(None)
        agreeing = _Db(rows=[types.SimpleNamespace(track=item.name, source_region="Christchurch") for _ in range(3)])
        conflicting = _Db(rows=[
            types.SimpleNamespace(track=item.name, source_region="Christchurch"),
            types.SimpleNamespace(track=item.name, source_region="Northern"),
        ])
        self.assertEqual(services._racecourse_region_remediation(agreeing, item)["suggested_region"], "Christchurch")
        self.assertIsNone(services._racecourse_region_remediation(conflicting, item)["suggested_region"])


class DiscoveryAndPermissionTest(unittest.TestCase):
    def test_check_calendar_is_discovery_only_even_when_payload_mentions_auto_import(self):
        backend = (ROOT / "routers" / "loveracing.py").read_text(encoding="utf-8")
        discover_route = backend[backend.index('@router.post("/discover")') : backend.index('@router.delete("/meetings/')]
        self.assertIn("return discover_calendar_candidates(db)", discover_route)
        self.assertNotIn("discover_and_import", discover_route)
        self.assertNotIn("require_edit_pin", discover_route)
        self.assertNotIn("require_master_admin_pin", discover_route)
        discovery_service = (ROOT / "loveracing_importer" / "services.py").read_text(encoding="utf-8")
        discovery_service = discovery_service[
            discovery_service.index("def discover_calendar_candidates") : discovery_service.index("def discover_and_import")
        ]
        self.assertNotIn("seed_local_racecourses", discovery_service)
        self.assertNotIn("import_meeting_url", discovery_service)

    def test_course_save_triggers_immediate_discovery_only_rematch(self):
        with patch.object(services, "upsert_racecourse", return_value={"status": "ok"}), patch.object(
            services, "discover_calendar_candidates", return_value={"status": "ok", "candidates": [{"status": "ready"}]}
        ) as discovery, patch.object(services, "import_meeting_url") as importer:
            result = services.upsert_racecourse_and_rematch(object(), {"name": "New Course", "source_region": "Central"})
        discovery.assert_called_once()
        importer.assert_not_called()
        self.assertEqual(result["rematch"]["candidates"][0]["status"], "ready")

    def test_safely_assigned_import_is_pin_free_but_ambiguous_import_checks_edit(self):
        assigned = {"source_url": MEETING_URL, "is_assigned": True, "region": "Christchurch"}
        unassigned = {"source_url": MEETING_URL, "is_assigned": False, "region": None}
        with patch.object(services, "last_calendar_candidates", return_value=[assigned]):
            self.assertFalse(services.import_requires_edit(object(), MEETING_URL, "Christchurch"))
            self.assertTrue(services.import_requires_edit(object(), MEETING_URL, "Northern"))
        with patch.object(services, "last_calendar_candidates", return_value=[unassigned]):
            self.assertTrue(services.import_requires_edit(object(), MEETING_URL, None))

    def test_permission_and_discovery_frontend_contracts(self):
        backend = (ROOT / "routers" / "loveracing.py").read_text(encoding="utf-8")
        frontend = (ROOT / "static" / "loveracing.html").read_text(encoding="utf-8")
        self.assertIn("Depends(require_edit_pin)", backend)
        self.assertIn("Depends(require_master_admin_pin)", backend)
        self.assertIn("if import_requires_edit", backend)
        self.assertNotIn('body: JSON.stringify({ force: forceImport.checked, auto_import: autoImport.checked })', frontend)
        self.assertIn('body: JSON.stringify({ discovery_only: true })', frontend)
        self.assertIn("requireEditPin", frontend)
        self.assertIn("requireMasterPin(\"Enter Master PIN to undo", frontend)


class NominationsStageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = parse_meeting_html(NOMINATIONS_SNAPSHOT.read_text(encoding="utf-8", errors="replace"), MEETING_URL)

    def test_real_riccarton_snapshot_uses_nomination_columns(self):
        self.assertEqual(self.parsed.stage, "nominations")
        self.assertEqual(self.parsed.races[0].status, "nominations")
        first = self.parsed.races[0].runners[0]
        self.assertEqual(first.horse_name, "O'Riordan")
        self.assertEqual(first.trainer_name, "Stephanie Faulkner")
        self.assertEqual(first.rating, 75)
        self.assertIsNone(first.jockey_name)

    def test_nomination_metadata_never_becomes_a_horse_or_jockey(self):
        runners = [runner for race in self.parsed.races for runner in race.runners]
        forbidden = {"colt", "gelding", "filly", "mare", "-", "58.00", "57.50", "59.00"}
        self.assertTrue(runners)
        self.assertFalse({runner.horse_name.lower() for runner in runners} & forbidden)
        self.assertTrue(all(runner.jockey_name is None for runner in runners))

    def test_nominations_skip_detail_fallback_and_live_database_apply(self):
        parsed = ParsedMeeting(
            stage="nominations",
            source_url=MEETING_URL,
            races=[ParsedRace(race_number=1, status="nominations", runners=[ParsedRunner(1, "Actual Horse", trainer_name="Actual Trainer")])],
        )
        db = _Db()
        meeting = types.SimpleNamespace(backoff_until=None, content_hash=None, id=1, race_count=0)
        fetch = FetchResult(success=True, source_url=MEETING_URL, status=200, html="nominations", content_hash="hash")
        self.assertFalse(services._should_fetch_detail_pages(parsed))
        with patch.object(services, "_meeting_row", return_value=meeting), patch.object(services, "fetch_meeting_html", return_value=fetch), patch.object(
            services, "parse_meeting_html", return_value=parsed
        ), patch.object(services, "_apply_meeting") as apply_meeting:
            result = services.import_meeting_url(db, MEETING_URL, source_region="Christchurch")
        apply_meeting.assert_not_called()
        self.assertEqual(result["status"], "nominations")
        self.assertEqual(db.rollbacks, 1)

    def test_later_fields_stage_remains_compatible(self):
        html = """
        <html><body><h1>Canterbury JC @ Riccarton Park Synthetic</h1><p>Wednesday, 26 Aug 2026</p>
        <a class="race-toggle toggle" href="#toggle-detail1">Open /Close</a>
        <table id="toggle-detail1"><tr><th>#</th><th>Form</th><th>Horse</th><th>Jockey</th><th>Trainer</th></tr>
        <tr><td>1</td><td>123</td><td>Declared Runner</td><td>Declared Jockey</td><td>Declared Trainer</td></tr></table>
        </body></html>
        """
        parsed = parse_meeting_html(html, MEETING_URL)
        self.assertEqual(parsed.stage, "fields")
        self.assertEqual(parsed.races[0].runners[0].horse_name, "Declared Runner")
        self.assertEqual(parsed.races[0].runners[0].jockey_name, "Declared Jockey")


if __name__ == "__main__":
    unittest.main()
