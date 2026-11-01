
"""Initialize a persistent API token before starting STARK OS."""

import os
import secrets
import sys
import time
from pathlib import Path

TOKEN_FILE = Path("/app/data/api-token")


def read_token() -> str:
    """Read a token, allowing a concurrent writer to finish."""
    for _ in range(50):
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
        except FileNotFoundError:
            pass
        time.sleep(0.1)

    raise RuntimeError("Could not read the persistent STARK API token")


def get_api_token() -> str:
    configured = os.environ.get("STARK_API_TOKEN", "").strip()

    if configured:
        return configured

    try:
        fd = os.open(
            TOKEN_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return read_token()

    token = secrets.token_urlsafe(32)

    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(token + "\n")
        file.flush()
        os.fsync(file.fileno())

    print("Generated a persistent STARK API token.")
    return token


def main() -> None:
    token = get_api_token()

    if len(token) < 24:
        raise RuntimeError("STARK_API_TOKEN must be at least 24 characters")

    os.environ["STARK_API_TOKEN"] = token

    os.execv(
        sys.executable,
        [sys.executable, "-m", "stark.main"],
    )


if __name__ == "__main__":
    main()
