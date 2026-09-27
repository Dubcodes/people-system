import re
from dataclasses import dataclass
from urllib.parse import urljoin

from loveracing_importer.fetcher import USER_AGENT
from urllib.request import Request, urlopen


CALENDAR_URL = "https://loveracing.nz/RaceInfo.aspx#bm-meeting-calendar"


@dataclass
class DiscoveredMeeting:
    source_url: str
    label: str
    racecourse: str | None = None


def fetch_calendar_html(timeout: int = 20) -> str:
    request = Request(
        "https://loveracing.nz/RaceInfo.aspx",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def discover_calendar_meetings(html: str) -> list[DiscoveredMeeting]:
    meetings = []
    seen = set()
    anchor_pattern = re.compile(r'<a\b[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<label>.*?)</a>', re.I | re.S)
    text_html = re.sub(r"\s+", " ", html)
    for match in anchor_pattern.finditer(text_html):
        href = match.group("href")
        label = re.sub(r"<[^>]+>", "", match.group("label"))
        label = " ".join(label.split())
        if not label:
            continue
        if not re.search(r"/RaceInfo/\d+/Meeting-Overview\.aspx$", href):
            continue
        start = max(0, match.start() - 250)
        end = min(len(text_html), match.end() + 250)
        raw_context = text_html[start:end]
        if "meeting-news" in raw_context or not re.search(r'class="[^"]*\bmeeting\b', raw_context, re.I):
            continue
        context = re.sub(r"<[^>]+>", " ", raw_context)
        context = " ".join(context.split())
        id_match = re.search(r"/raceinfo/(\d+)/", href, re.I)
        if "meeting-overview" not in href.lower() and id_match:
            source_url = f"https://loveracing.nz/raceinfo/{id_match.group(1)}/meeting-overview.aspx"
        else:
            source_url = urljoin("https://loveracing.nz/", href)
        key = source_url.lower()
        if key in seen:
            continue
        seen.add(key)
        meetings.append(DiscoveredMeeting(source_url=source_url, label=label, racecourse=context))
    return meetings


def calendar_discovery_is_complete(html: str, meetings: list[DiscoveredMeeting]) -> bool:
    normalized = re.sub(r"\s+", " ", html or "").lower()
    return bool(meetings) and "bm-meeting-calendar" in normalized
