"""
Auto-login ChatGPT accounts via OAuth PKCE flow.
Uses Playwright to automate browser login, fills email/password/2FA,
catches the OAuth callback, and imports refresh_token into 9router.

Input format (one per line):
  email|password|2fa_secret

Usage:
  python auto_login.py accounts.txt           # headless
  python auto_login.py accounts.txt --headed   # show browser
  python auto_login.py accounts.txt --slow     # slow mode for debugging
"""

import sys
import os
import json
import time
import base64
import hashlib
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Force UTF-8 + unbuffered output on Windows so server.py receives log
# lines in real-time instead of waiting for Python's pipe buffer.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    # On non-Windows, ensure line-buffered output
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

# ---------- TOTP ----------
try:
    import pyotp
except ImportError:
    print("[!] pyotp not installed. Run: python -m pip install pyotp")
    sys.exit(1)

# ---------- Playwright ----------
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[!] playwright not installed. Run: python -m pip install playwright && python -m playwright install chromium")
    sys.exit(1)

# ---------- OAuth Config (same as 9router) ----------
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
SCOPE = "openid profile email offline_access"
CALLBACK_PORT = 1455
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/auth/callback"
IMPORT_API = "http://localhost:9876/api/import"

# ---------- PKCE ----------
def generate_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url():
    """Build a fresh OAuth URL using the registered Codex callback URI."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
    }
    return AUTH_URL + "?" + urlencode(params), verifier, state


def exchange_code(code, verifier):
    body = json.dumps({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }).encode()
    req = Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        resp = urlopen(req, timeout=30)
        return json.loads(resp.read().decode()), None
    except HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return None, str(e)


def decode_jwt_email(access_token):
    try:
        parts = access_token.split(".")
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        prof = decoded.get("https://api.openai.com/profile", {})
        auth = decoded.get("https://api.openai.com/auth", {})
        return {
            "email": prof.get("email", ""),
            "account_id": auth.get("chatgpt_account_id", ""),
            "plan_type": auth.get("chatgpt_plan_type", ""),
        }
    except:
        return {"email": "", "account_id": "", "plan_type": ""}


def tokens_to_connection(tokens):
    at = tokens.get("access_token", "")
    info = decode_jwt_email(at)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    exp_in = tokens.get("expires_in", 864000)
    exp_at = datetime.fromtimestamp(
        time.time() + exp_in, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {
        "accessToken": at,
        "refreshToken": tokens.get("refresh_token", ""),
        "idToken": tokens.get("id_token", ""),
        "expiresAt": exp_at,
        "expiresIn": exp_in,
        "testStatus": "active",
        "lastUsedAt": now,
        "consecutiveUseCount": 0,
        "backoffLevel": 0,
        "providerSpecificData": {
            "chatgptAccountId": info["account_id"],
            "chatgptPlanType": info["plan_type"],
        },
        "lastError": None,
        "lastErrorAt": None,
        "email": info["email"],
        "name": info["email"],
        "provider": "codex",
        "authType": "oauth",
    }


def import_to_9router(conn):
    """Import via the local server and require SQLite verification for this email."""
    body = json.dumps({"connections": [conn]}).encode("utf-8")
    req = Request(IMPORT_API, data=body, headers={
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        resp = urlopen(req, timeout=15)
        payload = json.loads(resp.read().decode("utf-8"))
        if not payload.get("sqliteVerified"):
            detail = "; ".join(payload.get("errors") or []) or "email not verified in 9router SQLite"
            return None, detail
        return payload, None
    except Exception as e:
        return None, "{}: {}".format(type(e).__name__, str(e))


# ---------- Shared callback dispatcher ----------
class CallbackResult:
    def __init__(self):
        self.code = None
        self.state = None
        self.error = None
        self.done = threading.Event()


_callback_results = {}
_callback_lock = threading.RLock()
_callback_server = None
_callback_thread = None


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        state = params.get("state", [None])[0]
        with _callback_lock:
            result = _callback_results.get(state)
        if not result:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<html><body>OAuth callback khong hop le hoac da het han.</body></html>".encode("utf-8"))
            return
        result.code = params.get("code", [None])[0]
        result.state = state
        result.error = params.get("error", [None])[0]
        result.done.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<html><body style='font-family:system-ui;background:#1a1a2e;color:#e0e0e0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0'><div style='text-align:center'><h2>✅ Thành công!</h2><p>Vui lòng không tắt, để nó tự động hoàn tất.</p></div></body></html>".encode("utf-8"))


def start_callback_dispatcher():
    """Start exactly one server on OpenAI's registered callback port."""
    global _callback_server, _callback_thread
    with _callback_lock:
        if _callback_server:
            return
        class ReusableHTTPServer(HTTPServer):
            allow_reuse_address = True
        _callback_server = ReusableHTTPServer(("127.0.0.1", CALLBACK_PORT), CallbackHandler)
        _callback_thread = threading.Thread(target=_callback_server.serve_forever, daemon=True)
        _callback_thread.start()


