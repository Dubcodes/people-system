import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ViewerRaceLifecycleSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        start = cls.viewer.index("function validStateRaceId")
        end = cls.viewer.index("async function reconcileAuthoritativeViewerState")
        cls.reconcile = cls.viewer[start:end]

    def shared_selection(self, *scenarios):
        script = (
            self.reconcile
            + "\nconst scenarios = "
            + json.dumps(scenarios)
            + ";\nconsole.log(JSON.stringify(scenarios.map(({ state, overview }) => sharedRaceSelectionForEffectiveOverview(state, overview))));"
        )
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def auto_target(self, overview, race_data):
        start = self.viewer.index("function parseRaceDateTime")
        end = self.viewer.index("function updateAutoControls")
        helpers = self.viewer[start:end]
        script = (
            "let lastRaceData = " + json.dumps(race_data) + ";\n"
            "const AUTO_RACE_LEAD_MINUTES = 15;\n"
            "const NativeDate = Date; Date = class FakeDate extends NativeDate { "
            "constructor(...args) { super(...(args.length ? args : ['2026-08-28T10:50:00'])); } };\n"
            + helpers
            + "\nconsole.log(JSON.stringify(currentAutoTarget(" + json.dumps(overview) + ")));"
        )
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def test_stale_shared_date_is_checked_after_current_effective_day_load(self):
        result = self.shared_selection({
            "state": {"race_date": "2026-08-22", "selected_track": "Te Rapa", "selected_race_id": 88, "pre_mode": False},
            "overview": {"race_day": "2026-08-28", "tracks": ["Cambridge Synthetic"], "selected_track": "Cambridge Synthetic"},
        })[0]
        self.assertEqual(result, {
            "selectedTrack": "Cambridge Synthetic",
            "selectedRaceId": None,
            "preMode": True,
            "stateWasRejected": True,
            "requestedStateRaceId": None,
        })

    def test_same_day_valid_track_is_reused_and_unknown_track_is_rejected(self):
        same_day, unknown_track = self.shared_selection(
            {
                "state": {"race_date": "2026-08-28", "selected_track": "Cambridge", "selected_race_id": 4, "pre_mode": False},
                "overview": {"race_day": "2026-08-28", "tracks": ["Cambridge", "Ruakaka"], "selected_track": "Ruakaka"},
            },
            {
                "state": {"race_date": "2026-08-28", "selected_track": "Te Rapa", "selected_race_id": None, "pre_mode": True},
                "overview": {"race_day": "2026-08-28", "tracks": ["Cambridge", "Ruakaka"], "selected_track": "Cambridge"},
            },
        )
        self.assertEqual((same_day["selectedTrack"], same_day["selectedRaceId"], same_day["preMode"], same_day["stateWasRejected"]), ("Cambridge", 4, False, False))
        self.assertEqual((unknown_track["selectedTrack"], unknown_track["selectedRaceId"], unknown_track["preMode"], unknown_track["stateWasRejected"]), ("Cambridge", None, True, True))
        self.assertIn("requestedTrack && !availableTracks.includes(requestedTrack)", self.reconcile)

    def test_stale_or_missing_race_id_cannot_accept_backend_fallback(self):
        stale_race, missing_race = self.shared_selection(
            {
                "state": {"race_date": "2026-08-28", "selected_track": "Cambridge", "selected_race_id": 88, "pre_mode": False},
                "overview": {"race_day": "2026-08-28", "tracks": ["Cambridge"], "selected_track": "Cambridge"},
            },
            {
                "state": {"race_date": "2026-08-28", "selected_track": "Cambridge", "selected_race_id": None, "pre_mode": False},
                "overview": {"race_day": "2026-08-28", "tracks": ["Cambridge"], "selected_track": "Cambridge"},
            },
        )
        self.assertFalse(stale_race["stateWasRejected"])
        self.assertEqual(stale_race["requestedStateRaceId"], 88)
        self.assertTrue(missing_race["stateWasRejected"])
        self.assertIn("lastOverview?.selected_race_id !== requestedStateRaceId", self.viewer)
        self.assertIn("(!preMode && !stateRaceId)", self.reconcile)

    def test_settings_date_override_remains_the_initial_local_date_choice(self):
        reconcile = self.viewer[self.viewer.index("async function reconcileAuthoritativeViewerState"):self.viewer.index("function scheduleStatePoll")]
        self.assertIn("selectedRaceDate = raceDateOverride || null", reconcile)
        self.assertIn("selectedRaceDate = lastOverview?.race_day || raceDateOverride || null", reconcile)

    def test_existing_auto_evaluator_runs_only_when_auto_is_on_after_reconcile(self):
        auto_start = self.viewer.index("async function maybeAutoSelectRace")
        auto_end = self.viewer.index("async function loadPersonDetail")
        auto = self.viewer[auto_start:auto_end]
        self.assertIn("if (!autoRaceMode || !overview) return;", auto)
        self.assertGreater(
            self.viewer.index("await maybeAutoSelectRace();", self.viewer.index("async function reconcileAuthoritativeViewerState")),
            self.viewer.index("if (stateWasRejected) await syncViewerState(effectiveState)"),
        )
        self.assertIn("AUTO_RACE_LEAD_MINUTES = 15", self.viewer)
        self.assertIn("setInterval(() => maybeAutoSelectRace(), 60000)", self.viewer)

    def test_existing_auto_timing_selects_the_same_controlled_race(self):
        target = self.auto_target(
            {"race_day": "2026-08-28", "races": [{"id": 4, "label": "R4"}]},
            {"races": [{"id": 4, "start_time": "11:00 AM"}]},
        )
        self.assertFalse(target["preMode"])
        self.assertEqual(target["race"]["id"], 4)


if __name__ == "__main__":
    unittest.main()
