import re
from datetime import date, datetime, time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

from loveracing_importer.fetcher import meeting_id_from_url
from loveracing_importer.models import ParsedMeeting, ParsedRace, ParsedRunner
from loveracing_importer.name_utils import clean_horse_name as _clean_horse_name, clean_trainer_name as _clean_trainer_name


PARSER_VERSION = "loveracing-html-v13"
MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

MEETING_HEADER_SEARCH_LINES = 260
MEETING_LABELS = {"going", "weather", "rail", "track", "straight"}


class HtmlNode:
    def __init__(self, tag: str = "document", attrs: dict[str, str] | None = None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children: list[HtmlNode | str] = []

    def text(self, separator: str = "\n") -> str:
        parts = []

        def walk(node):
            for child in node.children:
                if isinstance(child, str):
                    value = " ".join(unescape(child).split())
                    if value:
                        parts.append(value)
                else:
                    walk(child)

        walk(self)
        return separator.join(parts)

    def lines(self) -> list[str]:
        return [_clean(line) for line in self.text("\n").splitlines() if _clean(line)]

    def find_all(self, tag: str | None = None, **attrs) -> list["HtmlNode"]:
        matches = []

        def walk(node):
            if (tag is None or node.tag == tag) and all(node.attrs.get(key) == value for key, value in attrs.items()):
                matches.append(node)
            for child in node.children:
                if isinstance(child, HtmlNode):
                    walk(child)

        walk(self)
        return matches

    def closest(self, tag: str | None = None, id_pattern: str | None = None) -> "HtmlNode | None":
        node = self.parent
        compiled = re.compile(id_pattern) if id_pattern else None
        while node:
            if tag and node.tag != tag:
                node = node.parent
                continue
            if compiled and not compiled.search(node.attrs.get("id", "")):
                node = node.parent
                continue
            return node
        return None


class TreeParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = HtmlNode(tag.lower(), {key: value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, data):
        if self.stack[-1].tag in {"script", "style"}:
            return
        if data and data.strip():
            self.stack[-1].children.append(data)


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split()).strip()


def _int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


TEXT_DATE_RE = re.compile(
    r"(?:(?:Mon(?:day)?|Tue(?:s|sday)?|Wed(?:nesday)?|Thu(?:rs|rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?),?\s*)?"
    r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})",
    re.I,
)


def _date_from_match(match: re.Match | None) -> date | None:
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower().rstrip("."))
    if not month:
        return None
    return date(int(match.group(3)), month, int(match.group(1)))


def _parse_date(text: str) -> date | None:
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    for line in lines[:MEETING_HEADER_SEARCH_LINES]:
        if "last updated" in line.lower():
            continue
        parsed = _date_from_match(TEXT_DATE_RE.search(line))
        if parsed:
            return parsed

    for match in TEXT_DATE_RE.finditer(text):
        context = text[max(0, match.start() - 40) : match.end() + 20].lower()
        if "last updated" in context:
            continue
        parsed = _date_from_match(match)
        if parsed:
            return parsed

    for line in lines[:MEETING_HEADER_SEARCH_LINES]:
        if "last updated" in line.lower():
            continue
        match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", line)
        if match:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))

    match = None
    for candidate in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        context = text[max(0, candidate.start() - 40) : candidate.end() + 20].lower()
        if "last updated" not in context:
            match = candidate
            break
    if match:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    return None


