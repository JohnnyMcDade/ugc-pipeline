#!/usr/bin/env python3
"""TikTok Content Posting API — OAuth helper (auto-capture variant).

Opens TikTok's authorize page in the browser, captures the redirect on a
local server at http://localhost:8080/callback, exchanges the code for
tokens, and writes them to .env. Fully automated — no manual code pasting.

Usage:
    python tiktok_oauth.py sharpguylab
    python tiktok_oauth.py rideupgrades
    python tiktok_oauth.py passivepoly

PREREQUISITES on the TikTok dev console for your app:
  1. `http://localhost:8080/callback` is in the app's allowed redirect URIs.
     (Sandbox/dev app tiers accept localhost; production tiers may not —
     if you see "redirect_uri does not match" at the authorize step, the
     URI isn't whitelisted on your specific app tier.)
  2. Content Posting API + Login Kit products enabled.
  3. Scopes `user.info.basic`, `video.upload`, `video.publish` approved.

What this writes to .env (per account):
  TIKTOK_SESSION_<HANDLE>       — access_token  (~24h)
  TIKTOK_BUSINESS_ID_<HANDLE>   — open_id       (stable per app+user)
  TIKTOK_REFRESH_TOKEN_<HANDLE> — refresh_token (~365d; powers refresh)
"""

from __future__ import annotations

import http.server
import os
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# override=True so a shell-exported empty TIKTOK_CLIENT_KEY="" doesn't
# silently shadow the real .env value.
load_dotenv(override=True)

CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
REDIRECT_URI = "http://localhost:8080/callback"
CALLBACK_PORT = 8080
SCOPES = ["user.info.basic", "video.upload", "video.publish"]
ENV_PATH = Path(__file__).parent / ".env"

# How long to wait for the browser redirect after opening the authorize page.
CALLBACK_TIMEOUT_SECONDS = 240

# Endpoints
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

ACCOUNT_ENV_MAP: dict[str, dict[str, str]] = {
    "sharpguylab": {
        "session": "TIKTOK_SESSION_SHARPGUYLAB",
        "business_id": "TIKTOK_BUSINESS_ID_SHARPGUYLAB",
        "refresh": "TIKTOK_REFRESH_TOKEN_SHARPGUYLAB",
    },
    "rideupgrades": {
        "session": "TIKTOK_SESSION_RIDEUPGRADES",
        "business_id": "TIKTOK_BUSINESS_ID_RIDEUPGRADES",
        "refresh": "TIKTOK_REFRESH_TOKEN_RIDEUPGRADES",
    },
    "passivepoly": {
        "session": "TIKTOK_SESSION_PASSIVEPOLY",
        "business_id": "TIKTOK_BUSINESS_ID_PASSIVEPOLY",
        "refresh": "TIKTOK_REFRESH_TOKEN_PASSIVEPOLY",
    },
}

