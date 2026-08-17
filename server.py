"""
9router Import Tool — Local Server
Import Codex OAuth connections into both 9router SQLite and n9router db.json.
"""

import sqlite3
import json
import os
import sys
import shutil
import uuid
import base64
import hashlib
import secrets
import webbrowser
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
try:
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
except ImportError:
    from urllib2 import Request, urlopen, URLError, HTTPError

# Force UTF-8 console output on Windows, otherwise logging emoji/arrow output
# from auto_login.py can crash with cp1252 charmap errors.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PORT = 9876
OAUTH_CALLBACK_PORT = 1455
PROVIDER = "codex"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# OAuth config — same as 9router/Codex CLI
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_SCOPE = "openid profile email offline_access"
OAUTH_REDIRECT_URI = "http://localhost:{}/auth/callback".format(OAUTH_CALLBACK_PORT)

# Pending OAuth state
_oauth_pending = {}  # state -> {code_verifier, created_at}

# Storage locations:
# - 9router SQLite: ~/.9router/db/data.sqlite
# - 9router JSON:   ~/.9router/data/db.json
# - n9router JSON:  ~/.n9router/db.json
def find_sqlite():
    candidates = [
        os.path.join(os.path.expanduser("~"), ".9router", "db", "data.sqlite"),
        os.path.join(os.environ.get("APPDATA", ""), "9router", "db", "data.sqlite"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "9router", "db", "data.sqlite"),
        os.path.join(os.path.expanduser("~"), "Library", "Application Support", "9router", "db", "data.sqlite"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


def unique_paths(paths):
    seen = set()
    out = []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def get_json_candidates():
    return unique_paths([
        # 9router v0.5+ root-level db.json (PRIMARY — used by current 9router)
        os.path.join(os.environ.get("APPDATA", ""), "9router", "db.json"),
        os.path.join(os.path.expanduser("~"), ".9router", "db.json"),
        # 9router JSON layouts seen in older versions
        os.path.join(os.path.expanduser("~"), ".9router", "data", "db.json"),
        os.path.join(os.environ.get("APPDATA", ""), "9router", "data", "db.json"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "9router", "data", "db.json"),
        os.path.join(os.path.expanduser("~"), "Library", "Application Support", "9router", "data", "db.json"),
        # n9router JSON layouts
        os.path.join(os.path.expanduser("~"), ".n9router", "db.json"),
        os.path.join(os.environ.get("APPDATA", ""), "n9router", "db.json"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "n9router", "db.json"),
        os.path.join(os.path.expanduser("~"), "Library", "Application Support", "n9router", "db.json"),
    ])


def find_json_paths():
    return [p for p in get_json_candidates() if os.path.exists(p)]


SQLITE_PATH = find_sqlite()
JSON_PATHS = find_json_paths()
DB_JSON_PATH = JSON_PATHS[0] if JSON_PATHS else get_json_candidates()[0]
BACKUP_ROOT = os.path.join(SCRIPT_DIR, "backups")
# OAuth workers may finish at the same time. Protect the full mutation and
# verification sequence so SQLite and JSON always stay consistent.
_storage_lock = threading.RLock()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sqlite_exists():
    return bool(SQLITE_PATH and os.path.exists(SQLITE_PATH))


def json_exists():
    return bool(JSON_PATHS)


def storage_exists():
    return sqlite_exists() or json_exists()


def backup_file(path, label, ext):
    if not path or not os.path.exists(path):
        return None
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bk = os.path.join(BACKUP_ROOT, "backup-{}-{}.{}".format(label, ts, ext))
    shutil.copy2(path, bk)
    return bk


def build_data_blob(conn):
    now = now_iso()
    raw_refresh = conn.get("refreshToken") or ""
    if isinstance(raw_refresh, str) and raw_refresh.startswith("eyJ"):
        raw_refresh = ""
    return {
        "accessToken": conn.get("accessToken") or "",
        "refreshToken": raw_refresh,
        "idToken": conn.get("idToken") or "",
        "expiresAt": conn.get("expiresAt") or "",
        "expiresIn": conn.get("expiresIn") if isinstance(conn.get("expiresIn"), int) else 0,
        "testStatus": conn.get("testStatus") or "active",
        "lastUsedAt": conn.get("lastUsedAt") or now,
        "consecutiveUseCount": 0,
        "backoffLevel": 0,
        "providerSpecificData": conn.get("providerSpecificData") or {},
        "lastError": None,
        "lastErrorAt": None,
    }


def build_json_connection(conn, priority=None, existing=None):
    now = now_iso()
    email_val = conn.get("email") or ""
    name = conn.get("name") or email_val or "Unknown"
    base = dict(existing or {})
    base.update({
        "id": str(uuid.uuid4()),
        "provider": PROVIDER,
        "authType": "oauth",
        "name": name,
        "email": email_val,
        "priority": priority if priority is not None else base.get("priority", 1),
        "isActive": True,
        "updatedAt": now,
    })
    base.update(build_data_blob(conn))
    if not base.get("createdAt"):
        base["createdAt"] = now
    return base


def load_db_json(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("providerConnections"), list):
        data["providerConnections"] = []
    return data


def save_db_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_sqlite_connections():
    if not sqlite_exists():
        return []
    db = sqlite3.connect(SQLITE_PATH, timeout=30)
    cur = db.cursor()
    cur.execute("SELECT id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt FROM providerConnections WHERE provider=? ORDER BY priority", (PROVIDER,))
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    result = []
    for r in rows:
        row = dict(zip(cols, r))
        try:
            row["data"] = json.loads(row["data"]) if row["data"] else {}
        except Exception:
            row["data"] = {}
        result.append(row)
    db.close()
    return result


def get_json_connections():
    if not JSON_PATHS:
        return []
    data = load_db_json(JSON_PATHS[0])
    if not data:
        return []
    result = []
    for conn in data.get("providerConnections", []):
        if conn.get("provider") == PROVIDER:
            row = dict(conn)
            row["data"] = build_data_blob(row)
            result.append(row)
    return sorted(result, key=lambda x: int(x.get("priority") or 999999))


def get_connections():
    json_conns = get_json_connections()
    sqlite_conns = get_sqlite_connections()
    # Prefer showing n9router when available, but keep SQLite visible as fallback.
    return json_conns or sqlite_conns


def import_connections_sqlite(connections):
    if not sqlite_exists():
        return 0, 0, ["9router SQLite not found: {}".format(SQLITE_PATH)]

    backup_file(SQLITE_PATH, "9router-sqlite", "sqlite")
    db = sqlite3.connect(SQLITE_PATH, timeout=30)
    cur = db.cursor()
    cur.execute("SELECT id, email, priority FROM providerConnections WHERE provider=?", (PROVIDER,))
    existing_map = {}
    for row in cur.fetchall():
        if row[1]:
            existing_map[row[1].lower().strip()] = {"id": row[0], "priority": row[2]}

    inserted = 0
    replaced = 0
    errors = []
    now = now_iso()
    # New accounts append after the current last Codex connection. Existing
    # accounts keep their current position when their tokens are replaced.
    cur.execute("SELECT COALESCE(MAX(priority), 0) FROM providerConnections WHERE provider=?", (PROVIDER,))
    next_new_priority = int(cur.fetchone()[0] or 0) + 1
    for conn in connections:
        email = (conn.get("email") or conn.get("name") or "").lower().strip()
        name = conn.get("name") or conn.get("email") or "Unknown"
        email_val = conn.get("email") or ""
        data_json = json.dumps(build_data_blob(conn), ensure_ascii=False)
        try:
            if email and email in existing_map:
                old = existing_map[email]
                cur.execute("DELETE FROM providerConnections WHERE id=? AND provider=?", (old["id"], PROVIDER))
                new_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO providerConnections (id,provider,authType,name,email,priority,isActive,data,createdAt,updatedAt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_id, PROVIDER, "oauth", name, email_val, old["priority"], 1, data_json, now, now))
                replaced += 1
            else:
                new_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO providerConnections (id,provider,authType,name,email,priority,isActive,data,createdAt,updatedAt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_id, PROVIDER, "oauth", name, email_val, next_new_priority, 1, data_json, now, now))
                next_new_priority += 1
                inserted += 1
        except Exception as e:
            errors.append("sqlite {}: {}".format(email_val, str(e)))

    db.commit()
    db.close()
    return inserted, replaced, errors


