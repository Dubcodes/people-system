import os
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from loveracing_importer import services
from loveracing_importer.discovery import calendar_discovery_is_complete
from services.race_day_service import meeting_is_currently_eligible


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.rows


class _Db:
    def __init__(self, runs):
        self.runs = runs

    def query(self, *_args):
        return _Query(self.runs)


def meeting():
    return types.SimpleNamespace(
        id=55929,
        source_meeting_id="55929",
        date=date(2026, 8, 30),
        race_count=8,
        import_status="ok",
        last_error=None,
        next_scheduled_refresh_at=datetime(2026, 8, 30, 12, 0),
        backoff_until=None,
        updated_at=None,
    )


def run(success, error_message=None):
    return types.SimpleNamespace(success=success, error_message=error_message)


class AbandonedMeetingLifecycleTest(unittest.TestCase):
    NOW = datetime(2026, 8, 30, 12, 0)
    EMPTY_PAGE = "ValueError: No races found in LOVERACING meeting page."

    def _confirmation(self, runs, candidates=(), discovery_error=None):
        def setting_value(_db, key):
            if key == "loveracing_last_discovery_success_at":
                return self.NOW.isoformat()
            if key == "loveracing_last_discovery_error":
                return discovery_error
            return None

        with patch.object(services, "get_setting_value", side_effect=setting_value), patch.object(
            services, "last_calendar_candidates", return_value=list(candidates)
        ):
            return services._meeting_is_conservatively_abandoned(_Db(runs), meeting(), self.EMPTY_PAGE, now=self.NOW)

    def test_one_temporary_empty_parse_failure_is_not_abandonment(self):
        self.assertFalse(self._confirmation([run(True)]))

    def test_failed_or_stale_calendar_discovery_is_never_absence_evidence(self):
        self.assertFalse(self._confirmation([run(True), run(False, self.EMPTY_PAGE)], discovery_error="HTTP 503"))

    def test_empty_or_partial_calendar_markup_is_not_successful_absence_evidence(self):
        self.assertFalse(calendar_discovery_is_complete("<html><body>temporary page</body></html>", []))
        self.assertFalse(calendar_discovery_is_complete('<a href="/RaceInfo/55929/Meeting-Overview.aspx">Te Aroha</a>', [object()]))

    def test_repeated_empty_page_after_calendar_removal_marks_only_the_old_meeting_inactive(self):
        self.assertTrue(self._confirmation([run(True), run(False, self.EMPTY_PAGE)]))
        self.assertFalse(self._confirmation(
            [run(True), run(False, self.EMPTY_PAGE)],
            [{"source_url": "https://loveracing.nz/RaceInfo/55929/Meeting-Overview.aspx"}],
        ))
        self.assertTrue(self._confirmation(
            [run(True), run(False, self.EMPTY_PAGE)],
            [{"source_url": "https://loveracing.nz/RaceInfo/56389/Meeting-Overview.aspx"}],
        ))

    def test_inactive_status_is_excluded_only_from_current_day_selection(self):
        self.assertFalse(meeting_is_currently_eligible("abandoned"))
        self.assertFalse(meeting_is_currently_eligible("postponed"))
        self.assertTrue(meeting_is_currently_eligible("parse_failed"))
        self.assertTrue(meeting_is_currently_eligible("ok"))

    def test_viewer_and_today_endpoint_do_not_fall_back_to_yesterday(self):
        people = (ROOT / "services" / "people_service.py").read_text(encoding="utf-8")
        races = (ROOT / "routers" / "race.py").read_text(encoding="utf-8")
        viewer = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("latest = db.query(Race.date)", people)
        self.assertNotIn("latest = db.query(Race.date)", races)
        self.assertIn("No ${overview.current_region || \"current region\"} meeting today", viewer)
        self.assertIn("if (!races.length || !overview.race_day) return null;", viewer)

    def test_refresh_skips_confirmed_inactive_but_not_parse_failed(self):
        source = (ROOT / "loveracing_importer" / "services.py").read_text(encoding="utf-8")
        refresh = source[source.index("def _refresh_due_imported_meetings") : source.index("def _meeting_payload")]
        self.assertIn("meeting.import_status in INACTIVE_MEETING_STATUSES", refresh)
        self.assertNotIn('meeting.import_status == "parse_failed"', refresh)


if __name__ == "__main__":
    unittest.main()