def stop_callback_dispatcher():
    global _callback_server, _callback_thread
    with _callback_lock:
        server = _callback_server
        _callback_server = None
        _callback_thread = None
        _callback_results.clear()
    if server:
        server.shutdown()
        server.server_close()


def register_callback(state):
    result = CallbackResult()
    with _callback_lock:
        _callback_results[state] = result
    return result


def unregister_callback(state):
    with _callback_lock:
        _callback_results.pop(state, None)


# ---------- Browser automation ----------
def debug_page(page, label):
    """Save screenshot + log page URL/title for debugging login flow."""
    try:
        safe = ''.join(c if c.isalnum() or c in ('_', '-') else '_' for c in label)[:60]
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"debug_{safe}.png")
        page.screenshot(path=path, full_page=True)
        print(f"    [debug] {label}: title={page.title()!r} url={page.url}")
        print(f"    [debug] screenshot: {path}")
    except Exception as e:
        print(f"    [debug] failed: {e}")


def click_first_visible(page, selectors, timeout=3000):
    """Click the first visible selector from a list. Races all selectors."""
    import time as _time
    # Quick check: any already visible?
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                loc.click()
                return True
        except Exception:
            pass
    # Poll until timeout
    deadline = _time.time() + timeout / 1000
    while _time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.click()
                    return True
            except Exception:
                pass
        _time.sleep(0.1)
    return False


def fill_first_visible(page, selectors, value, timeout=12000):
    """Fill the first visible input from a list. Races all selectors at once."""
    # Strategy 1: Try OR-combined selector for instant match
    combined = " >> visible=true, ".join(selectors)
    try:
        # Build a single locator that matches ANY of the selectors
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.fill(value)
                    return loc
            except Exception:
                pass
    except Exception:
        pass

    # Strategy 2: Poll all selectors rapidly until timeout
    import time as _time
    deadline = _time.time() + timeout / 1000
    while _time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.fill(value)
                    return loc
            except Exception:
                pass
        _time.sleep(0.15)  # Small poll interval

    # Strategy 3: One last sequential attempt with short timeout each
    per_sel = max(500, timeout // len(selectors)) if selectors else timeout
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=per_sel)
            loc.fill(value)
            return loc
        except Exception:
            pass
    return None


def wait_a_bit(page, ms=1500):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=ms)
    except Exception:
        pass
    time.sleep(ms / 1000)


def normalize_totp_secret(secret):
    """Normalize base32 TOTP secret and add padding if needed."""
    s = (secret or "").strip().replace(" ", "").replace("-", "").upper()
    if not s:
        return ""
    pad = len(s) % 8
    if pad:
        s += "=" * (8 - pad)
    return s


def make_totp_code(secret):
    """Generate current TOTP code safely."""
    s = normalize_totp_secret(secret)
    if not s:
        return ""
    return pyotp.TOTP(s).now()