def import_connections_json_path(connections, json_path):
    data = load_db_json(json_path)
    if data is None:
        return 0, 0, ["JSON db not found: {}".format(json_path)]

    backup_file(json_path, "json-db", "json")
    all_conns = data.get("providerConnections", [])
    existing_codex = []
    other_conns = []
    existing_map = {}
    for conn in all_conns:
        if conn.get("provider") == PROVIDER:
            existing_codex.append(conn)
            email = (conn.get("email") or conn.get("name") or "").lower().strip()
            if email:
                existing_map[email] = conn
        else:
            other_conns.append(conn)

    inserted = 0
    replaced = 0
    errors = []
    incoming = []
    incoming_keys = set()
    for conn in connections:
        try:
            email = (conn.get("email") or conn.get("name") or "").lower().strip()
            if not email:
                errors.append("json Missing email")
                continue
            incoming_keys.add(email)
            old = existing_map.get(email)
            if old:
                incoming.append(build_json_connection(conn, priority=old.get("priority", 1), existing=old))
                replaced += 1
            else:
                incoming.append(build_json_connection(conn, priority=1))
                inserted += 1
        except Exception as e:
            errors.append("json {}: {}".format(conn.get("email") or "?", str(e)))

    kept_codex = []
    for conn in existing_codex:
        email = (conn.get("email") or conn.get("name") or "").lower().strip()
        if email not in incoming_keys:
            kept_codex.append(conn)

    # Preserve existing order and append only genuinely new accounts at the end.
    replacements = {
        (conn.get("email") or conn.get("name") or "").lower().strip(): conn
        for conn in incoming
    }
    merged_codex = []
    used = set()
    for old in existing_codex:
        key = (old.get("email") or old.get("name") or "").lower().strip()
        if key in replacements:
            merged_codex.append(replacements[key])
            used.add(key)
        else:
            merged_codex.append(old)
    for conn in incoming:
        key = (conn.get("email") or conn.get("name") or "").lower().strip()
        if key not in used and key not in existing_map:
            merged_codex.append(conn)
            used.add(key)
    for idx, conn in enumerate(merged_codex, start=1):
        conn["priority"] = idx

    data["providerConnections"] = other_conns + merged_codex
    save_db_json(json_path, data)
    return inserted, replaced, errors


def import_connections_json(connections):
    total_inserted = 0
    total_replaced = 0
    errors = []
    if not JSON_PATHS:
        return 0, 0, ["No JSON db found. Candidates: {}".format("; ".join(get_json_candidates()))]
    for json_path in JSON_PATHS:
        ins, rep, errs = import_connections_json_path(connections, json_path)
        total_inserted += ins
        total_replaced += rep
        errors.extend(errs)
    return total_inserted, total_replaced, errors


def import_connections(connections):
    """Write all 9router stores as one serialized local operation."""
    with _storage_lock:
        if not storage_exists():
            return 0, 0, [
                "No 9router/n9router database found",
                "9router SQLite: {}".format(SQLITE_PATH),
                "JSON candidates: {}".format("; ".join(get_json_candidates())),
            ]

        total_inserted = 0
        total_replaced = 0
        errors = []
        if sqlite_exists():
            ins, rep, errs = import_connections_sqlite(connections)
            total_inserted += ins
            total_replaced += rep
            errors.extend(errs)
        if json_exists():
            ins, rep, errs = import_connections_json(connections)
            total_inserted += ins
            total_replaced += rep
            errors.extend(errs)
        return total_inserted, total_replaced, errors


def verify_sqlite_emails(connections):
    """Return True only when every incoming email exists in live 9router SQLite."""
    with _storage_lock:
        if not sqlite_exists():
            return False, [], ["9router SQLite not found: {}".format(SQLITE_PATH)]
        expected = sorted(set(
            (conn.get("email") or conn.get("name") or "").lower().strip()
            for conn in connections
            if (conn.get("email") or conn.get("name") or "").strip()
        ))
        if not expected:
            return False, [], ["No email available for SQLite verification"]
        db = sqlite3.connect(SQLITE_PATH, timeout=30)
        try:
            placeholders = ",".join("?" for _ in expected)
            rows = db.execute(
                "SELECT lower(trim(email)) FROM providerConnections "
                "WHERE provider=? AND lower(trim(email)) IN ({})".format(placeholders),
                [PROVIDER] + expected,
            ).fetchall()
            found = sorted(set(row[0] for row in rows if row[0]))
        finally:
            db.close()
        missing = sorted(set(expected) - set(found))
        errors = ["Email not found in SQLite after commit: {}".format(x) for x in missing]
        return not missing, found, errors


