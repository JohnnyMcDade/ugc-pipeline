"""TikTok Commercial Music Library catalog client (READ-ONLY).

Returns the catalog of `music_id` values approved for commercial use. NOT
MP3 downloads — actual music attribution happens at TikTok upload time via
the Content Posting API's `music_id` field. This client just enumerates
what's available + their metadata.

Endpoint shape this file is written against:

  POST  /v2/business/commercial_music/list
  body  { "moods": [...], "genres": [...], "limit": int, "sort": "trending" }

  →     { "data": { "music": [
            { "music_id": "...", "title": "...", "artist": "...",
              "trending_score": 0.92, "moods": [...], "genres": [...],
              "duration_seconds": 28, "commercial_use_approved": true,
              "preview_url": "..." },
            ...
          ] } }

WIRE THE REAL ENDPOINT: TikTok exposes this under a couple of different
paths depending on access tier. Once you've confirmed your dev app has
"Commercial Music Library" permission, replace the body of `_fetch_catalog`
with the real call. The auth header pattern (Bearer or X-Tt-Env headers)
also depends on tier — confirm in your dev console.
"""

from __future__ import annotations

import os
from typing import Any

import requests


_DEFAULT_BASE_URL = "https://business-api.tiktok.com"
_PATH_CATALOG = "/open_api/v1.3/commercial_music/list"  # placeholder shape


class TikTokMusicCatalogError(RuntimeError):
    pass


class TikTokMusicCatalogClient:
    def __init__(
        self,
        *,
        access_token: str | None = None,
        client_key: str | None = None,
        client_secret: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = 30,
    ) -> None:
        # Two auth modes — confirm yours in the TikTok dev console:
        #   1. Bearer token from your app's commercial-music grant
        #   2. App-level client_key/secret (some tiers use this for catalog reads)
        self.access_token = access_token or os.environ.get("TIKTOK_SESSION_SHARPGUYLAB")
        self.client_key = client_key or os.environ.get("TIKTOK_CLIENT_KEY")
        self.client_secret = client_secret or os.environ.get("TIKTOK_CLIENT_SECRET")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # Both headers are sent; whichever your tier accepts will be used.
        if self.access_token:
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        if self.client_key:
            self.session.headers["X-Tt-Client-Key"] = self.client_key

    def list_commercial_music(
        self,
        *,
        moods: list[str] | None = None,
        genres: list[str] | None = None,
        limit: int = 50,
        sort: str = "trending",
    ) -> list[dict[str, Any]]:
        """Returns a list of track dicts. Each dict shape:

            {
              "music_id":         str,
              "title":            str,
              "artist":           str,
              "trending_score":   float,    # 0.0-1.0
              "moods":            list[str],
              "genres":           list[str],
              "duration_seconds": int,
              "commercial_use_approved": bool,
              "preview_url":      str | None,
            }
        """
        tracks = self._fetch_catalog(moods=moods, genres=genres, limit=limit, sort=sort)
        # Defensive filter: even if the endpoint accepts a commercial_use=true
        # parameter, double-check on our side. The pipeline's attribution
        # downstream is meaningless if a track isn't actually approved.
        return [t for t in tracks if t.get("commercial_use_approved", False)]

    def _fetch_catalog(
        self,
        *,
        moods: list[str] | None,
        genres: list[str] | None,
        limit: int,
        sort: str,
    ) -> list[dict[str, Any]]:
        """The real network call. STUB — wire to your actual endpoint.

        When wiring:
          - Replace `_PATH_CATALOG` with the real path from TikTok's docs.
          - Replace the body shape if needed.
          - Map TikTok's response field names to the shape documented above.
          - Surface errors as `TikTokMusicCatalogError`.
        """
        if not (self.access_token or (self.client_key and self.client_secret)):
            raise TikTokMusicCatalogError(
                "No auth credentials. Set TIKTOK_CLIENT_KEY+SECRET or pass access_token."
            )
        raise NotImplementedError(
            "TikTok Commercial Music Library catalog fetch not yet wired. "
            "Confirm your dev app has commercial music permission, then "
            "replace integrations/tiktok_music_catalog.py:_fetch_catalog with "
            f"the real POST {_PATH_CATALOG} (or whichever path your tier exposes)."
        )
