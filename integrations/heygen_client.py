"""HeyGen API client.

Replaces the prior Arcads + Higgsfield clients — all three accounts now use
HeyGen for talking-head avatar UGC. Reference: developers.heygen.com.

Endpoints used:
  POST /v2/video/generate          → submit a generation request, returns video_id
  GET  /v1/video_status.get        → poll for status + final video_url

Auth: `X-Api-Key: <key>` header. One key per HeyGen organization (not per
TikTok account) — read from `HEYGEN_API_KEY` env.

Concurrency: HeyGen's typical paid tier allows ~5 concurrent generations.
The pipeline submits 3 accounts × up to 4 variants = 12 jobs at 8:00 AM,
but APScheduler runs accounts in parallel and each account submits serially
through its variants — so steady-state concurrent submissions ≤ 3, well
under the limit.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests


_DEFAULT_BASE_URL = "https://api.heygen.com"
_PATH_GENERATE = "/v2/video/generate"
_PATH_STATUS = "/v1/video_status.get"

_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class HeyGenAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HeyGen API {status}: {message}")
        self.status = status


class HeyGenClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = 30,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise RuntimeError("HEYGEN_API_KEY is empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key": api_key,
            "Accept": "application/json",
        })

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise HeyGenAPIError(0, f"network error: {e}") from e

            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError as e:
                    raise HeyGenAPIError(resp.status_code, f"non-JSON response: {e}") from e

            transient = resp.status_code in _TRANSIENT_STATUS
            if method.upper() == "POST":
                transient = transient and resp.status_code >= 500
            if transient and attempt < self.max_retries:
                time.sleep(2 ** attempt)
                continue

            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text[:300]
            raise HeyGenAPIError(resp.status_code, str(detail))

        raise HeyGenAPIError(0, f"exhausted retries: {last_exc}")

    def submit_video(self, payload: dict[str, Any]) -> str:
        """POST /v2/video/generate — payload is the full HeyGen v2 body
        (video_inputs, dimension, test, ...). Returns the video_id.
        """
        body = self._request("POST", _PATH_GENERATE, json_body=payload)
        # HeyGen wraps responses: {"code": 100, "data": {...}, "message": "..."}.
        # Non-success code is application-level failure even with HTTP 200.
        if int(body.get("code", 0)) not in (100, 0):
            raise HeyGenAPIError(200, f"submit failed: {body}")
        data = body.get("data") or {}
        video_id = data.get("video_id") or data.get("id")
        if not video_id:
            raise HeyGenAPIError(200, f"submit response missing video_id: {body}")
        return str(video_id)

    def get_video_status(self, video_id: str) -> dict[str, Any]:
        """GET /v1/video_status.get?video_id=<id> — flattens HeyGen's
        response so the poller sees a uniform `status` + `video_url` shape.
        """
        body = self._request("GET", _PATH_STATUS, params={"video_id": video_id})
        if int(body.get("code", 0)) not in (100, 0):
            raise HeyGenAPIError(200, f"status fetch failed: {body}")
        data = body.get("data") or {}
        status = (data.get("status") or "").lower()
        return {
            "status": status,
            "video_url": data.get("video_url"),
            "video_url_caption": data.get("video_url_caption"),
            "thumbnail_url": data.get("thumbnail_url"),
            "duration": data.get("duration"),
            "error": data.get("error"),
            "raw": data,
        }

    def download_video(self, video_url: str, dest: Path) -> Path:
        """Stream-download `video_url` to `dest`. HeyGen video URLs are
        pre-signed S3 / CloudFront URLs — no auth header needed, but sending
        ours is harmless.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(video_url, stream=True, timeout=self.timeout * 4) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest
