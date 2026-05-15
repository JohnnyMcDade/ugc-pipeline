#!/usr/bin/env python3
"""Manual TikTok token exchange — no OAuth helper, no PKCE, no local server.

Use this when tiktok_oauth.py keeps failing on app-config edge cases.
This script does ONE thing: take an authorization code as a CLI arg, POST
to TikTok's token endpoint with just client_key + client_secret, and
write the result to .env.

Step-by-step:

  1. Visit the authorize URL printed by `python get_token.py` (no args).
     Sign in as the target TikTok account, click Authorize.

  2. TikTok redirects to http://localhost:8080/callback?code=<LONG_CODE>&state=test123.
     The browser will show "site can't be reached" because there's no
     local server running this time — that's expected. The code is in
     the URL bar; copy it.

  3. Run:
        python get_token.py <account_handle> <paste_code_here>

     You can also paste the FULL callback URL — the script extracts
     `code` for you. So this works too:
        python get_token.py sharpguylab 'http://localhost:8080/callback?code=xyz&state=test123'

  4. The script POSTs to /v2/oauth/token/ with these form fields:
        client_key
        client_secret
        code
        grant_type=authorization_code
        redirect_uri=http://localhost:8080/callback
     No code_verifier. No code_challenge. No PKCE.

  5. Writes TIKTOK_SESSION_<HANDLE>, TIKTOK_BUSINESS_ID_<HANDLE>, and
     TIKTOK_REFRESH_TOKEN_<HANDLE> to .env. Atomic + post-write verified.

If TikTok still rejects this, the issue isn't PKCE or the verifier — it's
the TikTok dev console app configuration. Check:
  - Is the app set to "Confidential Client" / "Web App"?
  - Is there a "PKCE Required" toggle? (Try toggling it off explicitly.)
  - Is http://localhost:8080/callback in the allowed redirect URIs?
  - Are user.info.basic + video.upload + video.publish all approved?
"""

from __future__ import annotations

import json as _json
import os
import sys
import urllib.parse
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
SCOPES = "user.info.basic,video.upload,video.publish"
STATE = "test123"  # fixed because manual flow has no CSRF surface

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
ENV_PATH = Path(__file__).parent / ".env"

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


# ── Helpers ────────────────────────────────────────────────────────────────

def _redact(value: Any) -> str:
    if value is None:
        return "<None>"
    s = str(value)
    if not s:
        return "<EMPTY>"
    if len(s) <= 12:
        return f"<{len(s)} chars: {s!r}>"
    return f"<{len(s)} chars: {s[:8]!r}...{s[-4:]!r}>"


