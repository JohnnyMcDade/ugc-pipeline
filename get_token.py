#!/usr/bin/env python3
"""Manual + auto TikTok token exchange — no PKCE.

This is the no-PKCE fallback for when tiktok_oauth.py's PKCE flow keeps
failing due to TikTok app-config edge cases. Identical confidential-client
flow (client_secret only, no code_verifier / code_challenge), with two
modes for getting the authorization code:

  AUTO-CAPTURE MODE  (recommended if you can run a local server)
    python get_token.py <account_handle>            # production
    python get_token.py <account_handle> --sandbox  # sandbox

    Opens the TikTok authorize page in your default browser, captures
    the redirect to http://localhost:8080/callback automatically. Same
    UX as tiktok_oauth.py, just without PKCE.

  MANUAL-PASTE MODE  (use when you can't bind port 8080 / SSH session etc)
    python get_token.py <account_handle> <code>            # production
    python get_token.py <account_handle> <code> --sandbox  # sandbox

    You visit the authorize URL yourself, copy the `code` from the
    failed redirect URL bar, paste it as the second argument. Tolerates
    either the bare code OR the full callback URL.

In either mode, --sandbox switches to the sandbox client_key/secret AND
writes tokens to TIKTOK_SANDBOX_SESSION_<HANDLE> etc. so production
tokens are never touched.

PREREQUISITES on the TikTok dev console:
  - http://localhost:8080/callback in the allowed redirect URIs
  - Content Posting API + Login Kit products enabled
  - user.info.basic, video.upload, video.publish scopes approved
  - For the production app: PKCE Required toggle should be OFF (this
    script sends no PKCE; if PKCE is required, switch to tiktok_oauth.py)

If TikTok still rejects the response with no usable access_token, see
the printed "common causes" checklist for app-config knobs to inspect.
"""

from __future__ import annotations

import http.server
import json as _json
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

# Production credentials.
CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()

# Sandbox credentials (parallel app, sb-prefixed client_key).
SANDBOX_CLIENT_KEY = os.environ.get("TIKTOK_SANDBOX_CLIENT_KEY", "").strip()
SANDBOX_CLIENT_SECRET = os.environ.get("TIKTOK_SANDBOX_CLIENT_SECRET", "").strip()

REDIRECT_URI = "http://localhost:8080/callback"
CALLBACK_PORT = 8080
SCOPES = "user.info.basic,video.upload,video.publish"
STATE = "test123"  # fixed; CSRF surface in a single-user terminal flow is nil

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
ENV_PATH = Path(__file__).parent / ".env"
CALLBACK_TIMEOUT_SECONDS = 240

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

# Generated programmatically from the prod map so adding an account is
# a one-place edit.
SANDBOX_ACCOUNT_ENV_MAP: dict[str, dict[str, str]] = {
    handle: {role: key.replace("TIKTOK_", "TIKTOK_SANDBOX_", 1) for role, key in keys.items()}
    for handle, keys in ACCOUNT_ENV_MAP.items()
}

# Captured by the callback handler in the server thread; consumed by main.
_captured: dict[str, Any] = {
    "code": None, "state": None, "error": None, "error_description": None,
}
_done = threading.Event()


# ── Redaction + display helpers ────────────────────────────────────────────

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


# ── Auth + token endpoints ─────────────────────────────────────────────────

