#!/usr/bin/env python3
"""TikTok Content Posting API — OAuth helper (JSON body variant).

Identical to tiktok_oauth.py EXCEPT the token-exchange POST is sent as a
JSON body (Content-Type: application/json) instead of form-encoded
(application/x-www-form-urlencoded). Folds in the "URL-encode the
verifier" idea from the diagnostic spec — JSON sidesteps the URL
encoding question entirely (JSON strings just need JSON escaping,
which requests' json= handles).

ALL OTHER BEHAVIOR IS IDENTICAL to tiktok_oauth.py:
  - PKCE S256, base64url-strict verifier alphabet [A-Za-z0-9-_]
  - 64-char verifier (within RFC 7636's 43-128 range)
  - http://localhost:8080/callback redirect URI
  - Sandbox/production lanes via --sandbox flag

Usage:
    python tiktok_oauth_encoded.py sharpguylab
    python tiktok_oauth_encoded.py sharpguylab --sandbox

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

import base64
import hashlib
import http.server
import os
import secrets
import socketserver
import string
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

# Production credentials (default unless --sandbox flag is passed).
CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()

# Sandbox credentials. TikTok's sandbox uses the same auth + token endpoints —
# the app's sandbox-ness is determined by the client_key (sb-prefixed).
SANDBOX_CLIENT_KEY = os.environ.get("TIKTOK_SANDBOX_CLIENT_KEY", "").strip()
SANDBOX_CLIENT_SECRET = os.environ.get("TIKTOK_SANDBOX_CLIENT_SECRET", "").strip()

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

# Mirror of ACCOUNT_ENV_MAP with SANDBOX_ prefix on every target key, so
# sandbox tokens never overwrite production tokens. Generated programmatically
# from ACCOUNT_ENV_MAP to keep them in sync — if you add an account, you
# only need to update one place.
SANDBOX_ACCOUNT_ENV_MAP: dict[str, dict[str, str]] = {
    handle: {role: key.replace("TIKTOK_", "TIKTOK_SANDBOX_", 1) for role, key in keys.items()}
    for handle, keys in ACCOUNT_ENV_MAP.items()
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


# TikTok's PKCE validator rejects the full RFC 7636 unreserved set
# [A-Z a-z 0-9 - . _ ~]. Per TikTok's actual behavior (3 rejection rounds
# with verifiers that included `.` or `~`), restrict the verifier alphabet
# to base64url-strict: [A-Z a-z 0-9 - _]. This is a subset of both
# RFC 7636 §4.1 (so still spec-compliant) and the base64url alphabet
# (so always safe in URLs and form bodies without escaping).
PKCE_VERIFIER_ALPHABET = string.ascii_letters + string.digits + "-_"  # 64 chars
PKCE_UNRESERVED = PKCE_VERIFIER_ALPHABET   # backward-compat name retained
PKCE_VERIFIER_LENGTH = 64  # within RFC's 43-128 range


def _generate_pkce() -> tuple[str, str]:
    """Generate a TikTok-strict PKCE pair.

    Returns (code_verifier, code_challenge).

    Spec we're targeting:
      - verifier:  exactly PKCE_VERIFIER_LENGTH (64) chars from the
                   base64url-strict alphabet [A-Z a-z 0-9 - _]
                   (RFC 7636 allows `. ~` too; TikTok rejects those)
      - challenge: BASE64URL(SHA256(ASCII(verifier))), all `=` padding stripped

    Both values are PRINTED in run_oauth for manual verification — see the
    `openssl dgst -sha256` recipe printed alongside.
    """
    verifier = "".join(secrets.choice(PKCE_VERIFIER_ALPHABET) for _ in range(PKCE_VERIFIER_LENGTH))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _build_authorize_url(client_key: str, state: str, code_challenge: str) -> str:
    qs = urllib.parse.urlencode({
        "client_key": client_key,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
        # PKCE — TikTok requires both params.
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE_URL}?{qs}"


def _redact(value: str, head: int = 8, tail: int = 4) -> str:
    """Show first `head` and last `tail` chars + length for sensitive values.

    Lets us log the SHAPE of a token without dumping the full credential to
    terminal scrollback / log files. None and empty string both render as
    a clear marker so empty values are visually obvious.
    """
    if value is None:
        return "<None>"
    s = str(value)
    if not s:
        return "<EMPTY>"
    if len(s) <= head + tail:
        return f"<{len(s)} chars: {s!r}>"
    return f"<{len(s)} chars: {s[:head]!r}...{s[-tail:]!r}>"


def _log_response(r: "requests.Response") -> dict[str, Any]:
    """Print HTTP status + parsed body (with token values redacted) and
    return the parsed JSON body. Lets us see the SHAPE of TikTok's response
    even when our extraction logic gets it wrong.
    """
    print(f"  ↳ HTTP {r.status_code}  ·  content-type: {r.headers.get('Content-Type', '?')}")
    try:
        body = r.json()
    except ValueError:
        print(f"  ↳ raw (non-JSON): {r.text[:500]}")
        return {}

    # Pretty-print the body with token values redacted. Recursive walk.
    SENSITIVE_KEYS = {"access_token", "refresh_token"}

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: (_redact(v) if k in SENSITIVE_KEYS and isinstance(v, str) else _walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [_walk(x) for x in node]
        return node

    import json as _json
    redacted = _json.dumps(_walk(body), indent=2)
    for line in redacted.splitlines():
        print(f"  ↳ {line}")
    return body


def _exchange_code_for_token(
    code: str, code_verifier: str, *,
    client_key: str, client_secret: str,
) -> dict[str, Any]:
    print()
    print(f"  POST {TOKEN_URL}")
    print(f"    grant_type=authorization_code")
    print(f"    client_key={client_key[:6]}...  (key preview)")
    print(f"    code={code[:8]}...  (length {len(code)})")
    print(f"    redirect_uri={REDIRECT_URI}")
    print(f"    code_verifier={code_verifier}")
    print(f"    code_verifier length: {len(code_verifier)} chars")
    print(f"    body: JSON (Content-Type: application/json) — NOT form-encoded")

    # JSON body instead of form-encoded. `requests.post(..., json=...)` sets
    # Content-Type: application/json and JSON-encodes the dict — bypasses
    # the form-encoding code path entirely.
    r = requests.post(
        TOKEN_URL,
        json={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )

    body = _log_response(r)

    if r.status_code >= 400:
        print(f"\n✗ Token exchange failed at HTTP layer: {r.status_code}")
        sys.exit(1)

    # TikTok sometimes wraps the payload under "data", sometimes not — try both
    # and pick whichever has a truthy access_token. Previously we used the first
    # shape that had the KEY present, but TikTok can return a top-level 200 with
    # access_token PRESENT BUT EMPTY (e.g. on certain PKCE/scope edge cases) —
    # writing that to .env produced today's "completed without errors but empty
    # values" symptom. The fix: require a NON-EMPTY access_token.
    candidates = []
    if isinstance(body.get("data"), dict):
        candidates.append(("body['data']", body["data"]))
    candidates.append(("body (top-level)", body))

    chosen = None
    for label, c in candidates:
        if c and isinstance(c.get("access_token"), str) and c["access_token"].strip():
            chosen = c
            print(f"  ↳ extracted tokens from {label}")
            break

    if chosen is None:
        # Surface TikTok's error fields explicitly so the user knows what to fix.
        err = body.get("error") or (body.get("data") or {}).get("error")
        desc = (
            body.get("error_description")
            or (body.get("data") or {}).get("error_description")
            or (body.get("data") or {}).get("description")
        )
        print(f"\n✗ Token exchange returned a non-error response but no usable access_token.")
        if err:
            print(f"  TikTok error:        {err}")
        if desc:
            print(f"  TikTok description:  {desc}")
        print(f"  log_id (for support): {body.get('log_id') or (body.get('data') or {}).get('log_id')}")
        print(f"\n  Common causes:")
        print(f"    - PKCE verifier doesn't match the challenge sent at /authorize")
        print(f"      (use the openssl verify command printed earlier to check the math)")
        print(f"    - authorization code already used or expired (single-use, ~10 min)")
        print(f"    - redirect_uri at exchange differs from the one at /authorize")
        print(f"    - app missing the requested scopes ({', '.join(SCOPES)})")
        print(f"    - client_secret in .env doesn't match the TikTok app's secret")
        sys.exit(1)

    return chosen


def _write_env(
    account_handle: str, token_data: dict[str, Any], *,
    env_map: dict[str, dict[str, str]],
) -> tuple[str, str, str]:
    """Replace (or append) the three per-account env var lines in .env.
    Atomic via temp file + rename so an interrupted run can't corrupt secrets.

    `env_map` selects which set of target env-var names to write into —
    production (ACCOUNT_ENV_MAP) or sandbox (SANDBOX_ACCOUNT_ENV_MAP).
    """
    keys = env_map[account_handle]
    session_key = keys["session"]
    business_id_key = keys["business_id"]
    refresh_key = keys["refresh"]

    access_token = token_data["access_token"]
    open_id = token_data.get("open_id", "")
    refresh_token = token_data.get("refresh_token", "")

    # Defensive: if any of these are empty we should NOT silently write empty
    # lines to .env. The _exchange_code_for_token check above should have
    # already bailed in this case, but belt-and-braces — the bug we're fixing
    # is precisely "wrote empty values without complaining."
    if not access_token:
        print(f"\n✗ refusing to write empty access_token to .env")
        sys.exit(1)
    if not open_id:
        print(f"\n⚠ open_id is empty in token response — {business_id_key} will be empty")
    if not refresh_token:
        print(f"\n⚠ refresh_token is empty in token response — {refresh_key} will be empty "
              f"(no refresh helper possible; you'll have to re-run OAuth every 24h)")

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

    # Post-write verification — re-read .env and confirm the three values
    # landed. Catches every silent-failure path: file permission weirdness,
    # path mismatch, line-matching off-by-one, anything we haven't thought of.
    written = _read_env_keys({session_key, business_id_key, refresh_key})
    expected = {
        session_key: access_token,
        business_id_key: open_id,
        refresh_key: refresh_token,
    }
    drift = [(k, written.get(k), expected[k]) for k in expected
             if written.get(k) != expected[k]]
    if drift:
        print(f"\n✗ post-write verification failed at {ENV_PATH}:")
        for k, got, exp in drift:
            print(f"    {k}: wrote {_redact(exp)}, file now reads {_redact(got)}")
        sys.exit(1)
    print(f"  ✓ post-write verify: all 3 keys present in .env with matching values")

    return session_key, business_id_key, refresh_key


def _read_env_keys(keys: set[str]) -> dict[str, str]:
    """Parse .env for the specified keys. Doesn't use python-dotenv because
    we want the LITERAL on-disk value, not what got loaded into os.environ
    at process start.
    """
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


def run_oauth(account_handle: str, *, sandbox: bool = False) -> None:
    # Pick credentials + target env keys based on --sandbox flag.
    if sandbox:
        client_key = SANDBOX_CLIENT_KEY
        client_secret = SANDBOX_CLIENT_SECRET
        env_map = SANDBOX_ACCOUNT_ENV_MAP
        env_label = "SANDBOX"
        cred_missing_msg = (
            "✗ TIKTOK_SANDBOX_CLIENT_KEY or TIKTOK_SANDBOX_CLIENT_SECRET is empty in .env"
        )
    else:
        client_key = CLIENT_KEY
        client_secret = CLIENT_SECRET
        env_map = ACCOUNT_ENV_MAP
        env_label = "PRODUCTION"
        cred_missing_msg = (
            "✗ TIKTOK_CLIENT_KEY or TIKTOK_CLIENT_SECRET is empty in .env"
        )

    if not client_key or not client_secret:
        print(cred_missing_msg)
        sys.exit(1)
    if account_handle not in env_map:
        print(f"✗ Unknown account: {account_handle!r}")
        print(f"  Valid: {', '.join(env_map)}")
        sys.exit(1)

    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce()
    auth_url = _build_authorize_url(client_key, state, code_challenge)

    # Defensive assertions on the PKCE pair before we even open the browser.
    # If any of these fail, the bug is in _generate_pkce, not in TikTok.
    assert 43 <= len(code_verifier) <= 128, f"verifier length out of spec: {len(code_verifier)}"
    assert all(c in PKCE_UNRESERVED for c in code_verifier), "verifier has out-of-charset chars"
    assert len(code_challenge) == 43, f"S256 challenge should be 43 chars, got {len(code_challenge)}"
    assert "=" not in code_challenge, "challenge has padding (should be stripped)"

    print()
    print(f"  TikTok OAuth — authorizing @{account_handle}  [{env_label}]")
    print(f"  client_key:   {client_key[:6]}...")
    print(f"  redirect_uri: {REDIRECT_URI}")
    print(f"  scopes:       {', '.join(SCOPES)}")
    print(f"  PKCE:         S256 (strict spec — full values printed for verification)")
    print()
    print(f"  code_verifier:  {code_verifier}")
    print(f"    length:       {len(code_verifier)} chars (must be 43-128)")
    print(f"    charset OK:   {all(c in PKCE_UNRESERVED for c in code_verifier)} (RFC 7636 unreserved set)")
    print()
    print(f"  code_challenge: {code_challenge}")
    print(f"    method:       S256")
    print(f"    length:       {len(code_challenge)} chars (always 43 for SHA-256 base64url no-pad)")
    print()
    print(f"  Verify the math yourself:")
    print(f"    echo -n '{code_verifier}' | openssl dgst -sha256 -binary | base64 | tr '+/' '-_' | tr -d '='")
    print(f"  Should print: {code_challenge}")
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
    print("  ✓ Authorization code received, exchanging for access token (with PKCE verifier)...")
    token_data = _exchange_code_for_token(
        code, code_verifier,
        client_key=client_key, client_secret=client_secret,
    )

    session_key, business_id_key, refresh_key = _write_env(
        account_handle, token_data, env_map=env_map,
    )

    expires_in = int(token_data.get("expires_in") or 0)
    refresh_expires_in = int(token_data.get("refresh_expires_in") or 0)

    print()
    print(f"  ✓ @{account_handle} authorized [{env_label}]. Tokens written to .env:")
    print(f"    {session_key}")
    print(f"    {business_id_key}      = {token_data.get('open_id', '')}")
    print(f"    {refresh_key}")
    print()
    print(f"  ⏰ access_token expires in:  {expires_in // 3600}h {(expires_in % 3600) // 60}m")
    print(f"  ⏰ refresh_token expires in: {refresh_expires_in // 86400}d "
          f"(refresh helper uses it to mint new access_tokens without re-auth)")
    print()


def main() -> None:
    # Parse --sandbox flag positionally — accepted anywhere in argv.
    args = [a for a in sys.argv[1:] if a != "--sandbox"]
    sandbox = "--sandbox" in sys.argv[1:]

    if len(args) != 1:
        print("Usage: python tiktok_oauth.py <account_handle> [--sandbox]")
        print(f"  Valid handles: {', '.join(ACCOUNT_ENV_MAP)}")
        print()
        print("  --sandbox     Use TIKTOK_SANDBOX_CLIENT_KEY/SECRET and write tokens to")
        print("                TIKTOK_SANDBOX_SESSION_<HANDLE> etc. so production tokens")
        print("                stay untouched.")
        sys.exit(2)
    run_oauth(args[0].lower(), sandbox=sandbox)


if __name__ == "__main__":
    main()
