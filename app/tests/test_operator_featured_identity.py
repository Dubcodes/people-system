import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OperatorFeaturedIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.operator = (ROOT / "static" / "operator.html").read_text(encoding="utf-8")
        cls.featured = cls.operator[cls.operator.index("function renderFeatured()") : cls.operator.index("function renderGrid()")]

    def test_selected_person_with_image_keeps_the_existing_featured_image_path(self):
        self.assertIn('featured.innerHTML = `<img class="progressive-image"', self.featured)
        self.assertIn('loadProgressiveImage(featured.querySelector(\'img.progressive-image\'))', self.featured)
        self.assertIn('selectedPerson.thumbnail_url || selectedPerson.image_url', self.featured)

    def test_selected_person_without_image_gets_compact_identity_not_a_blank_photo(self):
        start = self.featured.index("if (!selectedPerson.image_url)")
        no_image_branch = self.featured[start : self.featured.index("featured.classList.remove('identity')", start)]
        self.assertIn("selected-person-card", no_image_branch)
        self.assertIn("selected-person-name", no_image_branch)
        self.assertIn("escapeHtml(selectedPerson.name)", no_image_branch)
        self.assertIn("primaryRole", no_image_branch)
        self.assertNotIn("<img", no_image_branch)
        self.assertIn(".featured.identity { min-height:0", self.operator)
        self.assertIn(".featured.identity { min-height:0; }", self.operator)

    def test_no_selection_removes_the_featured_area_without_a_fake_identity_card(self):
        no_selection_branch = self.featured[self.featured.index("if (!selectedPerson)") : self.featured.index("const renderKey")]
        self.assertIn("featured.classList.remove('visible')", no_selection_branch)
        self.assertIn("featured.innerHTML = ''", no_selection_branch)
        self.assertNotIn("selected-person-card", no_selection_branch)

    def test_featured_render_key_covers_image_name_and_roles_for_live_transitions(self):
        self.assertIn("selectedPerson.image_url || ''", self.featured)
        self.assertIn("selectedPerson.name || ''", self.featured)
        self.assertIn("(selectedPerson.roles || []).join('|')", self.featured)

    def test_operator_clicks_remain_temporary_highlights_not_viewer_selection(self):
        grid = self.operator[self.operator.index("function renderGrid()") : self.operator.index("function scheduleStatePoll")]
        self.assertIn("operator_highlight_person_id:Number(card.dataset.personId)", grid)
        self.assertNotIn("selected_person_id:Number(card.dataset.personId)", grid)

    def test_existing_compact_mobile_header_rules_remain_unchanged(self):
        mobile = self.operator[self.operator.index("@media (max-width:600px)") : self.operator.index("</style>")]
        self.assertIn(".page { padding:5px; gap:5px; }", mobile)
        self.assertIn(".top { gap:5px; padding:5px; border-radius:10px; }", mobile)
        self.assertIn(".compact-action, .sync-status { min-width:34px; min-height:34px; }", mobile)


if __name__ == "__main__":
    unittest.main()