def _redact_body(node: Any) -> Any:
    SENSITIVE = {"access_token", "refresh_token"}
    if isinstance(node, dict):
        return {k: (_redact(v) if k in SENSITIVE and isinstance(v, str) else _redact_body(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_redact_body(x) for x in node]
    return node


def _build_authorize_url() -> str:
    qs = urllib.parse.urlencode({
        "client_key": CLIENT_KEY,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": STATE,
    })
    return f"{AUTHORIZE_URL}?{qs}"


def _extract_code(pasted: str) -> str:
    """Accept either bare code OR full callback URL — extract the code field."""
    pasted = pasted.strip()
    if not pasted:
        return ""
    if pasted.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(pasted)
        params = urllib.parse.parse_qs(parsed.query)
        return (params.get("code") or [""])[0].strip()
    return pasted


def _read_env_keys(keys: set[str]) -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        k = k.strip()
        if k in keys:
            out[k] = v.strip()
    return out


def _write_env(
    account_handle: str, access_token: str, open_id: str, refresh_token: str,
) -> tuple[str, str, str]:
    keys = ACCOUNT_ENV_MAP[account_handle]
    session_key = keys["session"]
    business_id_key = keys["business_id"]
    refresh_key = keys["refresh"]

    if not access_token:
        print("\n✗ refusing to write empty access_token to .env")
        sys.exit(1)
    if not open_id:
        print(f"\n⚠ open_id is empty — {business_id_key} will be empty")
    if not refresh_token:
        print(f"\n⚠ refresh_token is empty — {refresh_key} will be empty "
              f"(no refresh helper possible; re-run get_token.py every 24h)")

    print()
    print(f"  writing to {ENV_PATH}:")
    print(f"    {session_key}     = {_redact(access_token)}")
    print(f"    {business_id_key} = {_redact(open_id)}")
    print(f"    {refresh_key}     = {_redact(refresh_token)}")

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

    # Post-write verify
    re_read = _read_env_keys({session_key, business_id_key, refresh_key})
    expected = {
        session_key: access_token,
        business_id_key: open_id,
        refresh_key: refresh_token,
    }
    drift = [(k, re_read.get(k), expected[k]) for k in expected
             if re_read.get(k) != expected[k]]
    if drift:
        print("\n✗ post-write verification failed:")
        for k, got, exp in drift:
            print(f"    {k}: wrote {_redact(exp)}, file reads {_redact(got)}")
        sys.exit(1)
    print(f"  ✓ post-write verify: all 3 keys present in .env with matching values")
    return session_key, business_id_key, refresh_key


# ── Main flow ──────────────────────────────────────────────────────────────

def print_usage_and_url() -> None:
    print("Usage: python get_token.py <account_handle> <auth_code>")
    print()
    print(f"  Valid handles: {', '.join(ACCOUNT_ENV_MAP)}")
    print()
    if not CLIENT_KEY or not CLIENT_SECRET:
        print("✗ TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET not set in .env")
        return
    print("To get an authorization code, open this URL in any browser,")
    print("sign in as the target TikTok account, click Authorize, then")
    print("copy the `code` parameter from the URL the browser tries to")
    print("redirect to (it'll fail to load — that's fine):")
    print()
    print(f"  {_build_authorize_url()}")
    print()
    print("Then run:")
    print("  python get_token.py sharpguylab <paste_code>")
    print()


def main() -> None:
    if len(sys.argv) < 3:
        print_usage_and_url()
        sys.exit(2)

    if not CLIENT_KEY or not CLIENT_SECRET:
        print("✗ TIKTOK_CLIENT_KEY or TIKTOK_CLIENT_SECRET is empty in .env")
        sys.exit(1)

    account = sys.argv[1].strip().lower()
    raw_code = " ".join(sys.argv[2:])  # tolerate multi-arg paste if shell split on `&`

    if account not in ACCOUNT_ENV_MAP:
        print(f"✗ Unknown account: {account!r}")
        print(f"  Valid: {', '.join(ACCOUNT_ENV_MAP)}")
        sys.exit(1)

    code = _extract_code(raw_code)
    if not code:
        print("✗ Could not extract a code from the second argument.")
        print(f"  Got: {raw_code[:80]!r}")
        sys.exit(1)

    print()
    print(f"  account:      {account}")
    print(f"  code:         {code[:12]}...  (length {len(code)})")
    print(f"  redirect_uri: {REDIRECT_URI}")
    print(f"  POST {TOKEN_URL}")
    print(f"  (no PKCE — confidential-client flow with client_secret only)")
    print()

    try:
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
    except requests.RequestException as e:
        print(f"✗ Network error: {e}")
        sys.exit(1)

    print(f"  ↳ HTTP {r.status_code}  ·  content-type: {r.headers.get('Content-Type', '?')}")
    try:
        body = r.json()
    except ValueError:
        print(f"  ↳ raw (non-JSON): {r.text[:500]}")
        sys.exit(1)

    redacted = _json.dumps(_redact_body(body), indent=2)
    for line in redacted.splitlines():
        print(f"  ↳ {line}")

    if r.status_code >= 400:
        print(f"\n✗ Token exchange failed at HTTP layer: {r.status_code}")
        sys.exit(1)

    # Look for tokens under both shapes (top-level OR under `data`).
    # Pick whichever has a non-empty access_token (the bug we hit
    # previously was settling for an empty token because the KEY was
    # present in the wrong wrapping).
    candidates: list[tuple[str, dict[str, Any]]] = []
    if isinstance(body.get("data"), dict):
        candidates.append(("body['data']", body["data"]))
    candidates.append(("body (top-level)", body))

    chosen: dict[str, Any] | None = None
    for label, c in candidates:
        if c and isinstance(c.get("access_token"), str) and c["access_token"].strip():
            chosen = c
            print(f"  ↳ extracted tokens from {label}")
            break

    if chosen is None:
        err = body.get("error") or (body.get("data") or {}).get("error")
        desc = (
            body.get("error_description")
            or (body.get("data") or {}).get("error_description")
            or (body.get("data") or {}).get("description")
        )
        print("\n✗ Response had no usable access_token.")
        if err:
            print(f"  TikTok error:       {err}")
        if desc:
            print(f"  TikTok description: {desc}")
        log_id = body.get("log_id") or (body.get("data") or {}).get("log_id")
        if log_id:
            print(f"  log_id:             {log_id}")
        print()
        print("  At this point the bug is on TikTok's side, not the script's.")
        print("  Check the dev console for:")
        print("    1. 'Confidential Client' / 'Web App' client type")
        print("    2. PKCE toggle — try DISABLING it explicitly")
        print("    3. http://localhost:8080/callback in allowed redirect URIs")
        print("    4. user.info.basic + video.upload + video.publish scopes approved")
        sys.exit(1)

    access_token = chosen["access_token"]
    open_id = chosen.get("open_id", "")
    refresh_token = chosen.get("refresh_token", "")

    _write_env(account, access_token, open_id, refresh_token)

    expires_in = int(chosen.get("expires_in") or 0)
    refresh_expires_in = int(chosen.get("refresh_expires_in") or 0)
    print()
    print(f"  ✓ @{account} authorized.")
    print(f"  ⏰ access_token expires in:  {expires_in // 3600}h {(expires_in % 3600) // 60}m")
    print(f"  ⏰ refresh_token expires in: {refresh_expires_in // 86400}d")
    print()


if __name__ == "__main__":
    main()
