"""TikTok Content Posting API client (write-side TikTok integration).

This is distinct from `tiktok_creative_center.py` (trend reads) and
`tiktok_scraper.py` (analytics reads). Auth context is per-account: the
client takes an account-scoped access token from `TIKTOK_SESSION_<HANDLE>`.

API surface implemented:
  - init_upload(video_size, post_info)
  - upload_bytes(upload_url, file_path)             # single-chunk PUT
  - fetch_status(publish_id)
  - post_video(file_path, caption, ...)             # high-level convenience

Comment endpoints (post_comment / pin_comment) are stubbed because the
Content Posting API tier most users have does NOT expose them. They surface
as `available=False` so the publisher logs the limitation cleanly instead
of crashing — drop in a real implementation when access is granted.

OAuth refresh is NOT handled here. If the access token is expired, the API
returns 401 and the publisher logs it; refresh is the user's responsibility
(or a future helper).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests


_DEFAULT_BASE_URL = "https://open.tiktokapis.com"
_PATH_INIT = "/v2/post/publish/video/init/"
_PATH_STATUS = "/v2/post/publish/status/fetch/"

_TERMINAL_SUCCESS = {"PUBLISH_COMPLETE", "PUBLISHED"}
_TERMINAL_FAILURE = {"FAILED", "PUBLISH_FAILED", "FAILURE"}


class TikTokAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"TikTok API {status}: {message}")
        self.status = status


class TikTokCommentsUnavailable(RuntimeError):
    pass


class TikTokPublishClient:
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = 60,
        poll_interval_seconds: int = 10,
        poll_timeout_seconds: int = 600,
    ) -> None:
        if not access_token:
            raise RuntimeError("TikTok access token is empty (TIKTOK_SESSION_*)")
        self.token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        })

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        r = self.session.post(url, json=body, timeout=self.timeout)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:300]
            raise TikTokAPIError(r.status_code, str(detail))
        return r.json()

    def init_upload(self, *, video_size: int, post_info: dict[str, Any]) -> dict[str, Any]:
        """Returns {publish_id, upload_url}. `post_info` is the TikTok
        post_info dict (title, privacy_level, etc.).
        """
        body = {
            "post_info": post_info,
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": video_size,        # single-chunk upload
                "total_chunk_count": 1,
            },
        }
        resp = self._post_json(_PATH_INIT, body)
        data = resp.get("data") or {}
        if "publish_id" not in data or "upload_url" not in data:
            raise TikTokAPIError(200, f"init response missing fields: {resp}")
        return {"publish_id": data["publish_id"], "upload_url": data["upload_url"]}

    def upload_bytes(self, upload_url: str, file_path: Path) -> None:
        """Single-chunk PUT to the signed upload URL. TikTok wants
        Content-Range and Content-Type set explicitly.
        """
        size = file_path.stat().st_size
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        }
        with file_path.open("rb") as f:
            r = requests.put(upload_url, data=f, headers=headers, timeout=self.timeout * 4)
        if r.status_code >= 400:
            raise TikTokAPIError(r.status_code, f"upload PUT failed: {r.text[:300]}")

    def fetch_status(self, publish_id: str) -> dict[str, Any]:
        resp = self._post_json(_PATH_STATUS, {"publish_id": publish_id})
        return resp.get("data") or {}

    def post_video(
        self,
        *,
        file_path: Path,
        caption: str,
        privacy_level: str = "PUBLIC_TO_EVERYONE",
        disable_duet: bool = False,
        disable_stitch: bool = False,
        disable_comment: bool = False,
        cover_timestamp_ms: int = 1000,
        music_id: str | None = None,
    ) -> dict[str, Any]:
        """High-level: init → upload → poll until PUBLISH_COMPLETE. Returns
        the final status dict (which includes the post id when complete).

        `music_id`: optional commercial-music ID from TikTok's library. When
        set, the post is attributed to that sound — TikTok's algorithm gives
        videos using trending sounds an algorithmic boost. The ID comes from
        Agent 9 (music scout) and is selected at publish time by the
        publisher.
        """
        size = file_path.stat().st_size
        post_info = {
            "title": caption[:2200],     # TikTok cap ~2200 chars
            "privacy_level": privacy_level,
            "disable_duet": disable_duet,
            "disable_comment": disable_comment,
            "disable_stitch": disable_stitch,
            "video_cover_timestamp_ms": cover_timestamp_ms,
        }
        if music_id:
            post_info["music_id"] = music_id
        init = self.init_upload(video_size=size, post_info=post_info)
        self.upload_bytes(init["upload_url"], file_path)

        # Poll.
        start = time.monotonic()
        last_status = ""
        while time.monotonic() - start < self.poll_timeout_seconds:
            status_doc = self.fetch_status(init["publish_id"])
            status = (status_doc.get("status") or "").upper()
            if status != last_status:
                last_status = status
            if status in _TERMINAL_SUCCESS:
                return {
                    "publish_id": init["publish_id"],
                    "status": status,
                    "post_ids": status_doc.get("publicaly_available_post_id")
                                 or status_doc.get("publicly_available_post_id") or [],
                    "raw": status_doc,
                }
            if status in _TERMINAL_FAILURE:
                raise TikTokAPIError(
                    200, f"publish terminal failure: {status_doc.get('fail_reason') or status_doc}"
                )
            time.sleep(self.poll_interval_seconds)

        raise TikTokAPIError(0, f"publish poll timed out (last status: {last_status})")

    # --- comment endpoints (likely unavailable on Content Posting tier) ---
    @property
    def comments_available(self) -> bool:
        return False

    def post_comment(self, post_id: str, text: str) -> str:
        raise TikTokCommentsUnavailable(
            "TikTok Content Posting API does not expose comment creation. "
            "Either upgrade to an API tier that includes comments, post the "
            "comment manually, or replace this method with a Creator Marketplace "
            "implementation."
        )

    def pin_comment(self, post_id: str, comment_id: str) -> bool:
        raise TikTokCommentsUnavailable(
            "TikTok Content Posting API does not expose comment pinning."
        )
