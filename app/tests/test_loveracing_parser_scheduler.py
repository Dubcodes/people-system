import unittest
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loveracing_importer.name_utils import clean_trainer_name, is_initial_style_name, split_trainer_names
from loveracing_importer.parser import parse_meeting_html, parse_race_detail_html
from loveracing_importer.scheduler import RefreshRequest, coalesce_refreshes, planned_refreshes


SAMPLE_HTML = """
<html>
  <body>
    <h1>Matamata RC @ Matamata</h1>
    <p>Wednesday, 13 May 2026</p>
    <p>Last updated 13/05/2026 11:00am</p>
    <p>Going: Soft</p>
    <p>Weather: Fine</p>
    <p>Rail: Out 4m | No Rain Last 24 Hours</p>
    <a title="Open / Close for further info" class="race-toggle toggle" href="#toggle-detail1">Open /Close</a>
    <a title="Open / Close for further info" class="race-toggle toggle" href="#toggle-detail3">Open /Close</a>
    <section id="toggle-detail1">
      <h2>Race 1</h2>
      <p>12:20 pm</p>
      <h3>MMRC LIFE MEMBERS - THANK YOU FOR YOUR SERVICE</h3>
      <p>Rating 60 HCP 2000m - $17,000</p>
      <div>Runner details</div>
      <div>1.</div><div>Horse A</div><div>4100X</div><div>Draw: 7</div><div>Rating: 60</div><div>Weight: 60.0</div><div>J: Jack Taplin (a3/53kg)</div><div>T: David Wilson</div>
      <div>10.</div><div>Pure Gold</div><div>SCR</div><div>T: Kylie Hoskin</div>
      <div>16.</div><div>Beau Ondago (AUS) (B2)</div><div>SCR</div><div>T: John Williams</div>
    </section>
    <section id="toggle-detail3">
      <h2>Race 3</h2>
      <p>1:30 pm</p>
      <h3>JOIN MMRC - NEW MEMBERS WELCOME</h3>
      <p>MDN 3YO 1200m - $17,000</p>
      <div>Runner details</div>
      <div>4.</div><div>Electric Angel</div><div>J: George Rooke</div><div>T: Stephen Marsh</div>
      <div>5.</div><div>Centurial</div><div>J: Wiremu Pinn</div><div>T: Mark Walker &amp; Sam Bergerson</div>
    </section>
  </body>
</html>
"""

FIELD_STYLE_HTML = """
<html>
  <body>
    <h1>Auckland Thoroughbred Racing @ Ellerslie</h1>
    <p>Saturday, 16 May 2026</p>
    <a title="Open / Close for further info" class="race-toggle toggle" href="#toggle-detail2">Open /Close</a>
    <li id="race2" class="race fields-download">
      <table class="overview-info">
        <tr>
          <td class="col1">2</td>
          <td class="col2">12:57 pm</td>
          <td class="col3">GOLF WAREHOUSE 2100</td>
          <td class="col4">Rating 75 Benchmark* 2100m - $40,000</td>
        </tr>
      </table>
      <div id="toggle-detail2" class="further-detail hidden">
        <div class="horses">
          <div class="rows">
            <div class="nztr-row row-header"><div class="col col-number">#</div><div class="col col-form">Form</div><div class="col col-horse">Horse</div></div>
            <div class="nztr-row"><div class="col col-number">1</div><div class="col col-form">08X83</div><img src="/Common/Image.ashx?p=%2fCommon%2fImages%2fSilks%2ftest.jpg" title="Red, royal blue sleeves" alt="silk" class="silk"><div class="col col-horse"><a>War Of Succession (AUS)</a><span class="subtext">(5g Tavistock - Ziffel)</span></div></div>
            <div class="nztr-row scratched"><div class="col col-number">16</div><div class="col col-form"></div><div class="col col-horse"><a>He's Classic</a><span class="subtext">(6g Rageese - Ali Choux)</span></div></div>
          </div>
        </div>
        <div class="horse-details">
          <div id="race2-fields" class="tab-content">
            <div class="nztr-row row-header"><div class="col col-draw">Draw</div><div class="col col-rgt">RTG</div><div class="col col-wgt">WGT</div><div class="col col-jockey">Jockey</div><div class="col col-trainer">Trainer</div><div class="col col-win">Win</div><div class="col col-place">Place</div></div>
            <div class="nztr-row"><div class="col col-draw">4</div><div class="col col-rgt">74</div><div class="col col-wgt">59.5</div><div class="col col-jockey">Hayley Hassman (a2/52kg)</div><div class="col col-trainer">Peter Coutts, Te Awamutu</div><div class="col col-win">-</div><div class="col col-place">-</div></div>
            <div class="nztr-row scratched"><div class="col col-draw"></div><div class="col col-rgt">65</div><div class="col col-wgt"></div><div class="col col-jockey"></div><div class="col col-trainer">John Williams</div><div class="col col-win"></div><div class="col col-place"></div></div>
          </div>
          <div id="race2-quick" class="tab-content">
            <div class="nztr-row"><div>16</div><div>less for 17 months; sixth by 12 lengths at Te Rapa 2400m. Capable.</div></div>
          </div>
        </div>
      </div>
    </li>
  </body>
</html>
"""

RESULT_STYLE_HTML = """
<html>
  <body>
    <h1>Auckland Thoroughbred Racing @ Ellerslie</h1>
    <p>Saturday, 6 Jun 2026</p>
    <a title="Open / Close for further info" class="race-toggle toggle" href="#toggle-detail1">Open /Close</a>
    <li id="race1" class="race fields-download">
      <table class="overview-info">
        <tr>
          <td class="col1">1</td>
          <td class="col2">11:57 am</td>
          <td class="col3">TRACKSIDE.CO.NZ 1400</td>
          <td class="col4">OPN HCP 1400m - $45,000</td>
          <td class="col5">1.25.55</td>
        </tr>
      </table>
      <div id="toggle-detail1" class="further-detail">
        <table>
          <tr><th>#</th><th>Horse</th><th>Jockey</th><th>Trainer</th><th>Win</th><th>Place</th><th>Lgth</th></tr>
          <tr><td>3</td><td>Uderzo</td><td>Warren Kennedy</td><td>Lance O'Sullivan &amp; Andrew Scott</td><td>$6.10</td><td>$2.90</td><td></td></tr>
          <tr><td>4</td><td>Sweet Smile</td><td>Masa Hashizume</td><td>Jenna Mahoney</td><td></td><td>$1.50</td><td>0.8L</td></tr>
        </table>
        <p>Other: 7-Astoria Brooke(1.6L), 2-Lhasa (AUS)(3.6L) Scratched: Agera, Mr Chow, Ballerina</p>
      </div>
    </li>
  </body>
</html>
"""


