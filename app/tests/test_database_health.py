import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import AuditEvent, Horse, HorsePerson, Person, PersonRole, Race, RaceCourse, RaceEntry, Role, Stable, TrackAlias
from services.database_health_service import DatabaseHealthCleanupError, remove_safe_orphan_horses, scan_database_health, safe_orphan_horse_candidate


class DatabaseHealthServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _report(self):
        report = scan_database_health(self.db)
        return {category["key"]: category["findings"] for category in report["categories"]}, report

    def _race(self, track="Te Rapa", region="Northern"):
        race = Race(track=track, source_region=region, race_number=1, date=date(2026, 8, 26))
        self.db.add(race)
        self.db.flush()
        return race

    def test_impossible_orphan_horses_are_safe_and_legitimate_names_are_not(self):
        numeric = Horse(name="58.00")
        mare = Horse(name="Mare")
        hyphen = Horse(name="-")
        legitimate = Horse(name="Six Eight")
        unusual = Horse(name="X X X")
        self.db.add_all([numeric, mare, hyphen, legitimate, unusual])
        self.db.commit()

        groups, report = self._report()
        safe_names = {finding["display_value"] for finding in groups["safe_orphan_horse_junk"]}
        self.assertEqual(safe_names, {"58.00", "Mare", "-"})
        self.assertNotIn("Six Eight", safe_names)
        self.assertNotIn("X X X", safe_names)
        self.assertTrue(report["read_only"])
        self.assertEqual(self.db.query(Horse).count(), 5)

    def test_impossible_horse_with_race_entry_is_referenced_and_never_safe(self):
        horse = Horse(name="58.00")
        self.db.add(horse)
        self.db.flush()
        self.db.add(RaceEntry(race_id=self._race().id, horse_id=horse.id))
        self.db.commit()

        groups, _ = self._report()
        self.assertEqual(groups["safe_orphan_horse_junk"], [])
        finding = groups["referenced_impossible_horse_artifacts"][0]
        self.assertEqual(finding["display_value"], "58.00")
        self.assertFalse(finding["safe_to_delete"])
        self.assertEqual(finding["reference_counts"]["race_entries"], 1)

    def test_impossible_horse_with_person_link_is_referenced_and_never_safe(self):
        horse = Horse(name="Mare")
        person = Person(name="A Person")
        self.db.add_all([horse, person])
        self.db.flush()
        self.db.add(HorsePerson(horse_id=horse.id, person_id=person.id, role="Trainer"))
        self.db.commit()

        groups, _ = self._report()
        finding = groups["referenced_impossible_horse_artifacts"][0]
        self.assertEqual(finding["reference_counts"]["horse_people"], 1)
        self.assertFalse(finding["safe_to_delete"])

    def test_impossible_horse_with_any_other_meaningful_link_is_not_safe(self):
        stable = Stable(name="Fixture Stable")
        horse = Horse(name="Colt", stable=stable)
        self.db.add_all([stable, horse])
        self.db.commit()

        groups, _ = self._report()
        finding = groups["referenced_impossible_horse_artifacts"][0]
        self.assertEqual(finding["reference_counts"]["stable_assignment"], 1)
        self.assertIsNone(safe_orphan_horse_candidate(self.db, horse.id))

    def test_initials_people_are_review_only_never_safe_junk(self):
        rogerson = Person(name="G A Rogerson")
        other = Person(name="D G Rogerson")
        self.db.add_all([rogerson, other])
        self.db.commit()

        groups, _ = self._report()
        review_names = {finding["display_value"] for finding in groups["review_name_people"]}
        self.assertIn("G A Rogerson", review_names)
        self.assertIn("D G Rogerson", review_names)
        self.assertEqual(groups["safe_orphan_horse_junk"], [])
        self.assertIsNone(safe_orphan_horse_candidate(self.db, rogerson.id))

    def test_duplicate_people_and_composite_jockey_are_report_only(self):
        first = Person(name="Alpha Person")
        second = Person(name=" alpha   person ")
        composite = Person(name="Sophia Nolan And Hanna Orting")
        jockey = Role(name="Jockey")
        self.db.add_all([first, second, composite, jockey])
        self.db.flush()
        self.db.add(PersonRole(person_id=composite.id, role_id=jockey.id))
        self.db.commit()

        groups, _ = self._report()
        self.assertEqual({finding["row_id"] for finding in groups["duplicate_people"] if finding["table"] == "people"}, {first.id, second.id})
        role_finding = groups["suspicious_role_combinations"][0]
        self.assertEqual(role_finding["display_value"], "Sophia Nolan And Hanna Orting — Jockey")
        self.assertFalse(role_finding["safe_to_delete"])
        self.assertEqual(self.db.query(PersonRole).count(), 1)

    def test_region_warnings_include_missing_course_and_race_course_conflict(self):
        unassigned = RaceCourse(name="Unknown Track", source_region=None)
        wingatui = RaceCourse(name="Wingatui", source_region="Dunedin")
        self.db.add_all([unassigned, wingatui])
        self.db.add(Race(track="Wingatui", source_region="Christchurch", race_number=2, date=date(2026, 8, 26)))
        self.db.commit()

        groups, _ = self._report()
        values = {finding["display_value"] for finding in groups["region_racecourse_health"]}
        self.assertIn("Unknown Track", values)
        self.assertIn("Wingatui R2", values)
        self.assertEqual(self.db.query(Race).one().source_region, "Christchurch")

    def test_orphan_relationship_and_malformed_track_alias_are_report_only(self):
        race = self._race()
        self.db.add(RaceEntry(race_id=race.id, horse_id=9999))
        self.db.add(TrackAlias(alias="   ", canonical_track="   "))
        self.db.commit()

        groups, _ = self._report()
        self.assertEqual(groups["orphan_relationship_rows"][0]["table"], "race_entries")
        self.assertEqual(groups["track_alias_health"][0]["row_id"], 1)
        self.assertFalse(groups["orphan_relationship_rows"][0]["safe_to_delete"])

    def test_scan_never_autoflushes_a_callers_pending_changes(self):
        db = sessionmaker(bind=self.engine, autoflush=True, autocommit=False, future=True)()
        pending = Horse(name="58.00")
        db.add(pending)

        with patch("services.database_health_service.create_backup_snapshot") as backup, patch(
            "services.database_health_service.record_audit_event"
        ) as audit:
            report = scan_database_health(db)
        groups = {category["key"]: category["findings"] for category in report["categories"]}

        self.assertIsNone(pending.id)
        self.assertEqual(groups["safe_orphan_horse_junk"], [])
        backup.assert_not_called()
        audit.assert_not_called()
        db.rollback()
        db.close()


class DatabaseHealthCleanupTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _backup():
        return {"status": "ok", "filename": "people-system-backup-health.json", "table_counts": {"horses": 1}}

    def test_cleanup_only_deletes_selected_safe_horse_after_backup_and_audits(self):
        selected = Horse(name="58.00")
        untouched = Horse(name="Mare")
        self.db.add_all([selected, untouched])
        self.db.commit()

        def backup_before_mutation(db, reason=None):
            self.assertEqual(reason, "database_health_pre_cleanup")
            self.assertIsNotNone(db.get(Horse, selected.id))
            return self._backup()

        with patch("services.database_health_service.create_backup_snapshot", side_effect=backup_before_mutation) as backup:
            result = remove_safe_orphan_horses(self.db, [selected.id], True)

        self.assertEqual(backup.call_args.kwargs["reason"], "database_health_pre_cleanup")
        self.assertEqual([row["id"] for row in result["deleted"]], [selected.id])
        self.assertIsNone(self.db.get(Horse, selected.id))
        self.assertIsNotNone(self.db.get(Horse, untouched.id))
        events = self.db.query(AuditEvent).order_by(AuditEvent.id.asc()).all()
        self.assertEqual([event.action for event in events], ["database_health_safe_cleanup", "database_health_cleanup_summary"])
        self.assertIn("58.00", events[0].before_json)

    def test_cleanup_aborts_without_delete_when_backup_fails(self):
        horse = Horse(name="Mare")
        self.db.add(horse)
        self.db.commit()

        with patch("services.database_health_service.create_backup_snapshot", side_effect=OSError("disk unavailable")):
            with self.assertRaises(DatabaseHealthCleanupError) as context:
                remove_safe_orphan_horses(self.db, [horse.id], True)

        self.assertEqual(context.exception.status_code, 503)
        self.assertIsNotNone(self.db.get(Horse, horse.id))
        self.assertEqual(self.db.query(AuditEvent).count(), 0)

    def test_cleanup_revalidates_when_a_selected_horse_gains_a_race_entry(self):
        horse = Horse(name="-")
        self.db.add(horse)
        self.db.commit()
        from services import database_health_service

        real_candidate = database_health_service._safe_orphan_horse_candidate_with_status
        calls = 0

        def candidate_with_late_reference(db, horse_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                race = Race(track="Te Rapa", source_region="Northern", race_number=5, date=date(2026, 8, 26))
                db.add(race)
                db.flush()
                db.add(RaceEntry(race_id=race.id, horse_id=horse_id))
                db.flush()
            return real_candidate(db, horse_id, **kwargs)

        with patch("services.database_health_service.create_backup_snapshot", return_value=self._backup()), patch(
            "services.database_health_service._safe_orphan_horse_candidate_with_status", side_effect=candidate_with_late_reference
        ):
            result = remove_safe_orphan_horses(self.db, [horse.id], True)

        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["skipped_horse_ids"], [horse.id])
        self.assertEqual(result["skipped"], [{"id": horse.id, "reason": "referenced"}])
        self.assertIsNotNone(self.db.get(Horse, horse.id))

    def test_cleanup_deduplicates_ids_and_skips_changed_or_legitimate_rows_without_audit(self):
        safe = Horse(name="58.00")
        legitimate = Horse(name="Real Runner")
        self.db.add_all([safe, legitimate])
        self.db.commit()

        with patch("services.database_health_service.create_backup_snapshot", return_value=self._backup()):
            result = remove_safe_orphan_horses(self.db, [safe.id, safe.id, legitimate.id, 99999], True)

        self.assertEqual([row["id"] for row in result["deleted"]], [safe.id])
        self.assertEqual(result["skipped"], [
            {"id": legitimate.id, "reason": "not_an_impossible_artifact"},
            {"id": 99999, "reason": "missing"},
        ])
        deleted_events = self.db.query(AuditEvent).filter(AuditEvent.action == "database_health_safe_cleanup").all()
        self.assertEqual([event.entity_id for event in deleted_events], [safe.id])
        self.assertIsNotNone(self.db.get(Horse, legitimate.id))

    def test_changed_name_and_non_horse_ids_are_rejected_before_backup_or_delete(self):
        horse = Horse(name="58.00")
        person = Person(name="Person Only")
        self.db.add_all([horse, person])
        self.db.commit()
        horse.name = "Real Runner"
        self.db.commit()

        with patch("services.database_health_service.create_backup_snapshot") as backup:
            with self.assertRaises(DatabaseHealthCleanupError) as context:
                remove_safe_orphan_horses(self.db, [horse.id, person.id + 1000], True)

        self.assertEqual(context.exception.status_code, 409)
        backup.assert_not_called()
        self.assertIsNotNone(self.db.get(Horse, horse.id))
        self.assertEqual(self.db.query(AuditEvent).count(), 0)

    def test_cleanup_skips_one_newly_referenced_horse_but_keeps_another_selected_safe_row(self):
        safe = Horse(name="Mare")
        changed = Horse(name="Filly")
        self.db.add_all([safe, changed])
        self.db.commit()
        from services import database_health_service

        real_candidate = database_health_service._safe_orphan_horse_candidate_with_status
        calls = 0

        def candidate_with_late_person_link(db, horse_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                person = Person(name="Late Linked Trainer")
                db.add(person)
                db.flush()
                db.add(HorsePerson(horse_id=changed.id, person_id=person.id, role="Trainer"))
                db.flush()
            return real_candidate(db, horse_id, **kwargs)

        with patch("services.database_health_service.create_backup_snapshot", return_value=self._backup()), patch(
            "services.database_health_service._safe_orphan_horse_candidate_with_status", side_effect=candidate_with_late_person_link
        ):
            result = remove_safe_orphan_horses(self.db, [safe.id, changed.id], True)

        self.assertEqual([row["id"] for row in result["deleted"]], [safe.id])
        self.assertEqual(result["skipped"], [{"id": changed.id, "reason": "referenced"}])
        self.assertIsNotNone(self.db.get(Horse, changed.id))

    def test_cleanup_rolls_back_deletion_and_audit_when_a_transaction_error_occurs(self):
        horse = Horse(name="Filly")
        self.db.add(horse)
        self.db.commit()
        from services import database_health_service

        original_audit = database_health_service.record_audit_event
        calls = 0

        def fail_on_summary(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("summary audit write failed")
            return original_audit(*args, **kwargs)

        with patch("services.database_health_service.create_backup_snapshot", return_value=self._backup()), patch(
            "services.database_health_service.record_audit_event", side_effect=fail_on_summary
        ):
            with self.assertRaises(DatabaseHealthCleanupError) as context:
                remove_safe_orphan_horses(self.db, [horse.id], True)

        self.assertEqual(context.exception.status_code, 500)
        self.assertIsNotNone(self.db.get(Horse, horse.id))
        self.assertEqual(self.db.query(AuditEvent).count(), 0)

    def test_cleanup_requires_explicit_selected_confirmation_and_master_dependency(self):
        source = (Path(__file__).resolve().parents[1] / "routers" / "admin.py").read_text(encoding="utf-8")
        route_source = source[source.index("def master_remove_safe_orphan_junk") : source.index("@router.get(\"/master/untagged\"")]
        self.assertIn("Depends(require_master_admin_pin)", route_source)
        with self.assertRaises(DatabaseHealthCleanupError) as context:
            remove_safe_orphan_horses(self.db, [], False)
        self.assertEqual(context.exception.status_code, 422)

    def test_mutation_revalidation_uses_a_postgresql_row_lock(self):
        source = (Path(__file__).resolve().parents[1] / "services" / "database_health_service.py").read_text(encoding="utf-8")
        self.assertIn("query.with_for_update()", source)
        cleanup_source = source[source.index("def remove_safe_orphan_horses") :]
        self.assertIn("lock_for_update=True", cleanup_source)


class DatabaseHealthUiSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (Path(__file__).resolve().parents[1] / "static" / "master_admin.html").read_text(encoding="utf-8")

    def test_health_ui_is_explicit_grouped_and_never_runs_cleanup_on_page_load(self):
        self.assertIn('id="scan-database-health"', self.page)
        self.assertIn('id="remove-safe-orphan-junk"', self.page)
        self.assertIn('data-safe-horse-id=', self.page)
        self.assertIn('/api/admin/master/database-health/remove-safe-orphan-junk', self.page)
        self.assertIn('JSON.stringify({ horse_ids: horseIds, confirm: true })', self.page)
        dom_ready = self.page[self.page.index("window.addEventListener('DOMContentLoaded'") :]
        self.assertNotIn("scanDatabaseHealth()", dom_ready)
        self.assertNotIn("removeSafeOrphanJunk()", dom_ready)


if __name__ == "__main__":
    unittest.main()