def login_account(page, email, password, totp_secret, headed=False):
    """Navigate one account's OAuth flow through the registered shared callback."""
    auth_url, verifier, state = build_auth_url()
    result = register_callback(state)

    try:
        # Navigate to auth URL
        page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
        wait_a_bit(page, 300)
        debug_page(page, "01_open_auth")

        # Do NOT click generic Continue/Login here. OpenAI's first screen usually
        # contains the email form directly. Since we launch a fresh browser per
        # account there is no previous session, so skip the chooser entirely.

        # --- Step 1: Email ---
        email_input = fill_first_visible(page, [
            'input[name="email"]',
            'input[type="email"]',
            'input[name="username"]',
            'input[id*="email" i]',
            'input[placeholder*="email" i]',
            'input[autocomplete="username"]',
            'input:not([type="hidden"]):not([type="password"])',
        ], email, timeout=10000)

        if not email_input:
            debug_page(page, "02_email_not_found")
            return None, "Email input not found", verifier, state

        time.sleep(0.05)
        if not click_first_visible(page, [
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'button:has-text("Tiếp tục")',
            'button:has-text("Log in")',
        ], timeout=900):
            email_input.press("Enter")
        # Do not sleep here. Wait directly for the password selector below so the
        # password is filled immediately when the field appears.
        debug_page(page, "03_after_email")

        # Some accounts are redirected to Apple/iCloud auth or another IdP.
        # Handle generic email/password pages too.
        # --- Step 2: Password ---
        pwd_input = fill_first_visible(page, [
            'input[name="password"]',
            'input[type="password"]',
            'input[id*="password" i]',
            'input[autocomplete="current-password"]',
            'input[placeholder*="password" i]',
            'input[placeholder*="mật khẩu" i]',
        ], password, timeout=9000)

        if not pwd_input:
            debug_page(page, "04_password_not_found")
            return None, "Password input not found", verifier, state

        time.sleep(0.05)
        if not click_first_visible(page, [
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
            'button:has-text("Đăng nhập")',
        ], timeout=900):
            pwd_input.press("Enter")
        wait_a_bit(page, 600)
        debug_page(page, "05_after_password")

        # --- Step 3: 2FA (TOTP) ---
        if totp_secret:
            try:
                otp_code = make_totp_code(totp_secret)
            except Exception as e:
                debug_page(page, "06_totp_secret_error")
                return None, f"Invalid 2FA secret: {e}", verifier, state

            otp_input = fill_first_visible(page, [
                'input[name="code"]',
                'input[inputmode="numeric"]',
                'input[autocomplete="one-time-code"]',
                'input[id*="code" i]',
                'input[placeholder*="code" i]',
                'input[placeholder*="verification" i]',
                'input[aria-label*="code" i]',
            ], otp_code, timeout=12000)

            if otp_input:
                print("    [2FA] Filled TOTP code")
                time.sleep(0.15)
                if not click_first_visible(page, [
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    'button:has-text("Verify")',
                    'button:has-text("Next")',
                    'button:has-text("Submit")',
                ], timeout=2500):
                    otp_input.press("Enter")
                wait_a_bit(page, 2500)
                debug_page(page, "06_after_2fa")
            else:
                print("    [2FA] No TOTP prompt found yet")
                debug_page(page, "06_2fa_not_found")

        # --- Step 4: Consent + callback ---
        # If final consent/authorization keeps loading, retry the consent click up
        # to 2 more times before marking the account failed.
        got_callback = False
        for consent_try in range(3):
            # Check if already on callback before trying consent clicks
            if "localhost" in page.url and "/auth/callback" in page.url:
                got_callback = result.done.wait(timeout=1)
                if not got_callback:
                    parsed = urlparse(page.url)
                    params = parse_qs(parsed.query)
                    callback_state = params.get("state", [None])[0]
                    if callback_state == state:
                        result.code = params.get("code", [None])[0]
                        result.state = callback_state
                        result.error = params.get("error", [None])[0]
                        got_callback = result.code is not None or result.error is not None
                if got_callback:
                    break

            for _ in range(3):
                clicked = click_first_visible(page, [
                    'button:has-text("Continue")',
                    'button:has-text("Authorize")',
                    'button:has-text("Allow")',
                    'button:has-text("Accept")',
                    'button:has-text("Yes")',
                ], timeout=800)
                if not clicked:
                    break
                print(f"    [consent] Clicked continue/authorize (try {consent_try + 1}/3)")
                wait_a_bit(page, 300)
                if "localhost" in page.url and "/auth/callback" in page.url:
                    break

            try:
                current = page.url
                body_text = page.locator("body").inner_text(timeout=500).lower()
                if "localhost" in current and "/auth/callback" in current:
                    parsed = urlparse(current)
                    params = parse_qs(parsed.query)
                    callback_state = params.get("state", [None])[0]
                    if callback_state == state:
                        result.code = params.get("code", [None])[0]
                        result.state = callback_state
                        result.error = params.get("error", [None])[0]
                        got_callback = result.code is not None or result.error is not None
                        if got_callback:
                            break
                if "invalid_state" in current.lower() or "invalid_state" in body_text or "session ended" in body_text:
                    return None, "RETRYABLE_INVALID_STATE: OAuth session ended/invalid_state", verifier, state
            except Exception:
                pass

            if consent_try < 2:
                print(f"    [retry] Callback not received, retrying final consent ({consent_try + 2}/3)")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                wait_a_bit(page, 600)

        if not got_callback:
            # Check if page URL is already on callback
            try:
                current = page.url
                if "localhost" in current and "/auth/callback" in current:
                    parsed = urlparse(current)
                    params = parse_qs(parsed.query)
                    result.code = params.get("code", [None])[0]
                    result.state = params.get("state", [None])[0]
                    got_callback = result.code is not None
            except:
                pass

        if not got_callback:
            return None, "Timeout waiting for callback after 3 consent attempts", verifier, state

        if result.error:
            return None, f"OAuth error: {result.error}", verifier, state

        if not result.code:
            return None, "No authorization code received", verifier, state

        if result.state and result.state != state:
            return None, "OAuth callback state did not match this worker", verifier, state

        # Exchange code for tokens
        tokens, err = exchange_code(result.code, verifier)
        if err:
            return None, f"Token exchange: {err}", verifier, state

        return tokens, None, verifier, state

    finally:
        unregister_callback(state)