# Captured by the callback handler in the server thread; consumed by main.
_captured: dict[str, Any] = {
    "code": None, "state": None, "error": None, "error_description": None,
}
_done = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _captured["code"] = (params.get("code") or [None])[0]
        _captured["state"] = (params.get("state") or [None])[0]
        _captured["error"] = (params.get("error") or [None])[0]
        _captured["error_description"] = (params.get("error_description") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if _captured["error"]:
            html = (
                "<!doctype html><html><body style='font-family:sans-serif;"
                "background:#0d1117;color:#e6edf3;padding:40px;text-align:center'>"
                f"<h1 style='color:#f85149'>✗ OAuth error: {_captured['error']}</h1>"
                f"<p>{_captured['error_description'] or ''}</p>"
                "<p style='color:#8b949e'>Return to the terminal — script will exit shortly.</p>"
                "</body></html>"
            )
        else:
            html = (
                "<!doctype html><html><body style='font-family:sans-serif;"
                "background:#0d1117;color:#e6edf3;padding:40px;text-align:center'>"
                "<h1 style='color:#3fb950'>✓ Authorization received</h1>"
                "<p>Token exchange running in your terminal. You can close this tab.</p>"
                "</body></html>"
            )
        self.wfile.write(html.encode("utf-8"))
        _done.set()

    def log_message(self, fmt, *args) -> None:  # silence default request log
        pass


def _start_callback_server() -> socketserver.TCPServer:
    """Bring up the loopback HTTP server BEFORE opening the browser so we
    don't race against a fast redirect.
    """
    socketserver.TCPServer.allow_reuse_address = True
    try:
        server = socketserver.TCPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    except OSError as e:
        raise SystemExit(
            f"✗ Could not bind localhost:{CALLBACK_PORT}: {e}\n"
            f"  Something else is using port {CALLBACK_PORT}. Kill it with:\n"
            f"    lsof -ti:{CALLBACK_PORT} | xargs kill"
        ) from e
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _build_authorize_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_key": CLIENT_KEY,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{qs}"


def _exchange_code_for_token(code: str) -> dict[str, Any]:
    r = requests.post(
        TOKEN_URL,
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if r.status_code >= 400:
        print(f"\n✗ Token exchange failed: HTTP {r.status_code}")
        print(r.text[:500])
        sys.exit(1)
    body = r.json()
    # TikTok sometimes wraps the payload under "data", sometimes not — handle both.
    inner = body.get("data") if isinstance(body.get("data"), dict) else body
    if "access_token" not in (inner or {}):
        print(f"\n✗ Token response missing access_token: {body}")
        sys.exit(1)
    return inner


def _write_env(account_handle: str, token_data: dict[str, Any]) -> tuple[str, str, str]:
    """Replace (or append) the three per-account env var lines in .env.
    Atomic via temp file + rename so an interrupted run can't corrupt secrets.
    """
    keys = ACCOUNT_ENV_MAP[account_handle]
    session_key = keys["session"]
    business_id_key = keys["business_id"]
    refresh_key = keys["refresh"]

    access_token = token_data["access_token"]
    open_id = token_data.get("open_id", "")
    refresh_token = token_data.get("refresh_token", "")

    if not ENV_PATH.exists():
        print(f"\n✗ .env not found at {ENV_PATH}")
        sys.exit(1)

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    replacements = {
        session_key: f"{session_key}={access_token}\n",
        business_id_key: f"{business_id_key}={open_id}\n",
        refresh_key: f"{refresh_key}={refresh_token}\n",
    }
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        replaced = False
        for k, repl in replacements.items():
            if stripped.startswith(f"{k}="):
                new_lines.append(repl)
                seen.add(k)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)
    for k, repl in replacements.items():
        if k not in seen:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(repl)

    tmp_path = ENV_PATH.with_suffix(".env.tmp")
    tmp_path.write_text("".join(new_lines), encoding="utf-8")
    os.replace(tmp_path, ENV_PATH)
    return session_key, business_id_key, refresh_key


def run_oauth(account_handle: str) -> None:
    if not CLIENT_KEY or not CLIENT_SECRET:
        print("✗ TIKTOK_CLIENT_KEY or TIKTOK_CLIENT_SECRET is empty in .env")
        sys.exit(1)
    if account_handle not in ACCOUNT_ENV_MAP:
        print(f"✗ Unknown account: {account_handle!r}")
        print(f"  Valid: {', '.join(ACCOUNT_ENV_MAP)}")
        sys.exit(1)

    state = secrets.token_urlsafe(32)
    auth_url = _build_authorize_url(state)

    print()
    print(f"  TikTok OAuth — authorizing @{account_handle}")
    print(f"  redirect_uri: {REDIRECT_URI}")
    print(f"  scopes:       {', '.join(SCOPES)}")
    print()

    # Bring up the callback server BEFORE opening the browser so a fast
    # redirect doesn't beat the listener up.
    server = _start_callback_server()

    print(f"  Opening browser to TikTok's authorize page...")
    print()
    print(f"  If the browser doesn't open, paste this manually:")
    print(f"    {auth_url}")
    print()
    webbrowser.open(auth_url)

    print(f"  Waiting for callback at {REDIRECT_URI} ...")
    print(f"  (timeout: {CALLBACK_TIMEOUT_SECONDS // 60} minutes — sign in + click Authorize)")
    completed = _done.wait(timeout=CALLBACK_TIMEOUT_SECONDS)
    server.shutdown()

    if not completed:
        print(f"\n✗ OAuth timed out — no callback received within "
              f"{CALLBACK_TIMEOUT_SECONDS // 60} minutes.")
        sys.exit(1)

    if _captured["error"]:
        print(f"\n✗ OAuth error from TikTok: {_captured['error']}")
        if _captured["error_description"]:
            print(f"  description: {_captured['error_description']}")
        sys.exit(1)

    if _captured["state"] != state:
        print("\n✗ State mismatch — possible CSRF, aborting")
        print(f"  expected: {state[:16]}...")
        print(f"  received: {(_captured['state'] or '')[:16]}...")
        sys.exit(1)

    code = _captured["code"]
    if not code:
        print("\n✗ No authorization code received from TikTok")
        sys.exit(1)

    print()
    print("  ✓ Authorization code received, exchanging for access token...")
    token_data = _exchange_code_for_token(code)

    session_key, business_id_key, refresh_key = _write_env(account_handle, token_data)

    expires_in = int(token_data.get("expires_in") or 0)
    refresh_expires_in = int(token_data.get("refresh_expires_in") or 0)

    print()
    print(f"  ✓ @{account_handle} authorized. Tokens written to .env:")
    print(f"    {session_key}")
    print(f"    {business_id_key}      = {token_data.get('open_id', '')}")
    print(f"    {refresh_key}")
    print()
    print(f"  ⏰ access_token expires in:  {expires_in // 3600}h {(expires_in % 3600) // 60}m")
    print(f"  ⏰ refresh_token expires in: {refresh_expires_in // 86400}d "
          f"(refresh helper uses it to mint new access_tokens without re-auth)")
    print()


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python tiktok_oauth.py <account_handle>")
        print(f"  Valid handles: {', '.join(ACCOUNT_ENV_MAP)}")
        sys.exit(2)
    run_oauth(sys.argv[1].lower())


if __name__ == "__main__":
    main()