def _build_authorize_url(client_key: str) -> str:
    qs = urllib.parse.urlencode({
        "client_key": client_key,
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


def _exchange_code(code: str, *, client_key: str, client_secret: str) -> dict[str, Any] | None:
    """POST to /v2/oauth/token/ with no PKCE. Returns the inner token dict
    on success, None on failure (with a printed error explaining why).
    """
    print()
    print(f"  POST {TOKEN_URL}")
    print(f"    grant_type=authorization_code")
    print(f"    client_key={client_key[:6]}...  (key preview)")
    print(f"    code={code[:8]}...  (length {len(code)})")
    print(f"    redirect_uri={REDIRECT_URI}")
    print(f"    (no PKCE — confidential-client flow with client_secret only)")
    print()

    try:
        r = requests.post(
            TOKEN_URL,
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"✗ Network error: {e}")
        return None

    print(f"  ↳ HTTP {r.status_code}  ·  content-type: {r.headers.get('Content-Type', '?')}")
    try:
        body = r.json()
    except ValueError:
        print(f"  ↳ raw (non-JSON): {r.text[:500]}")
        return None

    redacted = _json.dumps(_redact_body(body), indent=2)
    for line in redacted.splitlines():
        print(f"  ↳ {line}")

    if r.status_code >= 400:
        print(f"\n✗ Token exchange failed at HTTP layer: {r.status_code}")
        return None

    # Two possible response shapes — pick whichever has a non-empty access_token.
    candidates: list[tuple[str, dict[str, Any]]] = []
    if isinstance(body.get("data"), dict):
        candidates.append(("body['data']", body["data"]))
    candidates.append(("body (top-level)", body))

    for label, c in candidates:
        if c and isinstance(c.get("access_token"), str) and c["access_token"].strip():
            print(f"  ↳ extracted tokens from {label}")
            return c

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
    return None


# ── Local callback server (auto-capture mode) ──────────────────────────────

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


def _capture_code_via_browser(client_key: str) -> str | None:
    """Auto-capture mode: bind localhost:8080, open browser, wait for the
    TikTok redirect. Returns the code on success, None on timeout / error.
    """
    # Reset captured state in case this is the second auto-capture in one
    # process (shouldn't happen with main(), but be defensive).
    _captured.update({"code": None, "state": None, "error": None, "error_description": None})
    _done.clear()

    socketserver.TCPServer.allow_reuse_address = True
    try:
        server = socketserver.TCPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    except OSError as e:
        print(f"✗ Could not bind localhost:{CALLBACK_PORT}: {e}")
        print(f"  Kill whatever owns the port: lsof -ti:{CALLBACK_PORT} | xargs kill")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_url = _build_authorize_url(client_key)
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
        return None
    if _captured["error"]:
        print(f"\n✗ OAuth error from TikTok: {_captured['error']}")
        if _captured["error_description"]:
            print(f"  description: {_captured['error_description']}")
        return None
    code = _captured["code"]
    if not code:
        print("\n✗ No authorization code received from TikTok")
        return None
    return code


# ── .env write (atomic + post-write verify) ────────────────────────────────

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
    *, env_map: dict[str, dict[str, str]],
) -> tuple[str, str, str]:
    keys = env_map[account_handle]
    session_key = keys["session"]
    business_id_key = keys["business_id"]
    refresh_key = keys["refresh"]

    if not access_token:
        print("\n✗ refusing to write empty access_token to .env")
        sys.exit(1)
    if not open_id:
        print(f"\n⚠ open_id is empty — {business_id_key} will be empty")
    if not refresh_token:
        print(f"\n⚠ refresh_token is empty — {refresh_key} will be empty")

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


# ── Usage + main ──────────────────────────────────────────────────────────

def _print_usage(client_key: str, env_label: str) -> None:
    print("Usage:")
    print("  python get_token.py <handle>                  # auto-capture (production)")
    print("  python get_token.py <handle> --sandbox        # auto-capture (sandbox)")
    print("  python get_token.py <handle> <code>           # manual paste (production)")
    print("  python get_token.py <handle> <code> --sandbox # manual paste (sandbox)")
    print()
    print(f"  Valid handles: {', '.join(ACCOUNT_ENV_MAP)}")
    print()
    if client_key:
        print(f"To get an authorization code manually ({env_label} app), visit this URL,")
        print("sign in as the target TikTok account, click Authorize, then copy the")
        print("`code` parameter from the URL the browser tries to redirect to:")
        print()
        print(f"  {_build_authorize_url(client_key)}")
        print()


def main() -> None:
    # Strip --sandbox out of positional args.
    sandbox = "--sandbox" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "--sandbox"]

    # Pick credential lane + target env map up front.
    if sandbox:
        client_key = SANDBOX_CLIENT_KEY
        client_secret = SANDBOX_CLIENT_SECRET
        env_map = SANDBOX_ACCOUNT_ENV_MAP
        env_label = "SANDBOX"
        missing_msg = "✗ TIKTOK_SANDBOX_CLIENT_KEY or TIKTOK_SANDBOX_CLIENT_SECRET is empty in .env"
    else:
        client_key = CLIENT_KEY
        client_secret = CLIENT_SECRET
        env_map = ACCOUNT_ENV_MAP
        env_label = "PRODUCTION"
        missing_msg = "✗ TIKTOK_CLIENT_KEY or TIKTOK_CLIENT_SECRET is empty in .env"

    if len(args) < 1 or len(args) > 2:
        _print_usage(client_key, env_label)
        sys.exit(2)

    if not client_key or not client_secret:
        print(missing_msg)
        sys.exit(1)

    account = args[0].strip().lower()
    if account not in env_map:
        print(f"✗ Unknown account: {account!r}")
        print(f"  Valid: {', '.join(env_map)}")
        sys.exit(1)

    print()
    print(f"  get_token.py — @{account}  [{env_label}]")
    print(f"  client_key:   {client_key[:6]}...")
    print(f"  redirect_uri: {REDIRECT_URI}")
    print(f"  scopes:       {SCOPES}")
    print()

    # Decide mode: 1 arg → auto-capture; 2 args → manual paste.
    if len(args) == 1:
        print("  mode: auto-capture (will open browser + listen on localhost:8080)")
        code = _capture_code_via_browser(client_key)
        if not code:
            sys.exit(1)
    else:
        print("  mode: manual paste")
        code = _extract_code(args[1])
        if not code:
            print("✗ Could not extract a code from the second argument.")
            print(f"  Got: {args[1][:80]!r}")
            sys.exit(1)

    print(f"  ✓ have authorization code, exchanging...")
    token_data = _exchange_code(code, client_key=client_key, client_secret=client_secret)
    if token_data is None:
        sys.exit(1)

    access_token = token_data["access_token"]
    open_id = token_data.get("open_id", "")
    refresh_token = token_data.get("refresh_token", "")

    _write_env(account, access_token, open_id, refresh_token, env_map=env_map)

    expires_in = int(token_data.get("expires_in") or 0)
    refresh_expires_in = int(token_data.get("refresh_expires_in") or 0)
    print()
    print(f"  ✓ @{account} authorized [{env_label}].")
    print(f"  ⏰ access_token expires in:  {expires_in // 3600}h {(expires_in % 3600) // 60}m")
    print(f"  ⏰ refresh_token expires in: {refresh_expires_in // 86400}d")
    print()


if __name__ == "__main__":
    main()