# ---------- Main ----------
def parse_accounts(accounts_file):
    accounts = []
    with open(accounts_file, "r", encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            email = parts[0].strip()
            password = parts[1].strip() if len(parts) > 1 else ""
            totp = parts[2].strip() if len(parts) > 2 else ""
            if email and password:
                accounts.append((email, password, totp))
    return accounts


def emit_event(kind, email, **payload):
    """Emit machine-readable progress while keeping normal logs readable."""
    fields = ["EVENT", kind, email]
    fields.extend("{}={}".format(key, str(value).replace("|", "/")) for key, value in payload.items())
    print("|".join(fields), flush=True)


def login_one_account(index, total, account, headed, slow):
    email, password, totp_secret = account
    emit_event("START", email, index=index, total=total)
    print("[{}/{}] {}".format(index, total, email), flush=True)
    last_error = ""
    launch_kwargs = dict(
        headless=not headed,
        slow_mo=500 if slow else 0,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-save-password-bubble",
            "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection",
        ],
    )

    for account_attempt in range(3):
        browser = None
        context = None
        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(channel="chrome", **launch_kwargs)
                    if account_attempt == 0:
                        print("    Browser: Google Chrome | shared callback={}".format(REDIRECT_URI), flush=True)
                except Exception as error:
                    print("    [!] Chrome unavailable, fallback Chromium: {}".format(error), flush=True)
                    browser = pw.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                )
                page = context.new_page()
                if account_attempt:
                    print("    [retry] Restarted browser ({}/3)".format(account_attempt + 1), flush=True)
                tokens, error, _, _ = login_account(page, email, password, totp_secret, headed)
                if error:
                    last_error = error
                    retryable = "RETRYABLE_INVALID_STATE" in error or "Timeout waiting for callback" in error
                    if retryable and account_attempt < 2:
                        print("    [retry] {}".format(error), flush=True)
                        continue
                    clean_error = error.replace("RETRYABLE_INVALID_STATE: ", "")
                    print("    ❌ {}".format(clean_error), flush=True)
                    emit_event("ERROR", email, error=clean_error)
                    return {"email": email, "status": "error", "error": clean_error}

                conn = tokens_to_connection(tokens)
                actual_email = conn.get("email") or email
                plan = conn.get("providerSpecificData", {}).get("chatgptPlanType", "?")
                has_rt = bool(conn.get("refreshToken"))
                print("    ✅ {} | plan={} | rt={}".format(actual_email, plan, "yes" if has_rt else "NO!"), flush=True)
                response, import_error = import_to_9router(conn)
                if response:
                    print("IMPORT_OK|{}|{}|{}|{}".format(actual_email, plan, "yes" if has_rt else "no", response.get("sqlitePath", "")), flush=True)
                    emit_event("SUCCESS", actual_email, plan=plan, refresh="yes" if has_rt else "no")
                    return {"email": actual_email, "status": "success", "plan": plan, "hasRefreshToken": has_rt, "imported": True}

                message = "OAuth OK but 9router import failed: {}".format(import_error or "unknown error")
                print("IMPORT_FAIL|{}|{}".format(actual_email, import_error or "unknown import error"), flush=True)
                emit_event("ERROR", actual_email, error=message)
                return {"email": actual_email, "status": "error", "error": message, "imported": False}
        except Exception as error:
            last_error = str(error)
            if account_attempt < 2:
                print("    [retry] Exception, restarting browser: {}".format(error), flush=True)
                continue
            print("    ❌ Exception: {}".format(error), flush=True)
            emit_event("ERROR", email, error=last_error)
            return {"email": email, "status": "error", "error": last_error}
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
    emit_event("ERROR", email, error=last_error or "Unknown login error")
    return {"email": email, "status": "error", "error": last_error or "Unknown login error"}


