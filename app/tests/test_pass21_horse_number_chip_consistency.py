import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Pass21HorseNumberChipConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.renderer = cls.viewer[
            cls.viewer.index("function renderHorseNumberStrip") : cls.viewer.index("function renderOverview")
        ]

    def test_shared_control_owns_the_complete_chip_geometry_and_surface(self):
        rule = re.search(r"\.horse-number-control \{([^}]+)\}", self.viewer)
        self.assertIsNotNone(rule)
        declarations = rule.group(1)
        for fragment in (
            "display:inline-flex",
            "align-items:center",
            "justify-content:center",
            "min-width:30px",
            "min-height:28px",
            "border:1px solid var(--line)",
            "border-radius:8px",
            "background:linear-gradient(180deg, rgba(255,255,255,.96), var(--button))",
            "font-family:system-ui,-apple-system,\"Segoe UI\",sans-serif",
            "font-variant-numeric:lining-nums tabular-nums",
            "font-feature-settings:\"lnum\" 1,\"tnum\" 1",
        ):
            self.assertIn(fragment, declarations)
        self.assertNotIn("span.horse-number-control", self.viewer)

    def test_runner_status_is_the_only_source_of_scratched_styling(self):
        self.assertIn('const classes = `horse-number-control${scratched ? " scratched" : ""}`', self.renderer)
        self.assertEqual(self.renderer.count('class="${classes}"'), 4)
        self.assertIn(".horse-number-control.scratched", self.viewer)

    def test_all_person_context_branches_render_a_chip_control(self):
        for fragment in (
            "if (jockeyPersonId)",
            "if (trainers.length === 1)",
            "if (trainers.length > 1)",
            '<summary class="${classes}"',
            '<span class="${classes}"',
        ):
            self.assertIn(fragment, self.renderer)
        self.assertNotRegex(self.renderer, r"return `\$\{number\}`")

    def test_multi_trainer_chooser_keeps_exact_person_ids(self):
        chooser = self.renderer[
            self.renderer.index("if (trainers.length > 1)") : self.renderer.index('return `<span')
        ]
        self.assertIn('class="horse-number-choice"', chooser)
        self.assertIn('class="horse-number-choices"', chooser)
        self.assertIn('trainers.map(person => exactPersonButton(person, ""))', chooser)

    def test_no_person_chip_is_disabled_and_keeps_its_tooltip(self):
        fallback = self.renderer[self.renderer.index('return `<span') :]
        self.assertIn('title="${escapeHtml(title)}"', fallback)
        self.assertIn('aria-disabled="true"', fallback)
        self.assertIn('.horse-number-control[aria-disabled="true"] { cursor:not-allowed; }', self.viewer)

    def test_all_entries_are_retained_including_number_26(self):
        self.assertIn("entries.map(entry =>", self.renderer)
        self.assertNotIn(".slice(", self.renderer)
        entries = list(range(1, 27))
        self.assertEqual(entries[-1], 26)
        self.assertEqual(len(entries), 26)


if __name__ == "__main__":
    unittest.main()
