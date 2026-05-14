#!/usr/bin/env python3
"""TikTok Content Posting API — OAuth helper (manual-paste variant).

TikTok rejects http://localhost redirect URIs in production, so the OAuth
code is sent to your passivepoly.com domain instead and you paste it back
into the terminal manually.

Flow:
  1. Script prints + opens the authorize URL.
  2. You sign in as the target account and click Authorize.
  3. TikTok redirects to https://passivepoly.com/callback?code=...&state=...
  4. callback.html (in this repo) renders the code in a click-to-copy box.
  5. You paste the code into this terminal.
  6. Script exchanges the code for tokens and writes them to .env.

Usage:
    python tiktok_oauth.py sharpguylab
    python tiktok_oauth.py rideupgrades
    python tiktok_oauth.py passivepoly

PREREQUISITES on the TikTok dev console for your app:
  1. `https://passivepoly.com/callback` is in the app's allowed redirect URIs.
  2. The Content Posting API + Login Kit products are enabled.
  3. Scopes `user.info.basic`, `video.upload`, `video.publish` are approved.

PREREQUISITES on passivepoly.com:
  - Deploy `callback.html` (next to this file) at `/callback`. It's a
    pure static page — no server-side logic needed — that parses
    `?code=...` out of the query string and displays it for copying.

What this writes to .env (per account):
  TIKTOK_SESSION_<HANDLE>       — access_token  (~24h)
  TIKTOK_BUSINESS_ID_<HANDLE>   — open_id       (stable per app+user)
  TIKTOK_REFRESH_TOKEN_<HANDLE> — refresh_token (~365d; powers refresh)

Security note on the manual flow: the OAuth code is briefly visible in
the URL bar of your browser on passivepoly.com. The code is single-use
and expires within ~10 minutes, but anything logging request URLs on
the passivepoly.com side (nginx access logs, CDN analytics) will see it.
Standard caveat for any browser-redirect OAuth flow — just be aware.
"""

from __future__ import annotations

import os
import secrets
import sys
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
REDIRECT_URI = "https://passivepoly.com/callback"
SCOPES = ["user.info.basic", "video.upload", "video.publish"]
ENV_PATH = Path(__file__).parent / ".env"

# Endpoints
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# Maps account handle → env var names this script writes.
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


def _build_authorize_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_key": CLIENT_KEY,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{qs}"


def _extract_code(pasted: str) -> str:
    """Tolerate either the bare code OR the full callback URL pasted back.

    If the user copied the whole address bar contents, parse the `code`
    query parameter out of it. Otherwise treat the input as the code itself.
    """
    pasted = pasted.strip()
    if not pasted:
        return ""
    if pasted.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(pasted)
        params = urllib.parse.parse_qs(parsed.query)
        return (params.get("code") or [""])[0].strip()
    return pasted


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
    print("  Step 1/3: opening the TikTok authorize page in your browser.")
    print()
    print("  If it doesn't open, paste this URL manually:")
    print(f"    {auth_url}")
    print()
    webbrowser.open(auth_url)

    print("  Step 2/3: sign in as the target account, click Authorize.")
    print(f"  TikTok will redirect to {REDIRECT_URI}?code=...&state=...")
    print("  callback.html on that page renders the code in a copy box.")
    print()
    print("  Step 3/3: paste the code below.")
    print("  (You can paste either the bare code OR the full callback URL —")
    print("   if you paste the URL the script will extract the code itself.)")
    print()

    try:
        pasted = input("  code → ")
    except (EOFError, KeyboardInterrupt):
        print("\n✗ aborted")
        sys.exit(1)

    code = _extract_code(pasted)
    if not code:
        print("✗ empty code")
        sys.exit(1)

    print()
    print(f"  exchanging code (state ref: {state[:12]}...) for access token...")
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