class LoveracingParserSchedulerTest(unittest.TestCase):
    def test_parser_reads_hidden_toggle_sections(self):
        parsed = parse_meeting_html(SAMPLE_HTML, "https://loveracing.nz/raceinfo/54972/meeting-overview.aspx")

        self.assertEqual(parsed.meeting_name, "Matamata RC @ Matamata")
        self.assertEqual(parsed.venue_name, "Matamata")
        self.assertEqual(len(parsed.races), 2)
        self.assertEqual(parsed.races[0].race_number, 1)
        self.assertEqual(parsed.races[0].distance_m, 2000)
        self.assertEqual(parsed.races[0].stake_text, "$17,000")
        baxter = parsed.races[0].runners[0]
        self.assertEqual(baxter.horse_name, "Horse A")
        self.assertEqual(baxter.jockey_name, "Jack Taplin")
        self.assertEqual(baxter.jockey_claim_text, "a3/53kg")
        self.assertEqual(baxter.trainer_name, "David Wilson")
        scratched = {runner.horse_name: runner for runner in parsed.races[0].runners if runner.is_scratched}
        self.assertEqual(scratched["Pure Gold"].trainer_name, "Kylie Hoskin")
        self.assertTrue(scratched["Beau Ondago"].is_ballot)
        self.assertEqual(parsed.races[1].runners[1].trainer_name, "Mark Walker & Sam Bergerson")

    def test_parser_reads_live_style_hidden_field_rows(self):
        parsed = parse_meeting_html(FIELD_STYLE_HTML, "https://loveracing.nz/RaceInfo/54974/Meeting-Overview.aspx")

        self.assertEqual(len(parsed.races), 1)
        self.assertEqual(len(parsed.races[0].runners), 2)
        first = parsed.races[0].runners[0]
        scratched = parsed.races[0].runners[1]
        self.assertEqual(first.horse_name, "War Of Succession")
        self.assertEqual(first.horse_age_sex_breeding_text, "(5g Tavistock - Ziffel)")
        self.assertEqual(first.jockey_name, "Hayley Hassman")
        self.assertEqual(first.jockey_claim_text, "a2/52kg")
        self.assertEqual(first.trainer_name, "Peter Coutts")
        self.assertEqual(first.silk_image_url, "https://loveracing.nz/Common/Image.ashx?p=%2fCommon%2fImages%2fSilks%2ftest.jpg")
        self.assertEqual(first.silk_description, "Red, royal blue sleeves")
        self.assertIsNone(first.win_odds)
        self.assertEqual(scratched.horse_name, "He's Classic")
        self.assertEqual(scratched.trainer_name, "John Williams")
        self.assertTrue(scratched.is_scratched)
        self.assertIsNone(scratched.win_odds)

    def test_parser_ignores_historical_race_detail_links(self):
        html = """
        <html><body>
          <h1>Racing Rotorua @ Arawa Park</h1>
          <p>Wednesday, 1 Jul 2026</p>
          <li id="race2" class="race fields-download">
            <table class="overview-info"><tr>
              <td class="col1">2</td><td class="col2">12:20 pm</td>
              <td class="col3"><a href="#toggle-detail2">ENTAIN/NZB INSURANCE PEARL SERIES</a></td>
              <td class="col4">MDN 2YO&UP 1215m - $17,000</td>
            </tr></table>
            <div id="toggle-detail2">
              <div>Runner details</div>
              <div>1.</div><div>Gin And Bare It</div><div>J: Test Jockey</div><div>T: Test Trainer</div>
              <a href="/RaceInfo/54984/2/Race-Detail.aspx">Previous start</a>
            </div>
          </li>
        </body></html>
        """

        parsed = parse_meeting_html(html, "https://loveracing.nz/RaceInfo/55011/Meeting-Overview.aspx")

        self.assertEqual(parsed.races[0].source_detail_url, "https://loveracing.nz/RaceInfo/55011/2/Race-Detail.aspx")
    def test_parser_reads_scratched_names_from_result_summary(self):
        parsed = parse_meeting_html(RESULT_STYLE_HTML, "https://loveracing.nz/RaceInfo/55920/Meeting-Overview.aspx")

        self.assertEqual(len(parsed.races), 1)
        race = parsed.races[0]
        self.assertEqual(race.status, "results")
        self.assertEqual(race.scratched_horse_names, ["Agera", "Mr Chow", "Ballerina"])
        self.assertEqual([runner.horse_name for runner in race.runners], ["Uderzo", "Sweet Smile"])

    def test_parser_uses_meeting_title_ahead_of_live_tab_sidebar(self):
        html = """
        <html>
          <head><title>Race Meeting for RACING TE AROHA at TE AROHA on 1 JUN 2026 | LOVERACING.NZ</title></head>
          <body>
            <div class="meeting-name">M3 MANAWATU RC @ AWAPUNI SYNTHETIC 31 MAY</div>
            <section>
              <h2>Racing Te Aroha @ Te Aroha</h2>
              <span>Last updated 01/06/2026 10:55am</span>
              <span>Monday, 1 Jun 2026</span>
              <a title="Open / Close for further info" class="race-toggle toggle" href="#toggle-detail1">Open /Close</a>
              <div id="toggle-detail1">
                <h2>Race 1</h2>
                <p>12:20 pm</p>
                <h3>TEST RACE</h3>
                <p>MDN 1200m - $17,000</p>
                <div>Runner details</div>
                <div>1.</div><div>Runner One</div><div>J: Wiremu Pinn</div><div>T: Peter Coutts, Te Awamutu</div>
              </div>
            </section>
          </body>
        </html>
        """
        parsed = parse_meeting_html(html, "https://loveracing.nz/RaceInfo/54988/Meeting-Overview.aspx")

        self.assertEqual(parsed.meeting_name, "Racing Te Aroha @ Te Aroha")
        self.assertEqual(parsed.venue_name, "Te Aroha")
        self.assertEqual(parsed.races[0].runners[0].trainer_name, "Peter Coutts")

    def test_parser_prefers_abbreviated_meeting_date_over_last_updated(self):
        html = """
        <html>
          <body>
            <h1>Racing Te Aroha @ Te Aroha</h1>
            <span>Last updated 05/06/2026 8:12am</span>
            <span>Saturday, 6 Jun 2026</span>
            <a title="Open / Close for further info" class="race-toggle toggle" href="#toggle-detail1">Open /Close</a>
            <div id="toggle-detail1">
              <h2>Race 1</h2>
              <p>12:20 pm</p>
              <h3>TEST RACE</h3>
              <p>MDN 1200m - $17,000</p>
              <div>Runner details</div>
              <div>1.</div><div>Runner One</div><div>J: Wiremu Pinn</div><div>T: Peter Coutts</div>
            </div>
          </body>
        </html>
        """
        parsed = parse_meeting_html(html, "https://loveracing.nz/RaceInfo/55901/Meeting-Overview.aspx")

        self.assertEqual(parsed.date.isoformat(), "2026-06-06")

    def test_parser_uses_header_meeting_fields_not_runner_stats(self):
        filler = "\n".join("<p>Filler line</p>" for _ in range(270))
        html = f"""
        <html>
          <body>
            <h1>Auckland Thoroughbred Racing @ Ellerslie</h1>
            <p>Saturday, 16 May 2026</p>
            <p>Going</p>
            <p>Soft5 3.23 pm 14/05/26</p>
            <p>Weather</p>
            <p>Cloudy</p>
            {filler}
            <a title="Open / Close for further info" class="race-toggle toggle" href="#toggle-detail1">Open /Close</a>
            <section id="toggle-detail1">
              <h2>Race 1</h2>
              <p>12:20 pm</p>
              <h3>TEST RACE</h3>
              <p>MDN 1200m - $17,000</p>
              <div>Runner details</div>
              <div>1.</div><div>Runner One</div><div>Going: less for 17 months; sixth by 12 lengths at Te Rapa 2400m.</div>
            </section>
          </body>
        </html>
        """
        parsed = parse_meeting_html(html, "https://loveracing.nz/RaceInfo/54974/Meeting-Overview.aspx")

        self.assertEqual(parsed.going, "Soft5")
        self.assertEqual(parsed.weather, "Cloudy")

    def test_race_detail_page_joins_horse_list_to_field_rows(self):
        base = parse_meeting_html(SAMPLE_HTML, "https://loveracing.nz/raceinfo/54972/meeting-overview.aspx").races[0]
        html = """
        <ul class="horse-list">
          <li class="nztr-row headings"></li>
          <li class="nztr-row"><a><div class="column race-number placing">12</div><div class="column race-number startnumber">1</div><div class="column horse">Baxter<span class="jockey">Jack Taplin (A)</span></div></a></li>
          <li class="nztr-row scratched"><a><div class="column race-number startnumber">10</div><div class="column horse">Pure Gold<span class="jockey"></span></div></a></li>
        </ul>
        <ul id="overview" class="no-bullets field tab-content">
          <li class="nztr-row headings"><div>Bar</div><div>SP</div><div>Weight</div><div>Trainer</div></li>
          <li class="nztr-row"><div>7</div><div>$21.80</div><div>60.0</div><div>David Wilson</div></li>
          <li class="nztr-row scratched"><div></div><div></div><div>56.5</div><div>Kylie Hoskin</div></li>
        </ul>
        """
        race = parse_race_detail_html(html, "https://loveracing.nz/RaceInfo/54972/1/Race-Detail.aspx", base)

        self.assertEqual(len(race.runners), 2)
        self.assertEqual(race.runners[0].horse_name, "Baxter")
        self.assertEqual(race.runners[0].jockey_name, "Jack Taplin")
        self.assertEqual(race.runners[0].jockey_claim_text, "A")
        self.assertEqual(race.runners[0].trainer_name, "David Wilson")
        self.assertEqual(race.runners[0].barrier, 7)
        self.assertEqual(race.runners[1].trainer_name, "Kylie Hoskin")
        self.assertTrue(race.runners[1].is_scratched)

    def test_scheduler_coalesces_medium_and_high_priority(self):
        medium = [
            RefreshRequest(1, datetime(2026, 5, 13, 11, 45), "medium", "2h"),
            RefreshRequest(2, datetime(2026, 5, 13, 12, 15), "medium", "2h"),
        ]
        high = [
            RefreshRequest(1, datetime(2026, 5, 13, 12, 5), "high", "15m"),
            RefreshRequest(2, datetime(2026, 5, 13, 12, 13), "high", "15m"),
        ]

        medium_batch = coalesce_refreshes(medium)[0]
        high_batch = coalesce_refreshes(high)[0]

        self.assertEqual(medium_batch["run_at"], datetime(2026, 5, 13, 12, 0))
        self.assertEqual(medium_batch["meeting_ids"], [1, 2])
        self.assertEqual(high_batch["run_at"], datetime(2026, 5, 13, 12, 5))
        self.assertEqual(high_batch["meeting_ids"], [1, 2])

    def test_scheduler_plans_morning_and_daytime_scratching_refreshes(self):
        race_times = [
            datetime(2026, 6, 6, 11, 57),
            datetime(2026, 6, 6, 15, 42),
        ]

        requests = planned_refreshes(9, race_times)
        by_reason = {}
        for request in requests:
            by_reason.setdefault(request.reason, []).append(request.due_at)

        self.assertIn(datetime(2026, 6, 6, 7, 0), by_reason["7am_scratching_check"])
        self.assertIn(race_times[0] - timedelta(hours=2), by_reason["2h_before_first_race"])
        self.assertIn(race_times[0] - timedelta(hours=1), by_reason["1h_before_first_race"])
        self.assertIn(race_times[0] - timedelta(minutes=15), by_reason["15m_before_race"])
        self.assertIn(datetime(2026, 6, 6, 8, 0), by_reason["race_day_hourly_refresh"])
        self.assertIn(datetime(2026, 6, 6, 16, 0), by_reason["race_day_hourly_refresh"])

    def test_loveracing_trainer_partnership_names_split_to_people(self):
        self.assertEqual(split_trainer_names("Mark Walker & Sam Bergerson"), ["Mark Walker", "Sam Bergerson"])
        self.assertEqual(split_trainer_names("Peter & Jessica Brosnan"), ["Peter Brosnan", "Jessica Brosnan"])
        self.assertEqual(split_trainer_names("Conor and Sarah Fahy"), ["Conor Fahy", "Sarah Fahy"])
        self.assertEqual(split_trainer_names("G A & D G Rogerson & Clint Isdale"), ["G A Rogerson", "D G Rogerson", "Clint Isdale"])
        self.assertEqual(clean_trainer_name("Peter Coutts, Te Awamutu"), "Peter Coutts")
        self.assertEqual(split_trainer_names("Peter Brosnan, Matamata"), ["Peter Brosnan"])
        self.assertEqual(split_trainer_names("Peter & Jessica Brosnan, Matamata"), ["Peter Brosnan", "Jessica Brosnan"])
        self.assertTrue(is_initial_style_name("G A Rogerson"))


if __name__ == "__main__":
    unittest.main()
