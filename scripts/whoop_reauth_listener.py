#!/usr/bin/env python3
"""One-shot local WHOOP OAuth callback listener.

Prints an auth URL, waits for WHOOP to redirect to localhost, exchanges the
code, saves the token through whoop_monitor.py, and exits.
"""

from __future__ import annotations

import http.server
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import whoop_monitor


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    ok = False

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (query.get("code") or [""])[0]
        if not code:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing WHOOP authorization code.")
            return

        proc = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("whoop_monitor.py")), "exchange-code", code],
            text=True,
            capture_output=True,
            check=False,
        )
        CallbackHandler.ok = proc.returncode == 0
        self.send_response(200 if CallbackHandler.ok else 500)
        self.end_headers()
        if CallbackHandler.ok:
            self.wfile.write(b"WHOOP token saved. You can close this tab.")
        else:
            self.wfile.write(b"WHOOP token exchange failed. Return to Codex for details.")


def main() -> int:
    cfg = whoop_monitor.load_config()
    print(whoop_monitor.auth_url(cfg), flush=True)
    with http.server.HTTPServer(("127.0.0.1", 8888), CallbackHandler) as server:
        server.timeout = 1
        end_at = time.time() + 300
        while time.time() < end_at and not CallbackHandler.ok:
            server.handle_request()
    print("CALLBACK_OK" if CallbackHandler.ok else "CALLBACK_TIMEOUT", flush=True)
    return 0 if CallbackHandler.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
