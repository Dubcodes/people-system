import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.modules.setdefault("sqlalchemy.exc", types.SimpleNamespace(IntegrityError=Exception))
sys.modules.setdefault("sqlalchemy.orm", types.SimpleNamespace(Session=object, joinedload=lambda *_args, **_kwargs: None))
sys.modules.setdefault(
    "models",
    types.SimpleNamespace(
        Alias=type("Alias", (), {}),
        Horse=type("Horse", (), {}),
        ManualAttendee=type("ManualAttendee", (), {}),
        Person=type("Person", (), {}),
        PersonRole=type("PersonRole", (), {}),
        Race=type("Race", (), {}),
        RaceEntry=type("RaceEntry", (), {}),
        Role=type("Role", (), {}),
    ),
)
sys.modules.setdefault("services.audit_service", types.SimpleNamespace(record_audit_event=lambda *_args, **_kwargs: None))
sys.modules.setdefault(
    "schemas",
    types.SimpleNamespace(
        ManualAttendeeCreate=type("ManualAttendeeCreate", (), {}),
        ManualAttendeeSummary=type("ManualAttendeeSummary", (), {}),
        RaceImportRequest=type("RaceImportRequest", (), {}),
        RaceImportResponse=type("RaceImportResponse", (), {}),
    ),
)
sys.modules.setdefault("services.region_service", types.SimpleNamespace(default_region=lambda: "Northern", normalize_region=lambda value: value))
sys.modules.setdefault("services.time_service", types.SimpleNamespace(local_today=lambda: None))

from services.race_service import preview_race_data


class ManualRaceImportParserTest(unittest.TestCase):
    def test_preview_rejects_false_horse_names_from_compressed_text(self):
        raw_text = """
        Runner details
        1.
        -
        J: Good Rider
        T: Real Trainer
        2.
        54.00
        J: Good Rider
        T: Real Trainer
        3.
        Affordable (6g Hardham - Preventable)
        J: Good Rider
        T: Real Trainer
        4.
        Ace Lawson-Carroll (a)
        J: Good Rider
        T: Real Trainer
        5.
        less for 17 months; sixth by 12 lengths at Te Rapa 2400m. Capable.
        J: Good Rider
        T: Real Trainer
        """

        parsed = preview_race_data(raw_text)

        self.assertEqual(parsed["runner_count"], 1)
        self.assertEqual(parsed["runners"][0]["horse"], "Affordable")

    def test_preview_keeps_apostrophes_and_multi_word_horse_names(self):
        raw_text = """
        Runner details
        1.
        He's Classic (AUS) (B2)
        J: Good Rider
        T: Real Trainer
        """

        parsed = preview_race_data(raw_text)

        self.assertEqual(parsed["runner_count"], 1)
        self.assertEqual(parsed["runners"][0]["horse"], "He's Classic")


if __name__ == "__main__":
    unittest.main()
