"""Higgsfield AI HTTP client.

Mirrors the Arcads client surface so generator.py can drive both the same way:
  - submit_clip(payload) → job_id
  - get_clip_status(job_id) → {status, video_url, ...}
  - download_clip(url, dest) → Path

Higgsfield generates short clips (5-10s). One passivepoly script becomes one
hero_clip submission plus N segment submissions; generator.py loops over them.

Auth: HIGGSFIELD_API_KEY env (resolved into account.raw['api_credentials']['higgsfield_key']).

Endpoints follow Higgsfield's documented shape. If the user's account is on
a different version of the API, override `base_url` and the path constants.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests


_DEFAULT_BASE_URL = "https://api.higgsfield.ai/v1"
_PATH_SUBMIT = "/generations/text-to-video"
_PATH_STATUS = "/generations/{id}"

_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class HiggsfieldAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Higgsfield API {status}: {message}")
        self.status = status


class HiggsfieldClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = 30,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise RuntimeError("Higgsfield api_key is empty — check passivepoly.yaml api_credentials")
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
                raise HiggsfieldAPIError(0, f"network error: {e}") from e

            if resp.status_code < 400:
                return resp

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
            raise HiggsfieldAPIError(resp.status_code, str(detail))

        raise HiggsfieldAPIError(0, f"exhausted retries: {last_exc}")

    def submit_clip(self, clip_spec: dict[str, Any]) -> str:
        """`clip_spec` items expected: prompt, duration_seconds, aspect_ratio,
        camera, negative_prompt. The shape matches what engineer.py emits per
        segment. Extra fields are passed through.
        """
        body = {
            "prompt": clip_spec["prompt"],
            "duration": int(clip_spec.get("duration_seconds", 6)),
            "aspect_ratio": clip_spec.get("aspect_ratio", "9:16"),
            "negative_prompt": clip_spec.get("negative_prompt", ""),
            "camera_motion": clip_spec.get("camera"),
        }
        # Pass through any provider-specific extras the engineer added.
        for k, v in clip_spec.items():
            if k not in body and k not in {"prompt", "duration_seconds", "camera"}:
                body.setdefault(k, v)

        resp = self._request("POST", _PATH_SUBMIT, json_body=body)
        data = resp.json()
        job_id = data.get("id") or data.get("generation_id") or data.get("job_id")
        if not job_id:
            raise HiggsfieldAPIError(resp.status_code, f"submit response missing id: {data}")
        return str(job_id)

    def get_clip_status(self, job_id: str) -> dict[str, Any]:
        resp = self._request("GET", _PATH_STATUS.format(id=job_id))
        return resp.json()

    def download_clip(self, video_url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(video_url, stream=True, timeout=self.timeout * 4) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest
