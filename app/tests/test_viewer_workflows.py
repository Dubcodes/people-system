import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ViewerWorkflowSourceTest(unittest.TestCase):
    def test_quick_look_includes_race_associations(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<b>Jockey:</b>', html)
        self.assertIn('personIsTrainer ? "Co-trainer" : "Trainer"', html)
        self.assertIn("race-context-copy", html)

    def test_person_save_reuses_the_accepted_edit_pin_session(self):
        html = (ROOT / "static" / "records.html").read_text(encoding="utf-8")
        self.assertIn("requireEditPin('Enter Edit PIN to update this person record')", html)
        self.assertNotIn("forcePrompt", html)
        self.assertIn("headers:adminHeaders({'Content-Type':'application/json'})", html)

    def test_operator_and_records_use_prefix_search(self):
        records = (ROOT / "static" / "records.html").read_text(encoding="utf-8")
        operator = (ROOT / "static" / "operator.html").read_text(encoding="utf-8")
        self.assertIn("candidateTokens[candidateIndex].startsWith(queryToken)", records)
        self.assertIn("candidateTokens[candidateIndex].startsWith(queryToken)", operator)


if __name__ == "__main__":
    unittest.main()