def delete_connection(conn_id):
    deleted = False
    if sqlite_exists():
        backup_file(SQLITE_PATH, "9router-sqlite", "sqlite")
        db = sqlite3.connect(SQLITE_PATH)
        cur = db.cursor()
        cur.execute("DELETE FROM providerConnections WHERE id=? AND provider=?", (conn_id, PROVIDER))
        deleted = cur.rowcount > 0 or deleted
        db.commit()
        db.close()
    if json_exists():
        for json_path in JSON_PATHS:
            data = load_db_json(json_path)
            backup_file(json_path, "json-db", "json")
            before = len(data.get("providerConnections", []))
            data["providerConnections"] = [
                c for c in data.get("providerConnections", [])
                if not (c.get("id") == conn_id and c.get("provider") == PROVIDER)
            ]
            if len(data.get("providerConnections", [])) != before:
                pri = 1
                for conn in data["providerConnections"]:
                    if conn.get("provider") == PROVIDER:
                        conn["priority"] = pri
                        pri += 1
                save_db_json(json_path, data)
                deleted = True
    return deleted


# =============================================================================
# OAuth PKCE Flow
# =============================================================================

def generate_pkce():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_authorize_url():
    """Build OAuth authorize URL with PKCE."""
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    _oauth_pending[state] = {"code_verifier": code_verifier, "created_at": time.time()}
    
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
    }
    return OAUTH_AUTHORIZE_URL + "?" + urlencode(params), state


def exchange_code_for_tokens(code, state):
    """Exchange authorization code for tokens."""
    pending = _oauth_pending.pop(state, None)
    if not pending:
        return None, "Invalid or expired state"
    
    body = json.dumps({
        "grant_type": "authorization_code",
        "client_id": OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": pending["code_verifier"],
    }).encode("utf-8")
    
    req = Request(OAUTH_TOKEN_URL, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    
    try:
        resp = urlopen(req, timeout=30)
        tokens = json.loads(resp.read().decode("utf-8"))
        return tokens, None
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return None, "Token exchange failed ({}): {}".format(e.code, err_body)
    except Exception as e:
        return None, "Token exchange error: {}".format(str(e))


def tokens_to_connection(tokens):
    """Convert OAuth token response to a 9router connection dict."""
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    id_token = tokens.get("id_token", "")
    expires_in = tokens.get("expires_in", 864000)
    
    # Decode JWT to get email and account info
    email = ""
    account_id = ""
    plan_type = ""
    try:
        parts = access_token.split(".")
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        prof = decoded.get("https://api.openai.com/profile", {})
        auth = decoded.get("https://api.openai.com/auth", {})
        email = prof.get("email", "")
        account_id = auth.get("chatgpt_account_id", "")
        plan_type = auth.get("chatgpt_plan_type", "")
    except:
        pass
    
    now = now_iso()
    expires_at = datetime.fromtimestamp(
        time.time() + expires_in, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    
    return {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "idToken": id_token or "",
        "expiresAt": expires_at,
        "expiresIn": expires_in,
        "testStatus": "active",
        "lastUsedAt": now,
        "consecutiveUseCount": 0,
        "backoffLevel": 0,
        "providerSpecificData": {
            "chatgptAccountId": account_id,
            "chatgptPlanType": plan_type,
        },
        "lastError": None,
        "lastErrorAt": None,
        "email": email,
        "name": email,
        "provider": PROVIDER,
        "authType": "oauth",
    }


# OAuth callback result storage
_oauth_result = {"status": "idle"}  # idle | waiting | success | error


class OAuthCallbackHandler(SimpleHTTPRequestHandler):
    """Handles the OAuth callback on port 1455."""
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        global _oauth_result
        parsed = urlparse(self.path)
        if parsed.path == "/auth/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]
            error = params.get("error", [None])[0]
            
            if error:
                _oauth_result = {"status": "error", "error": error}
                self._send_html("<h2>Login failed</h2><p>{}</p><p>You can close this tab.</p>".format(error))
                return
            
            if not code or not state:
                _oauth_result = {"status": "error", "error": "Missing code or state"}
                self._send_html("<h2>Error</h2><p>Missing code or state parameter.</p>")
                return
            
            # Exchange code for tokens
            tokens, err = exchange_code_for_tokens(code, state)
            if err:
                _oauth_result = {"status": "error", "error": err}
                self._send_html("<h2>Token exchange failed</h2><p>{}</p>".format(err))
                return
            # Convert to connection and import
            conn = tokens_to_connection(tokens)
            email = conn.get("email", "Unknown")
            
            try:
                ins, rep, errs = import_connections([conn])
                _oauth_result = {
                    "status": "success",
                    "email": email,
                    "plan": conn.get("providerSpecificData", {}).get("chatgptPlanType", ""),
                    "hasRefresh": bool(conn.get("refreshToken")),
                    "inserted": ins,
                    "replaced": rep,
                }
                self._send_html(
                    '<h2 style="color:#22c55e">Login successful!</h2>'
                    '<p><strong>{}</strong> — {}</p>'
                    '<p>Refresh token: {}</p>'
                    '<p>Imported into 9router. You can close this tab.</p>'
                    '<script>setTimeout(()=>window.close(),3000)</script>'.format(
                        email,
                        conn.get("providerSpecificData", {}).get("chatgptPlanType", "free"),
                        "Yes" if conn.get("refreshToken") else "No",
                    )
                )
            except Exception as e:
                _oauth_result = {"status": "error", "error": str(e)}
                self._send_html("<h2>Import failed</h2><p>{}</p>".format(str(e)))
        else:
            self.send_response(404)
            self.end_headers()
    
    def _send_html(self, body):
        html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>9router OAuth</title>'
        html += '<style>body{font-family:system-ui;background:#1a1a2e;color:#e0e0e0;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}'
        html += '.card{background:#16213e;padding:40px;border-radius:16px;text-align:center;max-width:500px;box-shadow:0 8px 32px rgba(0,0,0,.3)}</style>'
        html += '</head><body><div class="card">{}</div></body></html>'.format(body)
        content = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)


def start_oauth_callback_server():
    """Start the OAuth callback server on port 1455 in a background thread."""
    try:
        server = HTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), OAuthCallbackHandler)
        server.timeout = 120  # 2 minute timeout
        # Handle one request then stop
        def serve():
            server.handle_request()
            server.server_close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        return True
    except OSError as e:
        print("  [!] Cannot start callback server on port {}: {}".format(OAUTH_CALLBACK_PORT, e))
        return False


# =============================================================================
# Auto-Login Worker (runs auto_login.py via subprocess)
# =============================================================================

_auto_login_status = {
    "running": False,
    "total": 0,
    "current": 0,
    "currentEmail": "",
    "done": 0,
    "failed": 0,
    "results": [],
    "stopped": False,
    "pid": None,
    "mode": "",
    "workersRequested": 3,
    "workersRunning": 0,
    "activeAccounts": [],
}
_auto_login_proc = None
_auto_login_lock = threading.Lock()


def _auto_login_worker(accounts, headed=True, workers=3):
    """Background worker that runs the parallel auto_login subprocess."""
    global _auto_login_status, _auto_login_proc
    import subprocess
    try:
        workers = int(workers)
    except (TypeError, ValueError):
        workers = 3
    workers = max(1, workers)
    active_workers = min(workers, len(accounts))
    tmp_file = os.path.join(SCRIPT_DIR, "_tmp_accounts.txt")
    with open(tmp_file, "w", encoding="utf-8") as output:
        for account in accounts:
            output.write("{}\n".format(account))
    _auto_login_status = {
        "running": True,
        "total": len(accounts),
        "current": 0,
        "currentEmail": "",
        "done": 0,
        "failed": 0,
        "results": [],
        "stopped": False,
        "pid": None,
        "mode": "headed" if headed else "headless",
        "workersRequested": workers,
        "workersRunning": active_workers,
        "activeAccounts": [],
    }
    result_by_email = {}

    # Run auto_login.py
    script_path = os.path.join(SCRIPT_DIR, "auto_login.py")
    if not os.path.exists(script_path):
        _auto_login_status["running"] = False
        _auto_login_status["results"].append({"email": "?", "status": "error", "error": "auto_login.py not found"})
        return

    args = [sys.executable, script_path, tmp_file, "--workers", str(workers)]
    if headed:
        args.append("--headed")

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            cwd=SCRIPT_DIR,
            env=env,
        )
        with _auto_login_lock:
            _auto_login_proc = proc
            _auto_login_status["pid"] = proc.pid
        print("  [auto] Mode: {} | PID: {}".format("headed" if headed else "headless", proc.pid))
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            print("  [auto] {}".format(line))

            # Deterministic events are safe even when subprocess worker logs interleave.
            if line.startswith("EVENT|"):
                parts = line.split("|")
                kind = parts[1] if len(parts) > 1 else ""
                email = parts[2].strip() if len(parts) > 2 else ""
                metadata = {}
                for item in parts[3:]:
                    if "=" in item:
                        key, value = item.split("=", 1)
                        metadata[key] = value
                active = _auto_login_status["activeAccounts"]
                if kind == "START" and email:
                    if email not in active:
                        active.append(email)
                    _auto_login_status["currentEmail"] = email
                    _auto_login_status["current"] = min(_auto_login_status["total"], len(active) + _auto_login_status["done"] + _auto_login_status["failed"])
                elif kind in ("SUCCESS", "ERROR") and email:
                    if email in active:
                        active.remove(email)
                    if result_by_email.get(email) not in ("success", "error"):
                        status = "success" if kind == "SUCCESS" else "error"
                        result_by_email[email] = status
                        if status == "success":
                            _auto_login_status["done"] += 1
                        else:
                            _auto_login_status["failed"] += 1
                        _auto_login_status["results"].append({
                            "email": email,
                            "status": status,
                            "plan": metadata.get("plan", ""),
                            "hasRefresh": metadata.get("refresh") == "yes",
                            "imported": status == "success",
                            "error": metadata.get("error", ""),
                        })
                continue

            # Compatibility parsing for any legacy progress line.
            if line.startswith("[") and "/" in line[:10] and "]" in line[:10]:
                # [1/3] email@...
                try:
                    bracket = line.split("]")[0].replace("[", "")
                    cur, total = bracket.split("/")
                    email = line.split("]")[1].strip()
                    _auto_login_status["current"] = int(cur)
                    _auto_login_status["currentEmail"] = email
                except:
                    pass

            # OAuth token acquisition is only an intermediate state. Keep its
            # metadata, but do not increment success until SQLite is verified.
            if line.startswith("✅ ") and "|" in line and "plan=" in line:
                parts = line.split("|")
                email = parts[0].replace("✅", "").strip()
                meta = result_by_email.get(email) if isinstance(result_by_email.get(email), dict) else {}
                for part in parts:
                    if "plan=" in part:
                        meta["plan"] = part.split("=", 1)[1].strip()
                    if "rt=yes" in part:
                        meta["hasRefresh"] = True
                result_by_email[email] = meta

            if line.startswith("IMPORT_OK|"):
                parts = line.split("|", 4)
                email = parts[1].strip() if len(parts) > 1 else ""
                plan = parts[2].strip() if len(parts) > 2 else ""
                has_rt = len(parts) > 3 and parts[3].strip() == "yes"
                if email and result_by_email.get(email) != "success":
                    result_by_email[email] = "success"
                    _auto_login_status["done"] += 1
                    _auto_login_status["results"].append({
                        "email": email,
                        "status": "success",
                        "plan": plan,
                        "hasRefresh": has_rt,
                        "imported": True,
                    })

            if line.startswith("IMPORT_FAIL|"):
                parts = line.split("|", 2)
                email = parts[1].strip() if len(parts) > 1 else (_auto_login_status.get("currentEmail") or "?")
                error = parts[2].strip() if len(parts) > 2 else "Unknown 9router import error"
                if email and result_by_email.get(email) != "error":
                    result_by_email[email] = "error"
                    _auto_login_status["failed"] += 1
                    _auto_login_status["results"].append({
                        "email": email,
                        "status": "error",
                        "error": "OAuth OK, import 9router failed: {}".format(error),
                        "imported": False,
                    })

            if line.startswith("❌ ") and "|" not in line:
                err_email = _auto_login_status.get("currentEmail", "?")
                err_msg = line.replace("❌", "").strip()
                if err_email and result_by_email.get(err_email) not in ("success", "error"):
                    result_by_email[err_email] = "error"
                    _auto_login_status["failed"] += 1
                    _auto_login_status["results"].append({
                        "email": err_email,
                        "status": "error",
                        "error": err_msg,
                    })

        proc.wait()
    except Exception as e:
        _auto_login_status["results"].append({"email": "?", "status": "error", "error": str(e)})
    finally:
        with _auto_login_lock:
            _auto_login_proc = None
            _auto_login_status["pid"] = None
        _auto_login_status["running"] = False
        # Cleanup temp file
        try:
            os.remove(tmp_file)
        except:
            pass


def start_auto_login(accounts, headed=True, workers=3):
    """Start auto-login in a background thread with a user-selected worker count."""
    if _auto_login_status.get("running"):
        return
    thread = threading.Thread(target=_auto_login_worker, args=(accounts, headed, workers), daemon=True)
    thread.start()


def _kill_pid(pid):
    """Force kill a single PID on Windows."""
    if not pid:
        return False
    try:
        import subprocess
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        return True
    except Exception:
        return False


def _kill_pid_tree(pid):
    """Force kill a PID and all its children."""
    if not pid:
        return False
    try:
        import subprocess
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        return True
    except Exception:
        return False


def _kill_playwright_browsers():
    """Kill Chrome/Chromium spawned by Playwright. Safe: only kills processes
    whose command line contains --disable-blink-features=AutomationControlled
    which is unique to our tool, never present in normal Chrome."""
    if os.name != "nt":
        return 0
    import subprocess
    import tempfile
    killed = 0
    marker = "--disable-blink-features=AutomationControlled"

    # Method 1: Write a temp .ps1 script to avoid quoting hell
    ps_script = os.path.join(SCRIPT_DIR, "_kill_browsers.ps1")
    try:
        ps_code = """$marker = "{marker}"
$procs = Get-CimInstance Win32_Process | Where-Object {{
  ($_.Name -like "chrome*" -or $_.Name -like "chromium*") -and
  $_.CommandLine -and
  $_.CommandLine.Contains($marker)
}}
$count = 0
foreach ($p in $procs) {{
  try {{
    Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    $count++
    Write-Host "KILLED:$($p.ProcessId)"
  }} catch {{}}
}}
Write-Host "TOTAL:$count"
""".format(marker=marker)
        with open(ps_script, "w", encoding="utf-8") as f:
            f.write(ps_code)

        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("KILLED:"):
                pid_str = line.split(":")[1]
                print("  [stop] Killed browser PID {}".format(pid_str))
                killed += 1
            elif line.startswith("TOTAL:"):
                total = int(line.split(":")[1])
                if total > killed:
                    killed = total
    except Exception as e:
        print("  [stop] PS script error: {}".format(e))
    finally:
        try:
            os.remove(ps_script)
        except Exception:
            pass

    # Method 2 fallback: wmic direct terminate
    if killed == 0:
        try:
            wmic_filter = "Name like 'chrome%' and CommandLine like '%{}%'".format(marker)
            result = subprocess.run(
                ["wmic", "process", "where", wmic_filter, "call", "terminate"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
            out = result.stdout or ""
            if "ReturnValue = 0" in out:
                killed += out.count("ReturnValue = 0")
                print("  [stop] wmic terminated {} browser process(es)".format(killed))
        except Exception as e:
            print("  [stop] wmic fallback error: {}".format(e))

    return killed


def stop_auto_login():
    """Force-stop every auto-login worker, browser, and active UI connection."""
    global _auto_login_proc
    killed = False
    pid = None
    proc = None
    with _auto_login_lock:
        proc = _auto_login_proc
        if proc:
            pid = proc.pid

    # Kill the complete Python -> Playwright -> Chrome process tree first.
    # This must happen before updating UI state so Stop All means nothing keeps
    # trying to log in or holding an OAuth callback connection in the background.
    if pid:
        killed = _kill_pid_tree(pid) or killed
        try:
            if proc:
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
                killed = True
            except Exception as error:
                print("  [stop] subprocess cleanup failed: {}".format(error))

    # A browser can occasionally outlive its Playwright parent, especially if a
    # callback/CAPTCHA is open. Sweep twice to catch children that detach during
    # the process-tree shutdown.
    killed_browsers = _kill_playwright_browsers()
    time.sleep(0.35)
    killed_browsers += _kill_playwright_browsers()

    _auto_login_status["running"] = False
    _auto_login_status["stopped"] = True
    _auto_login_status["current"] = 0
    _auto_login_status["currentEmail"] = ""
    _auto_login_status["workersRunning"] = 0
    _auto_login_status["activeAccounts"] = []
    _auto_login_status["killedBrowsers"] = killed_browsers
    _auto_login_status["results"].append({
        "email": "?",
        "status": "stopped",
        "error": "Stopped by user — all active workers disconnected",
    })
    with _auto_login_lock:
        _auto_login_proc = None
        _auto_login_status["pid"] = None
    print("  [stop] All workers disconnected. process={}, browsers={}".format(killed, killed_browsers))
    return killed or killed_browsers > 0


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def log_message(self, fmt, *args):
        print("  [HTTP] {} {}".format(self.command, self.path))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/health":
            self._json({
                "ok": True,
                "storage": "multi",
                "sqlite": sqlite_exists(),
                "sqlitePath": SQLITE_PATH or "",
                "json": json_exists(),
                "jsonPaths": JSON_PATHS,
                "jsonCandidates": get_json_candidates(),
                "path": JSON_PATHS[0] if JSON_PATHS else (SQLITE_PATH or ""),
            })
            return
        if p == "/api/connections":
            c = get_connections()
            self._json({"connections": c, "count": len(c)})
            return
        if p == "/api/oauth/status":
            self._json(_oauth_result)
            return
        if p == "/api/oauth/auto-status":
            self._json(_auto_login_status)
            return
        if p == "/" or p == "/index.html":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/import":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                conns = data.get("connections", [])
                if not conns:
                    self._json({"error": "No connections"}, 400)
                    return
                # Keep import and verification inside one storage critical section.
                with _storage_lock:
                    ins, rep, errs = import_connections(conns)
                    verified, verified_emails, verify_errors = verify_sqlite_emails(conns)
                all_errors = errs + verify_errors
                response = {
                    "inserted": ins,
                    "replaced": rep,
                    "errors": all_errors,
                    "total": ins + rep,
                    "sqliteVerified": verified,
                    "verifiedEmails": verified_emails,
                    "sqlitePath": SQLITE_PATH,
                }
                self._json(response, 200 if verified else 500)
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if p == "/api/oauth/start":
            global _oauth_result
            _oauth_result = {"status": "waiting"}
            # Start callback server
            if not start_oauth_callback_server():
                _oauth_result = {"status": "error", "error": "Port {} in use".format(OAUTH_CALLBACK_PORT)}
                self._json({"error": "Cannot start callback server"}, 500)
                return
            # Build authorize URL and open browser
            auth_url, state = build_authorize_url()
            webbrowser.open(auth_url)
            self._json({"ok": True, "authUrl": auth_url, "state": state})
            return
        if p == "/api/oauth/auto-login":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                accounts = data.get("accounts", [])
                if not accounts:
                    self._json({"error": "No accounts"}, 400)
                    return
                try:
                    workers = int(data.get("workers", 3))
                except (TypeError, ValueError):
                    self._json({"error": "Workers must be a positive integer"}, 400)
                    return
                if workers < 1:
                    self._json({"error": "Workers must be a positive integer"}, 400)
                    return
                # Always show Chromium for reliable interactive login/captcha handling.
                headed = True
                start_auto_login(accounts, headed=headed, workers=workers)
                self._json({
                    "ok": True,
                    "total": len(accounts),
                    "mode": "headed",
                    "workersRequested": workers,
                    "workersRunning": min(workers, len(accounts)),
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if p == "/api/oauth/auto-stop":
            killed = stop_auto_login()
            self._json({"ok": True, "killed": killed})
            return
        self._json({"error": "Not found"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.startswith("/api/connections/"):
            cid = p.split("/")[-1]
            self._json({"deleted": delete_connection(cid)})
            return
        self._json({"error": "Not found"}, 404)


def main():
    print("=" * 50)
    print("  9router Import Tool")
    print("=" * 50)
    if storage_exists():
        print("  9router SQLite:  {}".format(SQLITE_PATH if sqlite_exists() else "not found"))
        if JSON_PATHS:
            print("  JSON databases:")
            for p in JSON_PATHS:
                print("    - {}".format(p))
        else:
            print("  JSON databases: not found")
        print("  Import target:   all detected databases")
    else:
        print("  [!] No 9router/n9router database found!")
        print("  9router SQLite:  {}".format(SQLITE_PATH))
        print("  JSON candidates:")
        for p in get_json_candidates():
            print("    - {}".format(p))
        print("  Install/run 9router or n9router first, then restart this tool.")
    print("  URL: http://localhost:{}".format(PORT))
    print("=" * 50)
    print("  Press Ctrl+C to stop")
    print()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def open_browser():
        import time
        time.sleep(0.8)
        webbrowser.open("http://localhost:{}".format(PORT))
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
