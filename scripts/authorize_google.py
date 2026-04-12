#!/usr/bin/env python3
"""
One-time OAuth2 authorization for Jarvis Google integrations.

Run this LOCALLY on your Mac (not on the server) — it opens a browser for sign-in.
After completion, copy both files to the server.

Usage:
    cd ~/projects/jarvis
    pip install google-auth-oauthlib google-api-python-client
    python scripts/authorize_google.py

Then copy credentials to server:
    scp google_credentials.json google_token.json jarvis@138.197.70.215:/home/jarvis/jarvis/
"""
import os
import sys

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
]

CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "google_token.json")


def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing dependency. Run: pip install google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: {CREDENTIALS_FILE} not found.")
        print()
        print("Steps to get it:")
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. Create a project (or select existing)")
        print("  3. Enable these APIs:")
        print("       APIs & Services → Library → search and enable:")
        print("       - Google Calendar API")
        print("       - Gmail API")
        print("       - Google Drive API")
        print("  4. APIs & Services → Credentials → Create Credentials → OAuth client ID")
        print("  5. Application type: Desktop app")
        print("  6. Download JSON → save as google_credentials.json in the jarvis folder")
        print("  7. Also: APIs & Services → OAuth consent screen → add your email as Test user")
        sys.exit(1)

    print("Opening browser for Google authorization...")
    print("Sign in with your Google account and approve all requested permissions.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    if os.name != "nt":
        os.chmod(TOKEN_FILE, 0o600)

    print(f"\nAuthorization successful!")
    print(f"Token saved to: {TOKEN_FILE}")
    print()
    print("Now copy both files to the server:")
    print(f"  scp {CREDENTIALS_FILE} {TOKEN_FILE} jarvis@138.197.70.215:/home/jarvis/jarvis/")
    print()
    print("Then on the server, add to .env:")
    print("  GOOGLE_CREDENTIALS_FILE=google_credentials.json")
    print("  GOOGLE_TOKEN_FILE=google_token.json")


if __name__ == "__main__":
    main()