def _parse_last_updated(text: str, meeting_date: date | None) -> datetime | None:
    match = re.search(r"last\s+updated\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap]m)?", text, re.I)
    if not match:
        return None
    hour = int(match.group(4))
    minute = int(match.group(5))
    suffix = (match.group(6) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return datetime.combine(date(int(match.group(3)), int(match.group(2)), int(match.group(1))), time(hour, minute))


def _is_empty_label_value(value: str | None) -> bool:
    cleaned = _clean(value)
    if not cleaned or re.fullmatch(r"[:|\-\s]+", cleaned):
        return True
    return cleaned.lower().rstrip(":").strip() in MEETING_LABELS


def _trim_meeting_label_value(label: str, value: str) -> str:
    cleaned = _clean(value.lstrip(": "))
    if label.lower() == "going":
        trimmed = re.sub(r"\s+\d{1,2}\.\d{2}\s*(?:am|pm)?\s+\d{1,2}/\d{1,2}/\d{2,4}.*$", "", cleaned, flags=re.I)
        return _clean(trimmed) or cleaned
    return cleaned


def _label_value(lines: list[str], label: str) -> str | None:
    search_lines = lines[:MEETING_HEADER_SEARCH_LINES]
    pattern = re.compile(rf"^{re.escape(label)}\s*:?\s*(.+)$", re.I)
    for line in search_lines:
        match = pattern.match(line)
        if match:
            value = _trim_meeting_label_value(label, match.group(1))
            if not _is_empty_label_value(value):
                return value
    label_pattern = re.compile(rf"^{re.escape(label)}\s*:?\s*$", re.I)
    for index, line in enumerate(search_lines[:-1]):
        if label_pattern.match(line):
            for candidate in search_lines[index + 1 : index + 4]:
                value = _trim_meeting_label_value(label, candidate)
                if not _is_empty_label_value(value):
                    return value
    return None


def _title_case_label(value: str | None) -> str:
    acronyms = {"ATR", "WTR", "RC", "RI", "JC", "NZTR"}
    words = []
    for word in _clean(value).split():
        if word.upper() in acronyms:
            words.append(word)
        else:
            words.append(word.capitalize() if word.isupper() else word)
    return _clean(" ".join(words))


def _meeting_name_from_title(lines: list[str]) -> str | None:
    for line in lines[:8]:
        match = re.search(r"Race Meeting for\s+(.+?)\s+at\s+(.+?)\s+on\s+\d{1,2}\s+[A-Z]{3,9}\s+\d{4}", line, re.I)
        if match:
            return f"{_title_case_label(match.group(1))} @ {_title_case_label(match.group(2))}"
    return None


def _looks_like_live_tab_meeting(value: str | None) -> bool:
    return bool(re.match(r"^M\d+\s+.+\s+\d{1,2}\s+[A-Z]{3}$", _clean(value), re.I))


def _meeting_name(lines: list[str]) -> str | None:
    title_name = _meeting_name_from_title(lines)
    if title_name:
        return title_name
    for line in lines:
        match = re.search(r"(.+?)\s+Last updated\b", line, re.I)
        if match and len(match.group(1)) > 4:
            return _clean(match.group(1))
    for index, line in enumerate(lines):
        if re.search(r"^Last updated\b", line, re.I):
            for candidate in reversed(lines[max(0, index - 8) : index]):
                if " @ " in candidate and not _looks_like_live_tab_meeting(candidate):
                    return candidate
    for line in lines:
        if " @ " in line and not line.lower().startswith("loveracing") and not _looks_like_live_tab_meeting(line):
            return line
    title_candidates = [line for line in lines[:40] if "race" not in line.lower() and len(line) > 4]
    return title_candidates[0] if title_candidates else None


def _split_club_venue(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    if " @ " in name:
        club, venue = name.split(" @ ", 1)
        return _clean(club), _clean(venue)
    return None, name


def _detail_nodes(root: HtmlNode) -> list[tuple[int, HtmlNode]]:
    anchors = root.find_all("a")
    ids = []
    for anchor in anchors:
        href = anchor.attrs.get("href", "")
        match = re.fullmatch(r"#(toggle-detail(\d+))", href)
        if match:
            ids.append((int(match.group(2)), match.group(1)))
    if not ids:
        for node in root.find_all():
            node_id = node.attrs.get("id", "")
            match = re.fullmatch(r"toggle-detail(\d+)", node_id)
            if match:
                ids.append((int(match.group(1)), node_id))
    found = []
    for number, node_id in sorted(set(ids)):
        node = next((item for item in root.find_all() if item.attrs.get("id") == node_id), None)
        if node:
            found.append((number, node))
    return found


def _direct_text(node: HtmlNode) -> str:
    parts = []
    for child in node.children:
        if isinstance(child, str):
            value = _clean(child)
            if value:
                parts.append(value)
        elif child.tag in {"a", "span", "div", "strong", "em"}:
            value = _clean(child.text(" "))
            if value:
                parts.append(value)
    return _clean(" ".join(parts))


def _table_rows(table: HtmlNode) -> list[list[HtmlNode]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [child for child in tr.children if isinstance(child, HtmlNode) and child.tag in {"td", "th"}]
        if cells:
            rows.append(cells)
    return rows


def _cell_text(cells: list[HtmlNode], index: int) -> str | None:
    if index >= len(cells):
        return None
    return _clean(cells[index].text(" "))


def _header_map(table: HtmlNode) -> dict[str, int]:
    headers = {}
    for cells in _table_rows(table):
        if any(cell.tag == "th" for cell in cells):
            for index, cell in enumerate(cells):
                label = _clean(cell.text(" ")).lower().replace("#", "number")
                if label:
                    headers[label] = index
            break
    return headers


def _is_nominations_table(table: HtmlNode) -> bool:
    headers = set(_header_map(table))
    required = {"silk", "horse", "trainer", "age", "sex", "order", "rating", "weight"}
    if required.issubset(headers) and not any("jockey" in label or "j/d/t" in label for label in headers):
        return True
    return any("sal-nominations-" in cell.attrs.get("class", "").lower() for row in _table_rows(table) for cell in row)


def _meeting_stage(root: HtmlNode) -> str:
    if any(_is_nominations_table(table) for table in root.find_all("table")):
        return "nominations"
    return "fields"


def _column(headers: dict[str, int], *names: str, fallback: int | None = None) -> int | None:
    for name in names:
        normalized = name.lower()
        for label, index in headers.items():
            if normalized == label or normalized in label:
                return index
    return fallback


def _race_header(lines: list[str], fallback_number: int) -> tuple[int, str | None, str | None, str | None]:
    race_number = fallback_number
    start_time = None
    name = None
    conditions = None
    for index, line in enumerate(lines[:25]):
        race_match = re.search(r"\bRace\s+(\d+)\b", line, re.I)
        if race_match:
            race_number = int(race_match.group(1))
        time_match = re.search(r"\b(\d{1,2}:\d{2}\s*(?:am|pm)?)\b", line, re.I)
        if time_match and not start_time:
            start_time = time_match.group(1).upper().replace(" ", "")
        if not name and index > 0 and len(line) > 8 and not re.search(r"^(race|runner|field|jockey|trainer|draw|rating|weight)\b", line, re.I) and not time_match:
            name = line
    for line in lines[:40]:
        if re.search(r"\b\d{3,4}m\b", line, re.I) or "$" in line:
            conditions = line
            break
    return race_number, start_time, name, conditions


def _race_header_from_container(container: HtmlNode, fallback_number: int) -> tuple[int, str | None, str | None, str | None]:
    overview = next((table for table in container.find_all("table") if "overview-info" in table.attrs.get("class", "")), None)
    if not overview:
        return _race_header(container.lines(), fallback_number)
    rows = _table_rows(overview)
    cells = rows[0] if rows else []
    race_number = _int(_cell_text(cells, 0)) or fallback_number
    start_time = _cell_text(cells, 1)
    race_name = _cell_text(cells, 2)
    conditions = _cell_text(cells, 3)
    return race_number, start_time, race_name, conditions


def _parse_conditions(value: str | None) -> tuple[str | None, int | None, str | None]:
    if not value:
        return None, None, None
    distance = _int(re.search(r"\b(\d{3,4})m\b", value, re.I).group(1) if re.search(r"\b(\d{3,4})m\b", value, re.I) else None)
    stake_match = re.search(r"\$[\d,]+", value)
    stake = stake_match.group(0) if stake_match else None
    conditions = re.sub(r"\s*-\s*\$[\d,]+.*$", "", value)
    conditions = re.sub(r"\b\d{3,4}m\b", "", conditions, flags=re.I)
    return _clean(conditions) or value, distance, stake


def _is_runner_start(line: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}\.?", line)) or bool(re.match(r"^\d{1,2}\s+[A-Z0-9'().& -]{2,}", line))


def _status_flags(text: str, horse_name: str | None) -> tuple[str, bool, bool]:
    upper = text.upper()
    scratched = "SCR" in upper or "SCRATCHED" in upper
    ballot = bool(re.search(r"\(B\d+\)", horse_name or "", re.I)) or bool(re.search(r"\bB\d+\b", text, re.I))
    if scratched:
        return "scratched", True, ballot
    if ballot:
        return "ballot", False, True
    return "active", False, False


def _parse_jockey_claim(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    match = re.search(r"^(.*?)\s*\(([^)]{1,30})\)\s*$", value, re.I)
    if match:
        return _clean(match.group(1)), _clean(match.group(2))
    return value, None


def _clean_odds(value: str | None) -> str | None:
    cleaned = _clean(value)
    if not cleaned or cleaned in {"-", "$"}:
        return None
    if len(cleaned) > 20:
        return None
    if re.fullmatch(r"\$?\d+(?:\.\d{1,2})?", cleaned):
        return cleaned
    return None


def _horse_match_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean_horse_name(value).lower())


def _result_scratched_names(lines: list[str]) -> list[str]:
    raw_parts = []
    collecting = False
    for line in lines:
        if re.search(r"\bscratched\s*:", line, re.I):
            collecting = True
            raw_parts.append(re.split(r"\bscratched\s*:", line, maxsplit=1, flags=re.I)[1])
            continue
        if not collecting:
            continue
        if re.search(
            r"^(race|start|name|conditions|#|fields|quick|trials|gear changes|draw|rtg|wgt|jockey|trainer|win|place|owners:|other:|download|join in)\b",
            line,
            re.I,
        ):
            break
        raw_parts.append(line)
    if not raw_parts:
        return []
    raw = _clean(" ".join(raw_parts))
    raw = re.split(r"\b(?:gear changes|race replay|dividends|fields|quick|trials|barrier stats|download pdf)\b", raw, maxsplit=1, flags=re.I)[0]
    names = []
    seen = set()
    for part in raw.split(","):
        name = _clean_horse_name(part)
        if not name or name.lower() in {"nil", "none", "n/a", "-"}:
            continue
        key = _horse_match_key(name)
        if key and key not in seen:
            names.append(name)
            seen.add(key)
    return names


def _apply_result_scratches(runners: list[ParsedRunner], scratched_names: list[str]) -> None:
    scratched_keys = {_horse_match_key(name) for name in scratched_names if _horse_match_key(name)}
    if not scratched_keys:
        return
    for runner in runners:
        if _horse_match_key(runner.horse_name) in scratched_keys:
            runner.status = "scratched"
            runner.is_scratched = True
            if runner.jockey_name and runner.jockey_name.lower() in {"scr", "scratched"}:
                runner.jockey_name = None


def _parse_runner_block(block: list[str]) -> ParsedRunner | None:
    text = " | ".join(block)
    if not block:
        return None
    first = block[0]
    inline = re.match(r"^(?P<number>\d{1,2})\.?\s+(?P<name>.+)$", first)
    runner_number = int(inline.group("number")) if inline else _int(first)
    horse_name = _clean(inline.group("name")) if inline else (block[1] if len(block) > 1 else None)
    if horse_name:
        horse_name = re.sub(r"\s+\b(J:|T:|Draw|Barrier|Rating|Weight)\b.*$", "", horse_name, flags=re.I).strip()
        horse_name = _clean_horse_name(horse_name)
    form = None
    trainer = None
    jockey = None
    barrier = None
    rating = None
    weight = None
    win_odds = None
    place_odds = None
    breeding = None
    for index, line in enumerate(block):
        lower = line.lower()
        if re.fullmatch(r"[0-9xX-]{2,8}", line) and not form:
            form = line
        if lower.startswith("j:") or lower.startswith("jockey"):
            jockey = re.sub(r"^(j:|jockey:?)\s*", "", line, flags=re.I).strip()
        elif lower.startswith("t:") or lower.startswith("trainer"):
            trainer = _clean_trainer_name(re.sub(r"^(t:|trainer:?)\s*", "", line, flags=re.I).strip())
        elif lower.startswith("draw") or lower.startswith("barrier"):
            barrier = _int(line)
        elif lower.startswith("rating"):
            rating = _int(line)
        elif lower.startswith("weight"):
            weight = re.sub(r"^weight:?\s*", "", line, flags=re.I).strip()
        elif lower.startswith("win"):
            win_odds = _clean_odds(re.sub(r"^win:?\s*", "", line, flags=re.I).strip())
        elif lower.startswith("place"):
            place_odds = _clean_odds(re.sub(r"^place:?\s*", "", line, flags=re.I).strip())
        elif index > 1 and not breeding and re.search(r"\b(g|m|f|c|h)\b| by ", line, re.I):
            breeding = line
    if not jockey:
        match = re.search(r"\bJ:\s*([^|]+)", text, re.I)
        jockey = _clean(match.group(1)) if match else None
    if not trainer:
        match = re.search(r"\bT:\s*([^|]+)", text, re.I)
        trainer = _clean_trainer_name(match.group(1)) if match else None
    if barrier is None:
        match = re.search(r"\b(?:draw|barrier)\s*:?\s*(\d+)", text, re.I)
        barrier = int(match.group(1)) if match else None
    if rating is None:
        match = re.search(r"\brating\s*:?\s*(\d+)", text, re.I)
        rating = int(match.group(1)) if match else None
    if not weight:
        match = re.search(r"\b(?:weight|wt)\s*:?\s*([0-9.]+(?:kg)?)", text, re.I)
        weight = match.group(1) if match else None
    if not horse_name or not runner_number:
        return None
    status, scratched, ballot = _status_flags(text, horse_name)
    jockey, claim = _parse_jockey_claim(jockey)
    if scratched and jockey and jockey.lower() in {"scr", "scratched"}:
        jockey = None
    return ParsedRunner(
        runner_number=runner_number,
        horse_name=horse_name,
        horse_age_sex_breeding_text=breeding,
        form=form,
        barrier=barrier,
        rating=rating,
        weight=weight,
        jockey_name=jockey,
        jockey_claim_text=claim,
        trainer_name=trainer,
        win_odds=win_odds,
        place_odds=place_odds,
        status=status,
        is_scratched=scratched,
        is_ballot=ballot,
    )


def _parse_table_runner(cells: list[HtmlNode], headers: dict[str, int]) -> ParsedRunner | None:
    if any(cell.tag == "th" for cell in cells):
        return None
    row_text = " | ".join(_clean(cell.text(" ")) for cell in cells)
    if not _clean(row_text) or "owners:" in row_text.lower():
        return None
    number_index = _column(headers, "number", fallback=0)
    horse_index = _column(headers, "horse", "name", fallback=2)
    jockey_index = _column(headers, "jockey", "j/d/t", fallback=3)
    trainer_index = _column(headers, "trainer", fallback=4)
    win_index = _column(headers, "win")
    place_index = _column(headers, "place")
    barrier_index = _column(headers, "draw", "barrier")
    rating_index = _column(headers, "rating")
    weight_index = _column(headers, "weight", "wgt")
    form_index = _column(headers, "form")

    runner_number = _int(_cell_text(cells, number_index or 0))
    horse_name = _clean_horse_name(_cell_text(cells, horse_index or 0))
    jockey_name = _cell_text(cells, jockey_index) if jockey_index is not None else None
    trainer_name = _clean_trainer_name(_cell_text(cells, trainer_index)) if trainer_index is not None else None
    if not runner_number or not horse_name:
        return None
    status, scratched, ballot = _status_flags(row_text, horse_name)
    jockey_name, claim = _parse_jockey_claim(jockey_name)
    if scratched and jockey_name and jockey_name.lower() in {"scr", "scratched"}:
        jockey_name = None
    win = _clean_odds(_cell_text(cells, win_index)) if win_index is not None else None
    place = _clean_odds(_cell_text(cells, place_index)) if place_index is not None else None
    return ParsedRunner(
        runner_number=runner_number,
        horse_name=horse_name,
        form=_cell_text(cells, form_index) if form_index is not None else None,
        barrier=_int(_cell_text(cells, barrier_index)) if barrier_index is not None else None,
        rating=_int(_cell_text(cells, rating_index)) if rating_index is not None else None,
        weight=_cell_text(cells, weight_index) if weight_index is not None else None,
        jockey_name=jockey_name,
        jockey_claim_text=claim,
        trainer_name=trainer_name,
        win_odds=win,
        place_odds=place,
        status=status,
        is_scratched=scratched,
        is_ballot=ballot,
    )


def _parse_table_runners(detail_node: HtmlNode) -> list[ParsedRunner]:
    headers = _header_map(detail_node)
    runners = []
    seen = set()
    for cells in _table_rows(detail_node):
        runner = _parse_table_runner(cells, headers)
        if runner and runner.runner_number not in seen:
            runners.append(runner)
            seen.add(runner.runner_number)
    return runners


def _parse_nomination_runners(detail_node: HtmlNode, source_url: str) -> list[ParsedRunner]:
    table = detail_node if detail_node.tag == "table" else next(
        (item for item in detail_node.find_all("table") if _is_nominations_table(item)),
        None,
    )
    if not table or not _is_nominations_table(table):
        return []
    headers = _header_map(table)
    horse_index = _column(headers, "horse")
    trainer_index = _column(headers, "trainer")
    age_index = _column(headers, "age")
    sex_index = _column(headers, "sex")
    rating_index = _column(headers, "rating")
    weight_index = _column(headers, "weight")
    if horse_index is None or trainer_index is None:
        return []
    runners = []
    seen = set()
    for cells in _table_rows(table):
        if any(cell.tag == "th" for cell in cells) or horse_index >= len(cells):
            continue
        horse_cell = cells[horse_index]
        horse_anchor = next(
            (
                anchor
                for anchor in horse_cell.find_all("a")
                if "entrydetail.aspx" in anchor.attrs.get("href", "").lower()
                and "horseid=" in anchor.attrs.get("href", "").lower()
            ),
            None,
        )
        if not horse_anchor:
            continue
        horse_name = _clean_horse_name(horse_anchor.text(" "))
        key = _horse_match_key(horse_name)
        if not key or key in seen:
            continue
        seen.add(key)
        age = _cell_text(cells, age_index) if age_index is not None else None
        sex = _cell_text(cells, sex_index) if sex_index is not None else None
        weight = _cell_text(cells, weight_index) if weight_index is not None else None
        silk_image_url, silk_description = _silk_data(cells[0], source_url)
        runners.append(
            ParsedRunner(
                runner_number=len(runners) + 1,
                horse_name=horse_name,
                horse_age_sex_breeding_text=_clean(" ".join(value for value in (age, sex) if value)) or None,
                rating=_int(_cell_text(cells, rating_index)) if rating_index is not None else None,
                weight=weight if weight and weight != "-" else None,
                jockey_name=None,
                trainer_name=_clean_trainer_name(_cell_text(cells, trainer_index)),
                status="nominated",
                silk_image_url=silk_image_url,
                silk_description=silk_description,
            )
        )
    return runners


def _runner_blocks(lines: list[str]) -> list[list[str]]:
    starts = [index for index, line in enumerate(lines) if _is_runner_start(line)]
    blocks = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = lines[start:end]
        if len(block) >= 2 or re.match(r"^\d{1,2}\s+\S+", block[0]):
            blocks.append(block)
    return blocks


def _parse_runners(lines: list[str]) -> list[ParsedRunner]:
    lowered = [line.lower() for line in lines]
    start = 0
    for index, line in enumerate(lowered):
        if "runner details" in line or line in {"runner", "runners", "field"}:
            start = index + 1
            break
    trimmed = []
    for line in lines[start:]:
        if re.search(r"^(dividends|results|race replay|also nominated|betting|fixed odds)", line, re.I):
            break
        trimmed.append(line)
    runners = []
    seen = set()
    for block in _runner_blocks(trimmed):
        runner = _parse_runner_block(block)
        if runner and runner.runner_number not in seen:
            seen.add(runner.runner_number)
            runners.append(runner)
    return runners


def _detail_href(container: HtmlNode, source_url: str, race_number: int) -> str | None:
    source_meeting_id = meeting_id_from_url(source_url)
    expected_path = f"/RaceInfo/{source_meeting_id}/{race_number}/Race-Detail.aspx" if source_meeting_id else None
    for anchor in container.find_all("a"):
        href = anchor.attrs.get("href", "")
        if expected_path and href.lower().split("?", 1)[0].rstrip("/") == expected_path.lower().rstrip("/"):
            return urljoin(source_url, href)
    if expected_path:
        return urljoin(source_url, expected_path)
    return None


def _parse_race(number: int, node: HtmlNode, source_url: str, stage: str = "fields") -> ParsedRace:
    container = node.closest("li", r"race\d+") or node.parent or node
    lines = container.lines()
    race_number, start_time, name, raw_conditions = _race_header_from_container(container, number)
    conditions, distance, stake = _parse_conditions(raw_conditions)
    runners = (
        _parse_nomination_runners(node, source_url)
        if stage == "nominations"
        else _parse_meeting_detail_runners(node, source_url) or _parse_table_runners(node) or _parse_runners(node.lines())
    )
    scratched_names = _result_scratched_names(node.lines())
    _apply_result_scratches(runners, scratched_names)
    headers = _header_map(node)
    status = "nominations" if stage == "nominations" else ("results" if any("lgth" in label or "length" in label for label in headers) else None)
    return ParsedRace(
        race_number=race_number,
        source_detail_url=_detail_href(container, source_url, race_number),
        start_time=start_time,
        race_name=name,
        conditions=conditions,
        distance_m=distance,
        stake_text=stake,
        status=status,
        runners=runners,
        scratched_horse_names=scratched_names,
    )


def _class_contains(node: HtmlNode, token: str) -> bool:
    return token in (node.attrs.get("class", "").lower().split())


def _class_has(node: HtmlNode, token: str) -> bool:
    return token.lower() in node.attrs.get("class", "").lower()


def _first_by_class(node: HtmlNode, token: str) -> HtmlNode | None:
    return next((item for item in node.find_all() if _class_has(item, token)), None)


def _direct_child_by_class(node: HtmlNode, token: str) -> HtmlNode | None:
    return next(
        (
            child
            for child in node.children
            if isinstance(child, HtmlNode) and _class_has(child, token)
        ),
        None,
    )


def _direct_children_by_class(node: HtmlNode, token: str) -> list[HtmlNode]:
    return [
        child
        for child in node.children
        if isinstance(child, HtmlNode) and _class_has(child, token)
    ]


def _silk_data(node: HtmlNode, source_url: str) -> tuple[str | None, str | None]:
    image = next(
        (
            item
            for item in node.find_all("img")
            if _class_has(item, "silk") or item.attrs.get("alt", "").lower() == "silk"
        ),
        None,
    )
    if not image:
        return None, None
    src = _clean(image.attrs.get("src"))
    description = _clean(image.attrs.get("title")) or _clean(image.attrs.get("alt"))
    if description.lower() == "silk":
        description = None
    return (urljoin(source_url, src) if src else None), description


def _row_values_from_divs(row: HtmlNode) -> list[str]:
    return [_clean(child.text(" ")) for child in row.children if isinstance(child, HtmlNode) and child.tag == "div"]


def _parse_meeting_horse_row(row: HtmlNode, source_url: str) -> ParsedRunner | None:
    number_node = _direct_child_by_class(row, "col-number")
    form_node = _direct_child_by_class(row, "col-form")
    horse_node = _direct_child_by_class(row, "col-horse")
    if not number_node or not horse_node:
        return None
    runner_number = _int(number_node.text(" "))
    horse_link = next((link for link in horse_node.find_all("a") if _clean(link.text(" "))), None)
    horse_name = _clean(horse_link.text(" ")) if horse_link else _clean(horse_node.text(" "))
    breeding_node = _first_by_class(horse_node, "subtext")
    breeding = _clean(breeding_node.text(" ")) if breeding_node else None
    if breeding and horse_name.endswith(breeding):
        horse_name = _clean(horse_name[: -len(breeding)])
    horse_name = _clean_horse_name(horse_name)
    if not runner_number or not horse_name:
        return None
    status, scratched, ballot = _status_flags(row.text(" "), horse_name)
    if _class_has(row, "scratched"):
        status = "scratched"
        scratched = True
    silk_image_url, silk_description = _silk_data(row, source_url)
    return ParsedRunner(
        runner_number=runner_number,
        horse_name=horse_name,
        horse_age_sex_breeding_text=breeding,
        form=_clean(form_node.text(" ")) if form_node else None,
        status=status,
        is_scratched=scratched,
        is_ballot=ballot,
        silk_image_url=silk_image_url,
        silk_description=silk_description,
    )


def _parse_meeting_field_row(row: HtmlNode) -> dict[str, str | int | bool | None]:
    jockey_node = _direct_child_by_class(row, "col-jockey")
    jockey, claim = _parse_jockey_claim(_clean(jockey_node.text(" ")) if jockey_node else None)
    if _class_has(row, "scratched") and jockey and jockey.lower() in {"scr", "scratched"}:
        jockey = None
    trainer_node = _direct_child_by_class(row, "col-trainer")
    return {
        "barrier": _int(_direct_child_by_class(row, "col-draw").text(" ") if _direct_child_by_class(row, "col-draw") else None),
        "rating": _int(_direct_child_by_class(row, "col-rgt").text(" ") if _direct_child_by_class(row, "col-rgt") else None),
        "weight": _clean(_direct_child_by_class(row, "col-wgt").text(" ")) if _direct_child_by_class(row, "col-wgt") else None,
        "jockey_name": jockey,
        "jockey_claim_text": claim,
        "trainer_name": _clean_trainer_name(trainer_node.text(" ")) if trainer_node else None,
        "win_odds": _clean_odds(_direct_child_by_class(row, "col-win").text(" ") if _direct_child_by_class(row, "col-win") else None),
        "place_odds": _clean_odds(_direct_child_by_class(row, "col-place").text(" ") if _direct_child_by_class(row, "col-place") else None),
        "is_scratched": _class_has(row, "scratched"),
    }


def _parse_meeting_detail_runners(detail_node: HtmlNode, source_url: str) -> list[ParsedRunner]:
    horses_node = _first_by_class(detail_node, "horses")
    fields_node = next(
        (
            node
            for node in detail_node.find_all("div")
            if re.search(r"race\d+-fields$", node.attrs.get("id", ""), re.I)
        ),
        None,
    )
    if not horses_node or not fields_node:
        return []
    horse_rows = [
        row
        for row in horses_node.find_all("div")
        if _class_has(row, "nztr-row") and not _class_has(row, "row-header")
    ]
    field_rows = [
        row
        for row in _direct_children_by_class(fields_node, "nztr-row")
        if not _class_has(row, "row-header")
    ]
    runners = []
    seen = set()
    for index, row in enumerate(horse_rows):
        runner = _parse_meeting_horse_row(row, source_url)
        if not runner or runner.runner_number in seen:
            continue
        if index < len(field_rows):
            details = _parse_meeting_field_row(field_rows[index])
            runner.barrier = details["barrier"] if details["barrier"] is not None else runner.barrier
            runner.rating = details["rating"] if details["rating"] is not None else runner.rating
            runner.weight = details["weight"] or runner.weight
            runner.jockey_name = details["jockey_name"] or runner.jockey_name
            runner.jockey_claim_text = details["jockey_claim_text"] or runner.jockey_claim_text
            runner.trainer_name = details["trainer_name"] or runner.trainer_name
            runner.win_odds = details["win_odds"] or runner.win_odds
            runner.place_odds = details["place_odds"] or runner.place_odds
            if details["is_scratched"]:
                runner.status = "scratched"
                runner.is_scratched = True
        if runner.is_scratched and runner.jockey_name and runner.jockey_name.lower() in {"scr", "scratched"}:
            runner.jockey_name = None
        runners.append(runner)
        seen.add(runner.runner_number)
    return runners


def _parse_horse_list_row(row: HtmlNode, source_url: str) -> ParsedRunner | None:
    start_node = _first_by_class(row, "startnumber")
    horse_node = _first_by_class(row, "horse")
    if not start_node or not horse_node:
        return None
    runner_number = _int(start_node.text(" "))
    horse_text = _clean(horse_node.text(" "))
    jockey_node = _first_by_class(horse_node, "jockey")
    jockey = _clean(jockey_node.text(" ")) if jockey_node else None
    if jockey and horse_text.endswith(jockey):
        horse_text = _clean(horse_text[: -len(jockey)])
    horse_text = _clean_horse_name(horse_text)
    status, scratched, ballot = _status_flags(row.text(" "), horse_text)
    if _class_has(row, "scratched"):
        status = "scratched"
        scratched = True
    jockey, claim = _parse_jockey_claim(jockey)
    if not runner_number or not horse_text:
        return None
    silk_image_url, silk_description = _silk_data(row, source_url)
    return ParsedRunner(
        runner_number=runner_number,
        horse_name=horse_text,
        jockey_name=jockey,
        jockey_claim_text=claim,
        status=status,
        is_scratched=scratched,
        is_ballot=ballot,
        silk_image_url=silk_image_url,
        silk_description=silk_description,
    )


def _detail_overview_rows(root: HtmlNode) -> tuple[list[str], list[HtmlNode]]:
    overview = next((node for node in root.find_all("ul") if node.attrs.get("id") == "overview"), None)
    if not overview:
        return [], []
    rows = [child for child in overview.children if isinstance(child, HtmlNode) and child.tag == "li" and _class_has(child, "nztr-row")]
    headings = []
    data_rows = []
    for row in rows:
        values = _row_values_from_divs(row)
        if _class_has(row, "headings"):
            headings = [value.lower().replace("\xa0", " ") for value in values]
        elif values:
            data_rows.append(row)
    return headings, data_rows


def _detail_column(headings: list[str], *names: str) -> int | None:
    for name in names:
        for index, heading in enumerate(headings):
            if name.lower() in heading:
                return index
    return None


def _merge_detail_row(runner: ParsedRunner, headings: list[str], row: HtmlNode | None) -> ParsedRunner:
    if not row:
        return runner
    values = _row_values_from_divs(row)
    bar = _detail_column(headings, "bar")
    weight = _detail_column(headings, "weight")
    trainer = _detail_column(headings, "trainer")
    sp = _detail_column(headings, "sp")
    if bar is not None and bar < len(values):
        runner.barrier = _int(values[bar])
    if weight is not None and weight < len(values):
        runner.weight = values[weight] or runner.weight
    if trainer is not None and trainer < len(values):
        runner.trainer_name = _clean_trainer_name(values[trainer]) or runner.trainer_name
    if sp is not None and sp < len(values):
        runner.win_odds = _clean_odds(values[sp]) or runner.win_odds
    if _class_has(row, "scratched"):
        runner.status = "scratched"
        runner.is_scratched = True
    return runner


def parse_race_detail_html(html: str, source_url: str, base_race: ParsedRace | None = None) -> ParsedRace:
    parser = TreeParser()
    parser.feed(html)
    root = parser.root
    horse_list = next((node for node in root.find_all("ul") if _class_has(node, "horse-list")), None)
    if not horse_list:
        raise ValueError("Race detail page did not include a horse-list block.")
    horse_rows = [child for child in horse_list.children if isinstance(child, HtmlNode) and child.tag == "li" and _class_has(child, "nztr-row")]
    headings, detail_rows = _detail_overview_rows(root)
    runners = []
    detail_index = 0
    for row in horse_rows:
        runner = _parse_horse_list_row(row, source_url)
        if runner:
            runners.append(_merge_detail_row(runner, headings, detail_rows[detail_index] if detail_index < len(detail_rows) else None))
            detail_index += 1
    if not runners:
        raise ValueError("Race detail page did not include parseable runners.")
    scratched_names = _result_scratched_names(root.lines())
    _apply_result_scratches(runners, scratched_names)
    race = ParsedRace(
        race_number=base_race.race_number if base_race else 0,
        source_detail_url=source_url,
        start_time=base_race.start_time if base_race else None,
        race_name=base_race.race_name if base_race else None,
        conditions=base_race.conditions if base_race else None,
        distance_m=base_race.distance_m if base_race else None,
        stake_text=base_race.stake_text if base_race else None,
        status="detail",
        runners=runners,
        scratched_horse_names=scratched_names,
    )
    return race


def parse_meeting_html(html: str, source_url: str) -> ParsedMeeting:
    parser = TreeParser()
    parser.feed(html)
    root = parser.root
    lines = root.lines()
    full_text = "\n".join(lines)
    if "toggle-detail" not in html and "meeting" not in full_text.lower():
        raise ValueError("Page does not look like a LOVERACING meeting overview page.")

    meeting_date = _parse_date(full_text)
    meeting_name = _meeting_name(lines)
    club, venue = _split_club_venue(meeting_name)
    meeting = ParsedMeeting(
        source_url=source_url,
        stage=_meeting_stage(root),
        source_meeting_id=meeting_id_from_url(source_url),
        date=meeting_date,
        meeting_name=meeting_name,
        club_name=club,
        venue_name=venue,
        last_updated=_parse_last_updated(full_text, meeting_date),
        going=_label_value(lines, "Going"),
        weather=_label_value(lines, "Weather"),
        rail=_label_value(lines, "Rail"),
        track_direction=_label_value(lines, "Track"),
        straight_length=_label_value(lines, "Straight"),
    )
    detail_nodes = _detail_nodes(root)
    if not detail_nodes:
        meeting.warnings.append("No hidden toggle-detail race sections were found.")
    for number, node in detail_nodes:
        race = _parse_race(number, node, source_url, meeting.stage)
        if not race.runners:
            meeting.warnings.append(f"Race {race.race_number} parsed with zero runners.")
        meeting.races.append(race)
    if not meeting.races:
        raise ValueError("No races found in LOVERACING meeting page.")
    if meeting.stage != "nominations" and meeting.races and all(race.status == "results" for race in meeting.races):
        meeting.stage = "results"
    duplicate_numbers = {race.race_number for race in meeting.races if [item.race_number for item in meeting.races].count(race.race_number) > 1}
    for number in sorted(duplicate_numbers):
        meeting.warnings.append(f"Duplicate race number parsed: Race {number}.")
    return meeting
