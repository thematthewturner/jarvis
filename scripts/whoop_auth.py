"""
Standalone WHOOP OAuth2 authorization script.
No dependencies beyond stdlib — run with system python3.

Usage:
    python3 scripts/whoop_auth.py
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# Load .env manually (no python-dotenv needed)
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

CLIENT_ID     = os.getenv("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("WHOOP_REDIRECT_URI", "http://localhost:8888/callback")
AUTH_URL      = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL     = "https://api.prod.whoop.com/oauth/oauth2/token"
TOKENS_PATH   = Path(__file__).parent.parent / "data" / "whoop_tokens.json"
SCOPE         = "offline read:recovery read:sleep read:workout read:body_measurement read:cycles read:profile"

if not CLIENT_ID or not CLIENT_SECRET:
    print("ERROR: Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in .env")
    sys.exit(1)

# PKCE — required by WHOOP
code_verifier  = secrets.token_urlsafe(64)
code_challenge = base64.urlsafe_b64encode(
    hashlib.sha256(code_verifier.encode()).digest()
).rstrip(b"=").decode()

state = secrets.token_urlsafe(16)
params = {
    "client_id":             CLIENT_ID,
    "redirect_uri":          REDIRECT_URI,
    "response_type":         "code",
    "scope":                 SCOPE,
    "state":                 state,
    "code_challenge":        code_challenge,
    "code_challenge_method": "S256",
}
auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
print(f"\nOpening browser for WHOOP authorization...")
print(f"URL: {auth_url}\n")
webbrowser.open(auth_url)

code_holder = {}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        code_holder["code"] = qs.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")

    def log_message(self, *args):
        pass

port = int(urllib.parse.urlparse(REDIRECT_URI).port or 8888)
server = http.server.HTTPServer(("", port), Handler)
print(f"Waiting for callback on port {port} (timeout: 120s)...")
t = threading.Thread(target=server.handle_request)
t.start()
t.join(timeout=120)

code = code_holder.get("code")
if not code:
    print("ERROR: No code received — timed out or redirect URI mismatch.")
    sys.exit(1)

print(f"Got auth code: {code[:20]}...")
print(f"Exchanging for tokens (with PKCE)...")
data = urllib.parse.urlencode({
    "grant_type":    "authorization_code",
    "code":          code,
    "redirect_uri":  REDIRECT_URI,
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code_verifier": code_verifier,
}).encode()

req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
req.add_header("Content-Type", "application/x-www-form-urlencoded")
req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
req.add_header("Accept", "application/json")
try:
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"ERROR {e.code}: {body}")
    sys.exit(1)

tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
TOKENS_PATH.parent.mkdir(exist_ok=True)
TOKENS_PATH.write_text(json.dumps(tokens, indent=2))

print(f"\nSuccess! Tokens saved to {TOKENS_PATH}")
print(f"Access token expires in {tokens.get('expires_in', '?')}s")
print(f"\nNext step: scp the tokens to the server:")
print(f"  scp \"{TOKENS_PATH}\" jarvis@138.197.70.215:/home/jarvis/jarvis/data/whoop_tokens.json")
