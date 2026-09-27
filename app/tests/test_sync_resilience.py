import unittest
import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class SyncResilienceSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.operator = (ROOT / "static" / "operator.html").read_text(encoding="utf-8")
        cls.state_router = (ROOT / "routers" / "state.py").read_text(encoding="utf-8")

    def test_sync_health_is_time_based_and_has_bounded_backoff(self):
        for page in (self.viewer, self.operator):
            self.assertIn("SYNC_LOST_AFTER_MS = 15000", page)
            self.assertIn("STATE_REQUEST_TIMEOUT_MS = 8000", page)
            self.assertIn("STATE_RETRY_MAX_MS = 10000", page)
            self.assertIn("Date.now() - lastKnownActivity >= SYNC_LOST_AFTER_MS", page)
            self.assertIn("Math.min(STATE_RETRY_MAX_MS", page)

    def test_state_polling_is_scheduled_without_overlapping_intervals(self):
        self.assertIn("if (statePollInFlight) return;", self.viewer)
        self.assertIn("scheduleStatePoll(nextDelay)", self.viewer)
        self.assertNotIn("pollViewerStateWithDoubleCheck", self.viewer)
        self.assertIn("if (refreshInFlight) return;", self.operator)
        self.assertIn("scheduleStatePoll(nextDelay)", self.operator)
        self.assertNotIn("refreshWithDoubleCheck", self.operator)

    def test_reconnect_preserves_server_authority_and_existing_selection_rules(self):
        self.assertIn("reconcileAuthoritativeViewerState(state)", self.viewer)
        self.assertIn("loadPersonDetail(nextSelectedPersonId, { publishState: false, controller: statePollController })", self.viewer)
        self.assertIn('loadOverview(searchInput.value, { skipAuto: true, publishState: false, controller: statePollController })', self.viewer)
        self.assertIn("stateMatchesEffectiveRaceDay(state, effectiveRaceDay)", self.viewer)
        self.assertIn("requestedTrack && !availableTracks.includes(requestedTrack)", self.viewer)
        self.assertIn("needsAuthoritativeReconcile = true", self.viewer)
        self.assertGreaterEqual(self.viewer.count("needsAuthoritativeReconcile = false"), 2)
        self.assertIn("applyOperatorHighlight(state)", self.viewer)
        self.assertIn("operator_highlight_person_id", self.operator)
        self.assertNotIn("selected_person_id:Number(card.dataset.personId)", self.operator)
        self.assertNotIn("postSharedState({ selected_track", self.operator)

    def test_bootstrap_fetches_authoritative_state_before_loading_local_defaults(self):
        bootstrap = self.viewer[self.viewer.index("async function bootstrap()") :]
        self.assertLess(bootstrap.index("await runStatePoll(true)"), bootstrap.index("if (!lastOverview)"))
        self.assertIn('loadOverview("", { skipAuto: true, publishState: false })', bootstrap)

    def test_reconnect_controller_cancels_the_entire_active_refresh_chain(self):
        self.assertIn("controller: statePollController", self.viewer)
        for call in (
            "loadViewerPreferences(statePollController)",
            "fetchSelectedPerson(statePollController)",
            "loadOverview(statePollController)",
            "ensureAllPeopleLoaded(statePollController)",
        ):
            self.assertIn(call, self.operator)

    def test_revision_is_metadata_not_a_cross_process_acceptance_gate(self):
        for page in (self.viewer, self.operator):
            self.assertNotRegex(page, r"revision\s*[<>]=?\s*lastStateRevision")
            self.assertNotRegex(page, r"lastStateRevision\s*[<>]=?\s*revision")
        self.assertIn("state.runtime_id !== lastStateRuntimeId", self.viewer)

    def test_mobile_sync_outline_and_fullscreen_controls_are_non_layout_features(self):
        for page in (self.viewer, self.operator):
            self.assertIn("body.sync-lost::after", page)
            self.assertRegex(page, r"position:\s*fixed")
            self.assertRegex(page, r"pointer-events:\s*none")
            self.assertIn("id=\"fullscreen-toggle\"", page)
            self.assertIn("requestFullscreen?.()", page)
            self.assertIn("exitFullscreen?.()", page)

    def test_state_router_exposes_lightweight_revision_and_response_time(self):
        self.assertIn("_state_revision = 0", self.state_router)
        self.assertIn('"revision": _state_revision', self.state_router)
        self.assertIn('"runtime_id": _state_runtime_id', self.state_router)
        self.assertIn('"server_time": datetime.now(timezone.utc).isoformat()', self.state_router)
        self.assertIn("_state_revision += 1", self.state_router)


class StateRouterBehaviourTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class APIRouterStub:
            def __init__(self, **_kwargs):
                pass

            def get(self, *_args, **_kwargs):
                return lambda function: function

            def post(self, *_args, **_kwargs):
                return lambda function: function

        fastapi = types.ModuleType("fastapi")
        fastapi.APIRouter = APIRouterStub
        database = types.ModuleType("database")
        database.SessionLocal = lambda: None
        services = types.ModuleType("services")
        services.__path__ = []
        settings_service = types.ModuleType("services.system_settings_service")
        settings_service.get_setting_value = lambda *_: None
        settings_service.set_setting_value = lambda *_: None
        module_name = "state_router_sync_resilience_test"
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "routers" / "state.py")
        cls.state = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "fastapi": fastapi,
            "database": database,
            "services": services,
            "services.system_settings_service": settings_service,
            module_name: cls.state,
        }):
            spec.loader.exec_module(cls.state)

    def setUp(self):
        self.state._state = {
            "selected_person_id": 7,
            "operator_highlight_person_id": None,
            "operator_highlight_expires_at": None,
            "selected_track": "Existing Track",
            "selected_race_id": 42,
            "pre_mode": False,
            "race_date": "2026-08-24",
        }
        self.state._state_revision = 500

    def test_explicit_null_clears_stale_race_when_entering_pre_mode(self):
        result = self.state.update_viewer_state(
            self.state.ViewerStateUpdate(selected_race_id=None, pre_mode=True)
        )
        self.assertIsNone(result["selected_race_id"])
        self.assertTrue(result["pre_mode"])
        self.assertEqual(result["revision"], 501)

    def test_process_revision_reset_still_returns_current_authoritative_state(self):
        self.state._state_revision = 1
        result = self.state.get_viewer_state()
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["selected_person_id"], 7)
        self.assertEqual(result["runtime_id"], self.state._state_runtime_id)
        self.assertIn("server_time", result)


if __name__ == "__main__":
    unittest.main()
