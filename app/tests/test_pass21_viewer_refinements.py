from html.parser import HTMLParser
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _HorseStripParentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.horse_strip_ancestors = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.stack.append((tag, attributes))
        if attributes.get("id") == "horse-number-strip":
            self.horse_strip_ancestors = list(self.stack[:-1])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


class Pass21ViewerRefinementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_horse_strip_is_inside_the_compact_focus_header(self):
        parser = _HorseStripParentParser()
        parser.feed(self.viewer)
        ancestor_classes = {
            class_name
            for _, attrs in parser.horse_strip_ancestors
            for class_name in attrs.get("class", "").split()
        }
        self.assertIn("focus-header", ancestor_classes)
        self.assertIn("flex:1 1 360px", self.viewer)
        self.assertIn(".horse-number-strip { display:flex; flex-wrap:wrap", self.viewer)
        strip_renderer = self.viewer[
            self.viewer.index("function renderHorseNumberStrip") : self.viewer.index("function renderOverview")
        ]
        self.assertIn("entries.map(entry =>", strip_renderer)
        self.assertNotIn(".slice(", strip_renderer)

    def test_current_race_member_does_not_get_a_duplicate_add_button(self):
        detail = self.viewer[
            self.viewer.index("function renderPersonDetail") : self.viewer.index("async function loadPersonDetail")
        ]
        self.assertIn("!preMode && relevantRaceEntriesForPerson(detail).length > 0", detail)
        self.assertIn("&& !alreadyInSelectedRace", detail)
        self.assertIn("canAttachToMeeting && attachLabel", detail)

    def test_outsider_and_pre_mode_keep_the_existing_manual_add_path(self):
        relevance = self.viewer[
            self.viewer.index("function relevantRaceEntriesForPerson") : self.viewer.index("function raceContextForPerson")
        ]
        detail = self.viewer[
            self.viewer.index("function renderPersonDetail") : self.viewer.index("async function loadPersonDetail")
        ]
        self.assertIn("if (preMode || !entries.length) return []", relevance)
        self.assertIn("Boolean(lastOverview?.race_day && selectedTrack) && !alreadyInSelectedRace", detail)
        self.assertIn("quickLookAttachLabel()", detail)

    def test_silk_comes_from_each_exact_matched_entry_and_has_no_placeholder(self):
        context = self.viewer[
            self.viewer.index("function raceContextForPerson") : self.viewer.index("function horseAssociatesMarkup")
        ]
        self.assertIn("const matches = relevantRaceEntriesForPerson(detail)", context)
        self.assertIn("matches.map(entry =>", context)
        self.assertIn("entry.silk_image_url ?", context)
        self.assertIn("entry.silk_description || 'Silk'", context)
        self.assertIn(': ""', context)

    def test_failed_silk_images_are_removed_without_a_broken_placeholder(self):
        fallback = self.viewer[
            self.viewer.index("function wireRaceSilkFallbacks") : self.viewer.index("function horseAssociatesMarkup")
        ]
        self.assertIn('image.addEventListener("error"', fallback)
        self.assertIn('item.classList.add("no-silk")', fallback)
        self.assertIn("image.remove()", fallback)
        self.assertIn("wireRaceSilkFallbacks();", self.viewer)


if __name__ == "__main__":
    unittest.main()
