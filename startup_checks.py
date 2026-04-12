import logging
import os
from pathlib import Path

logger = logging.getLogger("jarvis.startup")


def validate_required_env(required: list[str], scope: str = "runtime"):
    missing = [key for key in required if not os.getenv(key, "").strip()]
    if missing:
        raise RuntimeError(f"Missing required {scope} environment variables: {', '.join(missing)}")


def enforce_file_mode_600(path: str):
    if os.name == "nt" or not os.path.exists(path):
        return
    mode = os.stat(path).st_mode & 0o777
    if mode != 0o600:
        os.chmod(path, 0o600)
        logger.warning("Adjusted permissions on %s to 0600 (was %o)", path, mode)


def secure_token_files(project_root: str | None = None):
    root = Path(project_root or Path(__file__).resolve().parent)
    token_files = [
        root / "google_token.json",
        root / "data" / "whoop_tokens.json",
    ]
    for token_path in token_files:
        enforce_file_mode_600(str(token_path))
