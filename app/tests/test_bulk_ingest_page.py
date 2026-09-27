import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "static" / "bulk_ingest.html"


class BulkIngestPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_cancelled_skip_note_stops_before_request(self):
        self.assertIn("const note = window.prompt('Optional skip note');", self.html)
        self.assertIn("if (note === null) return;", self.html)
        self.assertIn("if (!response.ok)", self.html)

    def test_local_and_pending_images_can_be_enlarged(self):
        self.assertGreaterEqual(self.html.count("data-enlarge-image"), 4)
        self.assertIn('id="image-lightbox"', self.html)
        self.assertIn("imageLightbox.showModal()", self.html)


if __name__ == "__main__":
    unittest.main()
