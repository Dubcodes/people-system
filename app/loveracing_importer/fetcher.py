import hashlib
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from loveracing_importer.models import FetchResult


SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "loveracing_snapshots"
USER_AGENT = "RaceDayViewer/1.0 (local race-day operations)"


def meeting_id_from_url(url: str) -> str | None:
    parts = [part for part in urlparse(url).path.split("/") if part]
    lowered = [part.lower() for part in parts]
    if "raceinfo" in lowered:
        index = lowered.index("raceinfo")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _snapshot_path(url: str, content_hash: str, fetched_at: datetime) -> Path:
    meeting_id = meeting_id_from_url(url) or "meeting"
    safe_hash = content_hash[:16]
    filename = f"{fetched_at.strftime('%Y%m%d-%H%M%S')}-{meeting_id}-{safe_hash}.html"
    return SNAPSHOT_DIR / filename


def fetch_meeting_html(url: str, timeout: int = 20) -> FetchResult:
    fetched_at = datetime.utcnow()
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            html = raw.decode(charset, errors="replace")
            content_hash = hashlib.sha256(raw).hexdigest()
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = _snapshot_path(url, content_hash, fetched_at)
            path.write_text(html, encoding="utf-8")
            return FetchResult(
                success=True,
                source_url=url,
                status=getattr(response, "status", 200),
                html=html,
                content_hash=content_hash,
                raw_html_path=str(path),
            )
    except HTTPError as error:
        return FetchResult(success=False, source_url=url, status=error.code, error_message=str(error))
    except (TimeoutError, URLError, OSError) as error:
        return FetchResult(success=False, source_url=url, error_message=str(error))
