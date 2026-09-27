import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DummyModel:
    pass


sys.modules.setdefault("sqlalchemy.orm", types.SimpleNamespace(Session=object, joinedload=lambda *_args, **_kwargs: None))
sys.modules.setdefault(
    "models",
    types.SimpleNamespace(
        Alias=DummyModel,
        Horse=DummyModel,
        HorsePerson=DummyModel,
        LoveracingImportRun=DummyModel,
        ManualAttendee=DummyModel,
        Person=DummyModel,
        PersonRole=DummyModel,
        Race=DummyModel,
        RaceCourse=DummyModel,
        RaceEntry=DummyModel,
        RaceMeeting=DummyModel,
        Role=DummyModel,
    ),
)
sys.modules.setdefault("services.audit_service", types.SimpleNamespace(record_audit_event=lambda *_args, **_kwargs: None))
sys.modules.setdefault(
    "services.region_service",
    types.SimpleNamespace(
        allowed_regions=lambda: ["Northern"],
        default_region=lambda: "Northern",
        normalize_region=lambda value: value,
    ),
)
sys.modules.setdefault(
    "services.system_settings_service",
    types.SimpleNamespace(
        get_setting_value_safe=lambda *_args, **_kwargs: None,
        get_setting_value=lambda *_args, **_kwargs: None,
        set_setting_value=lambda *_args, **_kwargs: None,
    ),
)
sys.modules.setdefault("services.time_service", types.SimpleNamespace(local_now=lambda: None, local_today=lambda: None))

from loveracing_importer.models import ParsedMeeting, ParsedRace, ParsedRunner
from loveracing_importer.services import _auto_loader_lock, _detail_race_matches_overview, _evidence_backed_racing_role, _has_conflicting_racing_role, _match_discovered_course, _race_needs_detail_page, _should_preserve_existing_result_entries


class Horse:
    def __init__(self, name):
        self.name = name


class Entry:
    def __init__(self, horse_name):
        self.horse = Horse(horse_name)


class Race:
    def __init__(self, horse_names):
        self.entries = [Entry(name) for name in horse_names]


def result_race(horse_names):
    return ParsedRace(
        race_number=5,
        status="results",
        runners=[ParsedRunner(runner_number=index + 1, horse_name=name) for index, name in enumerate(horse_names)],
    )


class LoveracingResultPreservationTest(unittest.TestCase):
    def test_import_does_not_override_opposite_existing_racing_role(self):
        person = types.SimpleNamespace(
            role_assignments=[
                types.SimpleNamespace(
                    end_date=None,
                    role=types.SimpleNamespace(name="Trainer"),
                )
            ]
        )

        self.assertTrue(_has_conflicting_racing_role(person, "Jockey"))
        self.assertFalse(_has_conflicting_racing_role(person, "Trainer"))

    def test_role_cleanup_requires_unambiguous_race_entry_evidence(self):
        self.assertEqual(_evidence_backed_racing_role(0, 3), "Trainer")
        self.assertEqual(_evidence_backed_racing_role(4, 0), "Jockey")
        self.assertIsNone(_evidence_backed_racing_role(2, 2))
        self.assertIsNone(_evidence_backed_racing_role(0, 0))

    def test_complete_upcoming_race_does_not_need_detail_page(self):
        upcoming = ParsedRace(
            race_number=4,
            status=None,
            runners=[ParsedRunner(runner_number=index + 1, horse_name=f"Horse {index + 1}") for index in range(10)],
        )

        self.assertFalse(_race_needs_detail_page(upcoming))

    def test_result_race_still_uses_detail_fallback(self):
        result = result_race(["First", "Second", "Third"])

        self.assertTrue(_race_needs_detail_page(result))

    def test_parsed_meeting_header_overrides_noisy_calendar_context(self):
        meeting = types.SimpleNamespace(
            label="Wellington RC @ Trentham",
            racecourse="Nearby calendar text Racing Rotorua @ Arawa Park",
        )
        rotorua = types.SimpleNamespace(name="Rotorua", loveracing_name="Rotorua", short_code=None)
        parsed = ParsedMeeting(
            meeting_name="Wellington RC @ Trentham",
            venue_name="Trentham",
            club_name="Wellington RC",
        )

        matched, warnings = _match_discovered_course(meeting, [rotorua], parsed)

        self.assertIsNone(matched)
        self.assertEqual(warnings, [])
    def test_accepts_detail_page_when_overview_horses_are_present(self):
        overview = result_race(["Gin And Bare It", "Miss Parker"])
        detail = result_race(["Gin And Bare It", "Miss Parker", "Perfectly Set"])

        self.assertTrue(_detail_race_matches_overview(overview, detail))

    def test_rejects_historical_detail_page_with_different_horses(self):
        overview = result_race(["Gin And Bare It", "Miss Parker", "Impressive Belle"])
        historical = result_race(["Merchant Banker", "Gin And Bare It", "Odds And Ends"])

        self.assertFalse(_detail_race_matches_overview(overview, historical))
    def test_preserves_partial_result_when_horses_match_stored_field(self):
        existing = Race(["Top Street", "Red Ned", "Rocem", "Odysseus", "Doctor Iris"])
        parsed = result_race(["Top Street", "Red Ned", "Rocem"])

        self.assertTrue(_should_preserve_existing_result_entries(existing, parsed))

    def test_replaces_result_when_horses_do_not_match_stored_field(self):
        existing = Race(
            [
                "Mr Fabulous",
                "Countryman",
                "Odysseus",
                "Zac Flash",
                "Nightfall",
                "Almanac",
                "Lord Spencer",
                "Teen Titan",
                "Top Street",
                "Touch O'Paradise",
                "Rocem",
            ]
        )
        parsed = result_race(["Top Street", "Red Ned", "Rocem", "Odysseus", "Doctor Iris", "King Cabal", "Skaw Valley"])

        self.assertFalse(_should_preserve_existing_result_entries(existing, parsed))


class AutoLoaderLockTest(unittest.TestCase):
    class Connection:
        def __init__(self, acquired):
            self.acquired = acquired
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params):
            self.calls.append((str(statement), params))
            value = self.acquired if len(self.calls) == 1 else True
            return types.SimpleNamespace(scalar=lambda: value)

    class Bind:
        dialect = types.SimpleNamespace(name="postgresql")

        def __init__(self, connection):
            self.connection = connection

        def connect(self):
            return self.connection

    class Db:
        def __init__(self, connection):
            self.bind = AutoLoaderLockTest.Bind(connection)

        def get_bind(self):
            return self.bind

    def test_acquired_lock_is_released(self):
        connection = self.Connection(True)

        with _auto_loader_lock(self.Db(connection)) as acquired:
            self.assertTrue(acquired)

        self.assertEqual(len(connection.calls), 2)
        self.assertIn("pg_try_advisory_lock", connection.calls[0][0])
        self.assertIn("pg_advisory_unlock", connection.calls[1][0])

    def test_busy_lock_skips_without_unlocking_another_worker(self):
        connection = self.Connection(False)

        with _auto_loader_lock(self.Db(connection)) as acquired:
            self.assertFalse(acquired)

        self.assertEqual(len(connection.calls), 1)



if __name__ == "__main__":
    unittest.main()
