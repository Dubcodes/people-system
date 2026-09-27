"""Small authenticated client for the Frame Pull bridge API.

Only this backend component knows the bridge base URL and bearer token. Browser
pages use People Intelligence routes, never a Frame Pull URL or credential.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FramePullError(RuntimeError):
    pass


class FramePullOffline(FramePullError):
    pass


class FramePullAuthenticationError(FramePullError):
    pass


@dataclass(frozen=True)
class DownloadedPortrait:
    data: bytes
    content_type: str


class FramePullClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout_seconds: float = 8.0):
        self.base_url = (base_url if base_url is not None else os.getenv("FRAME_PULL_BASE_URL", "")).rstrip("/")
        self.token = token if token is not None else os.getenv("FRAME_PULL_API_TOKEN", "")
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _url(self, path: str) -> str:
        if not self.configured:
            raise FramePullOffline("Frame Pull connection is not configured.")
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, path: str, method: str = "GET") -> tuple[bytes, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self._url(path), method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310: configured private service only
                return response.read(), response.headers.get_content_type()
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise FramePullAuthenticationError("Frame Pull authentication failed.") from exc
            if exc.code == 404:
                raise FramePullError("Frame Pull result was not found.") from exc
            raise FramePullError("Frame Pull returned an error.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise FramePullOffline("Frame Pull is offline or unreachable.") from exc

    def _json(self, path: str, method: str = "GET"):
        body, _ = self._request(path, method)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FramePullError("Frame Pull returned malformed JSON.") from exc

    def health(self) -> dict:
        return self._json("/api/bridge/v1/health")

    def list_race_days(self) -> list[dict]:
        return self._json("/api/bridge/v1/race-days")

    def list_people(self, *, race_day_id: int | None = None, review_status: str | None = None, export_state: str | None = None) -> list[dict]:
        query = "&".join(f"{key}={value}" for key, value in {
            "race_day_id": race_day_id, "review_status": review_status, "export_state": export_state,
        }.items() if value is not None)
        return self._json(f"/api/bridge/v1/people{'?' + query if query else ''}")

    def get_person_group(self, group_id: int) -> dict:
        return self._json(f"/api/bridge/v1/people/{int(group_id)}")

    def get_interview(self, interview_id: int) -> dict:
        return self._json(f"/api/bridge/v1/interviews/{int(interview_id)}")

    def list_interviews(self, *, review_status: str | None = None, export_state: str | None = None, ungrouped_only: bool = False) -> list[dict]:
        query = "&".join(f"{key}={value}" for key, value in {
            "review_status": review_status, "export_state": export_state,
            "ungrouped_only": "true" if ungrouped_only else None,
        }.items() if value is not None)
        return self._json(f"/api/bridge/v1/interviews{'?' + query if query else ''}")

    def download_portrait(self, group_id: int) -> DownloadedPortrait:
        body, content_type = self._request(f"/api/bridge/v1/groups/{int(group_id)}/portrait")
        if not content_type.startswith("image/") or not body:
            raise FramePullError("Frame Pull portrait is not a valid image response.")
        return DownloadedPortrait(body, content_type)

    def acknowledge_export(self, group_id: int) -> dict:
        return self._json(f"/api/bridge/v1/groups/{int(group_id)}/mark-exported", method="POST")

    def download_interview_portrait(self, interview_id: int) -> DownloadedPortrait:
        body, content_type = self._request(f"/api/bridge/v1/interviews/{int(interview_id)}/portrait")
        if not content_type.startswith("image/") or not body:
            raise FramePullError("Frame Pull portrait is not a valid image response.")
        return DownloadedPortrait(body, content_type)

    def acknowledge_interview_export(self, interview_id: int) -> dict:
        return self._json(f"/api/bridge/v1/interviews/{int(interview_id)}/mark-exported", method="POST")

