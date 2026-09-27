import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.search_service import search_value_matches, values_match_search


class FlexibleSearchTest(unittest.TestCase):
    def test_abbreviated_first_and_last_name_matches(self):
        self.assertTrue(search_value_matches("Samantha Collett", "sam c"))
        self.assertTrue(search_value_matches("Samantha Collett", "s coll"))

    def test_word_prefixes_must_stay_in_order(self):
        self.assertFalse(search_value_matches("Samantha Collett", "coll sam"))

    def test_aliases_remain_searchable(self):
        self.assertTrue(values_match_search(["Samantha Collett", "Sam Collett"], "sam coll"))

    def test_unrelated_name_does_not_match(self):
        self.assertFalse(search_value_matches("Samantha Collett", "sam log"))


if __name__ == "__main__":
    unittest.main()
