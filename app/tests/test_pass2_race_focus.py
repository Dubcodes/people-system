import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_MODULE_NAME = "pass2_race_focus_schemas"
SCHEMA_SPEC = importlib.util.spec_from_file_location(SCHEMA_MODULE_NAME, ROOT / "schemas.py")
SCHEMA_MODULE = importlib.util.module_from_spec(SCHEMA_SPEC)
sys.modules[SCHEMA_MODULE_NAME] = SCHEMA_MODULE
SCHEMA_SPEC.loader.exec_module(SCHEMA_MODULE)
RaceEntrySummary = SCHEMA_MODULE.RaceEntrySummary


class RaceEntryIdentitySchemaTest(unittest.TestCase):
    def test_race_entry_carries_exact_horse_jockey_and_distinct_trainer_ids(self):
        entry = RaceEntrySummary(
            horse_id=91,
            horse_name="Exact Runner",
            horse_number=12,
            jockey_person_id=101,
            jockey_name="Exact Jockey",
            trainer_name="Trainer One, Trainer Two",
            trainer_people=[
                {"person_id": 201, "name": "Trainer One"},
                {"person_id": 202, "name": "Trainer Two"},
            ],
            associated_person_ids=[301, 302],
            is_scratched=True,
        )
        payload = entry.model_dump()
        self.assertEqual(payload["horse_id"], 91)
        self.assertEqual(payload["jockey_person_id"], 101)
        self.assertEqual([person["person_id"] for person in payload["trainer_people"]], [201, 202])
        self.assertEqual(payload["associated_person_ids"], [301, 302])
        self.assertTrue(payload["is_scratched"])


class Pass2ViewerSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.operator = (ROOT / "static" / "operator.html").read_text(encoding="utf-8")
        cls.people_service = (ROOT / "services" / "people_service.py").read_text(encoding="utf-8")

    def test_backend_builds_exact_identity_fields_without_schema_migration(self):
        for fragment in (
            "horse_id=entry.horse_id",
            "jockey_person_id=entry.jockey_id",
            'trainer_people=[{"person_id": person.id, "name": person.name}',
            "associated_person_ids=associated_person_ids",
        ):
            self.assertIn(fragment, self.people_service)
        self.assertIn('"jockey" not in link.role.lower()', self.people_service)

    def test_horse_strip_uses_exact_ids_and_conservative_no_jockey_fallbacks(self):
        strip = self.viewer[
            self.viewer.index("function renderHorseNumberStrip") : self.viewer.index("function renderOverview")
        ]
        self.assertIn("data-select-person-id=\"${jockeyPersonId}\"", strip)
        self.assertIn("if (trainers.length === 1)", strip)
        self.assertIn("if (trainers.length > 1)", strip)
        self.assertIn('class="horse-number-choice"', strip)
        self.assertIn('aria-disabled="true"', strip)
        self.assertIn('class="${classes}"', strip)
        self.assertNotIn("find(person", strip)

    def test_exact_person_controls_share_the_canonical_viewer_selection_path(self):
        self.assertIn('event.target.closest("[data-select-person-id]")', self.viewer)
        self.assertIn("loadPersonDetail(personId)", self.viewer)
        self.assertIn("jockey_person_id", self.viewer)
        self.assertIn("trainer_people", self.viewer)

    def test_horse_associates_are_race_relevant_lazy_and_exclude_jockeys(self):
        relevance = self.viewer[
            self.viewer.index("function relevantRaceEntriesForPerson") : self.viewer.index("function raceContextForPerson")
        ]
        self.assertIn("entry.jockey_person_id", relevance)
        self.assertIn("entry.trainer_people", relevance)
        self.assertIn("entry.associated_person_ids", relevance)
        self.assertIn("linkedHorseIds.has(Number(entry.horse_id))", relevance)
        self.assertIn('!/jockey/i.test(String(link.role || ""))', relevance)
        self.assertNotIn("horse_name", relevance)

        lazy = self.viewer[
            self.viewer.index("async function loadHorseAssociateDetail") : self.viewer.index("function quickLookAttachLabel")
        ]
        self.assertIn("if (!container.open", lazy)
        self.assertIn("fetchWithTimeout(`/api/horses/${horseId}`)", lazy)
        self.assertIn("new Map(relevantEntries.map", lazy)
        self.assertIn("!/jockey/i.test", lazy)
        self.assertIn('role: role || "Associate"', lazy)

    def test_wake_lock_lifecycle_is_fullscreen_only_and_duplicate_safe(self):
        for page in (self.viewer, self.operator):
            self.assertIn('navigator.wakeLock.request("screen")' if page is self.viewer else "navigator.wakeLock.request('screen')", page)
            self.assertIn("!document.fullscreenElement || wakeLockSentinel || wakeLockRequestPromise", page)
            self.assertIn("await sentinel.release()", page)
            self.assertIn("visibilitychange", page)
            self.assertIn("document.visibilityState", page)
            self.assertIn("syncWakeLockWithFullscreen()", page)

    def test_operator_pass1_authority_is_unchanged(self):
        self.assertNotIn("postSharedState({ selected_track", self.operator)
        self.assertNotIn("selected_race_id:Number(card.dataset.personId)", self.operator)
        self.assertIn("operator_highlight_person_id:Number(card.dataset.personId)", self.operator)


if __name__ == "__main__":
    unittest.main()