def main():
    if len(sys.argv) < 2:
        print("Usage: python auto_login.py accounts.txt [--headed] [--slow] [--workers N]")
        sys.exit(1)
    accounts_file = sys.argv[1]
    headed = "--headed" in sys.argv or "--show" in sys.argv
    slow = "--slow" in sys.argv
    workers = 3
    if "--workers" in sys.argv:
        try:
            workers = int(sys.argv[sys.argv.index("--workers") + 1])
        except (ValueError, IndexError):
            print("[!] --workers must be a positive integer")
            sys.exit(1)
    if workers < 1:
        print("[!] --workers must be a positive integer")
        sys.exit(1)
    if not os.path.exists(accounts_file):
        print("[!] File not found: {}".format(accounts_file))
        sys.exit(1)
    accounts = parse_accounts(accounts_file)
    if not accounts:
        print("[!] No accounts found in file")
        sys.exit(1)

    active_workers = min(workers, len(accounts))
    print("=" * 55)
    print("  Auto-Login ChatGPT → 9router (parallel OAuth PKCE)")
    print("=" * 55)
    print("  Accounts: {} | Workers requested: {} | Running: {}".format(len(accounts), workers, active_workers))
    print("  Mode: {} | Import: {}".format("headed" if headed else "headless", IMPORT_API))
    print("=" * 55, flush=True)

    results = []
    start_callback_dispatcher()
    try:
        with ThreadPoolExecutor(max_workers=active_workers, thread_name_prefix="oauth") as executor:
            futures = {
                executor.submit(login_one_account, index, len(accounts), account, headed, slow): account[0]
                for index, account in enumerate(accounts, start=1)
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as error:
                    email = futures[future]
                    emit_event("ERROR", email, error=str(error))
                    results.append({"email": email, "status": "error", "error": str(error)})
    finally:
        stop_callback_dispatcher()

    results.sort(key=lambda result: (result.get("email") or "").lower())
    ok = sum(1 for result in results if result["status"] == "success")
    fail = sum(1 for result in results if result["status"] == "error")
    print("\n{}\n  SUMMARY\n{}\n  ✅ Success: {}\n  ❌ Failed:  {}\n  Total:     {}".format("=" * 55, "=" * 55, ok, fail, len(results)), flush=True)
    out_file = os.path.join(os.path.dirname(os.path.abspath(accounts_file)), "auto_login_results.json")
    with open(out_file, "w", encoding="utf-8") as output:
        json.dump(results, output, indent=2, ensure_ascii=False)
    print("  Results saved: {}".format(out_file), flush=True)


if __name__ == "__main__":
    main()
