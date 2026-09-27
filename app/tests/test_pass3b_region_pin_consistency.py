import ast
import json
import types
import unittest
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]


def _normalize_region(value):
    cleaned = " ".join((value or "").split()).strip()
    return cleaned if cleaned in {"Central", "Northern", "Christchurch", "Dunedin"} else None


def _region_helpers():
    source = (ROOT / "services" / "people_service.py").read_text(encoding="utf-8")
    wanted = {"_stored_regions", "_stored_id_list", "_automatic_person_regions", "_person_regions", "_race_entry_participation_regions"}
    nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"Optional": Optional, "Person": object, "normalize_region": _normalize_region}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "people_service_region_helpers", "exec"), namespace)
    return namespace


class AutomaticParticipationRegionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _region_helpers()

    @staticmethod
    def person(**overrides):
        values = {
            "id": 100,
            "visible_regions": None,
            "source_region": None,
            "images": [],
            "manual_attendees": [],
            "horse_links": [],
        }
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def test_direct_jockey_trainer_and_co_trainer_entries_add_exact_race_regions(self):
        regions = self.helpers["_race_entry_participation_regions"](
            [
                (11, None, None, "Northern"),
                (None, 22, None, "Christchurch"),
                (None, None, "33, 34", "Dunedin"),
            ]
        )
        self.assertEqual(regions[11], {"Northern"})
        self.assertEqual(regions[22], {"Christchurch"})
        self.assertEqual(regions[33], {"Dunedin"})
        self.assertEqual(regions[34], {"Dunedin"})

    def test_participation_in_two_regions_is_additive(self):
        regions = self.helpers["_race_entry_participation_regions"](
            [(11, None, None, "Northern"), (None, 11, None, "Christchurch")]
        )
        self.assertEqual(regions[11], {"Northern", "Christchurch"})

    def test_manual_extra_visibility_is_additive_without_duplicate_effective_output(self):
        person = self.person(visible_regions="Northern, Christchurch")
        detected = self.helpers["_automatic_person_regions"](person, {"Northern"})
        self.assertEqual(detected, ["Northern"])
        self.assertEqual(self.helpers["_person_regions"](person, detected), ["Christchurch", "Northern"])

    def test_unrelated_race_and_name_text_do_not_add_visibility(self):
        person = self.person(id=100)
        direct_regions = self.helpers["_race_entry_participation_regions"](
            [(101, 102, "Fixture Rider", "Dunedin")]
        )
        self.assertEqual(self.helpers["_automatic_person_regions"](person, direct_regions.get(person.id, set())), [])
        self.assertNotIn(person.id, direct_regions)
        helper_source = ast.get_source_segment(
            (ROOT / "services" / "people_service.py").read_text(encoding="utf-8"),
            next(node for node in ast.parse((ROOT / "services" / "people_service.py").read_text(encoding="utf-8")).body if isinstance(node, ast.FunctionDef) and node.name == "_race_entry_participation_regions"),
        )
        self.assertNotIn("name", helper_source.lower())

    def test_existing_horse_linked_region_detection_remains_available(self):
        race = types.SimpleNamespace(source_region="Central")
        entry = types.SimpleNamespace(race=race)
        horse = types.SimpleNamespace(race_entries=[entry])
        person = self.person(horse_links=[types.SimpleNamespace(horse=horse)])
        self.assertEqual(self.helpers["_automatic_person_regions"](person), ["Central"])


class ManagePeopleRegionPresentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = (ROOT / "static" / "records.html").read_text(encoding="utf-8")

    def test_detected_regions_are_read_only_and_manual_regions_remain_editable(self):
        detected = self.records[
            self.records.index("Automatically detected regions") : self.records.index("Aliases", self.records.index("Automatically detected regions"))
        ]
        manual = self.records[
            self.records.index("Manual extra visibility") : self.records.index("Automatically detected regions")
        ]
        self.assertIn("Read-only", detected)
        self.assertIn('id="person-detected-regions"', detected)
        self.assertNotIn("<input", detected)
        self.assertIn('id="person-visible-regions"', manual)
        self.assertIn('type="checkbox"', manual)
        self.assertIn("visible_regions: visibleRegions", self.records)


class EditPinSessionConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = (ROOT / "static" / "records.html").read_text(encoding="utf-8")
        cls.loveracing = (ROOT / "static" / "loveracing.html").read_text(encoding="utf-8")
        cls.people_router = (ROOT / "routers" / "people.py").read_text(encoding="utf-8")
        cls.loveracing_router = (ROOT / "routers" / "loveracing.py").read_text(encoding="utf-8")

    def test_first_edit_prompts_but_accepted_session_is_reused_for_person_save(self):
        helper = self.records[self.records.index("async function requirePin") : self.records.index("function adminHeaders")]
        self.assertIn("if (existing)", helper)
        self.assertIn("window.prompt(message)", helper)
        self.assertIn('sessionStorage.setItem(\'peopleSystemAdminPin\', pin)', helper)
        self.assertNotIn("forcePrompt", helper)
        self.assertIn("requireEditPin('Enter Edit PIN to update this person record')", self.records)

    def test_destructive_actions_remain_master_gated(self):
        self.assertIn("requireMasterAdminPin()", self.records)
        self.assertIn("'/api/admin/master-check'", self.records)
        self.assertIn("Depends(require_master_admin_pin)", self.people_router)

    def test_loveracing_normal_edits_continue_to_use_the_edit_gate(self):
        self.assertIn('requireEditPin("Enter Edit PIN to change LOVERACING automation")', self.loveracing)
        self.assertIn('requireEditPin("Enter Edit PIN to establish or change this LOVERACING course assignment")', self.loveracing)
        self.assertIn("Depends(require_edit_pin)", self.loveracing_router)


class WingatuiHistoricalContaminationEvidenceTest(unittest.TestCase):
    BACKUP = ROOT / "backups" / "people-system-auto-backup-20260731-050501.json"

    def test_wingatui_backup_rows_were_historically_tagged_christchurch_without_a_course_assignment(self):
        tables = json.loads(self.BACKUP.read_text(encoding="utf-8"))["tables"]
        wingatui_races = [race for race in tables["races"] if race["track"] == "Wingatui"]
        wingatui_courses = [course for course in tables["racecourses"] if "wingatui" in str(course).lower()]
        self.assertEqual(len(wingatui_races), 8)
        self.assertEqual({race["source_region"] for race in wingatui_races}, {"Christchurch"})
        self.assertEqual(wingatui_courses, [])

    def test_viewer_race_day_queries_remain_authoritatively_region_scoped(self):
        people_service_source = (ROOT / "services" / "people_service.py").read_text(encoding="utf-8")
        race_router_source = (ROOT / "routers" / "race.py").read_text(encoding="utf-8")
        race_day_service_source = (ROOT / "services" / "race_day_service.py").read_text(encoding="utf-8")
        self.assertIn("Race.date == race_day, Race.source_region == source_region", race_day_service_source)
        self.assertIn("races_for_race_day(db, race_day, current_region", people_service_source)
        self.assertIn("races_for_race_day(db, race_day, default_region()", race_router_source)


if __name__ == "__main__":
    unittest.main()
