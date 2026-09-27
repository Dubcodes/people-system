import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VersionMetadataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.version_path = ROOT / "static" / "version.json"
        cls.settings = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")

    def test_canonical_development_release_metadata_is_present(self):
        self.assertTrue(self.version_path.is_file())
        metadata = json.loads(self.version_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], "0.10.0-rc1")
        self.assertEqual(metadata["status"], "Release Candidate")
        self.assertEqual(metadata["stable_base"], "0.9.0")
        self.assertEqual(metadata["date"], "2026-08-27")

    def test_settings_reads_metadata_without_hard_coding_the_release(self):
        self.assertIn('id="application-version"', self.settings)
        self.assertIn('fetch("/static/version.json"', self.settings)
        self.assertNotIn("0.10.0-rc1", self.settings)
        self.assertNotIn("Release Candidate", self.settings)

    def test_version_fetch_is_fresh_and_failure_is_informational(self):
        loader = self.settings[self.settings.index("async function loadApplicationVersion") : self.settings.index("function clearStaleRaceDateOverride")]
        self.assertIn('cache: "no-store"', loader)
        self.assertIn('"Cache-Control": "no-cache"', loader)
        self.assertIn("Version unavailable", loader)
        self.assertIn("catch (_)", loader)

    def test_settings_initialisation_and_existing_helpers_remain_independent(self):
        self.assertIn("loadApplicationVersion();", self.settings)
        self.assertIn("Promise.all([loadSuggestions(), loadRaceDays()])", self.settings)
        self.assertIn('id="save-settings"', self.settings)


if __name__ == "__main__":
    unittest.main()
