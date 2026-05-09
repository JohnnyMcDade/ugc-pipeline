"""Arcads.ai HTTP client.

Three responsibilities:
  - submit_video(payload)        → returns job_id (str)
  - get_video_status(job_id)     → returns {status, video_url, ...}
  - download_video(url, dest)    → streams the mp4 to disk

Auth: per-account API key resolved from `account.raw['api_credentials']['arcads_key']`
(the config loader strips the `_env` suffix and substitutes the env value).

Endpoints below use the path shape Arcads documents publicly. If the user's
account is on a different API version, override `base_url` and the path
constants — every endpoint goes through one of two methods, so the surface
to update is tiny.

Retries: idempotent GET (status, download) is retried with backoff on
transient errors; POST (submit_video) is retried only on 5xx, never on 4xx.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests


_DEFAULT_BASE_URL = "https://api.arcads.ai/v1"
_PATH_SUBMIT = "/videos"
_PATH_STATUS = "/videos/{id}"

_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class ArcadsAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Arcads API {status}: {message}")
        self.status = status


class ArcadsClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = 30,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise RuntimeError("Arcads api_key is empty — check the account YAML's api_credentials block")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        retry_post_on_5xx_only: bool = True,
        stream: bool = False,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method, url, json=json_body, timeout=self.timeout, stream=stream,
                )
            except requests.RequestException as e:
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise ArcadsAPIError(0, f"network error: {e}") from e

            if resp.status_code < 400:
                return resp

            transient = resp.status_code in _TRANSIENT_STATUS
            if method.upper() == "POST" and retry_post_on_5xx_only:
                transient = transient and resp.status_code >= 500

            if transient and attempt < self.max_retries:
                time.sleep(2 ** attempt)
                continue

            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text[:300]
            raise ArcadsAPIError(resp.status_code, str(detail))

        raise ArcadsAPIError(0, f"exhausted retries: {last_exc}")

    def submit_video(self, payload: dict[str, Any]) -> str:
        """POST /videos → returns the job id."""
        resp = self._request("POST", _PATH_SUBMIT, json_body=payload)
        body = resp.json()
        job_id = body.get("id") or body.get("video_id") or body.get("job_id")
        if not job_id:
            raise ArcadsAPIError(resp.status_code, f"submit response missing id: {body}")
        return str(job_id)

    def get_video_status(self, job_id: str) -> dict[str, Any]:
        """GET /videos/{id} → status + video_url when ready."""
        resp = self._request("GET", _PATH_STATUS.format(id=job_id))
        return resp.json()

    def download_video(self, video_url: str, dest: Path) -> Path:
        """Stream-download `video_url` to `dest`. Uses the auth header in case
        Arcads serves video URLs that require it; harmless when they don't.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(video_url, stream=True, timeout=self.timeout * 4) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest
