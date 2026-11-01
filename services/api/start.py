
"""Initialize a persistent API token before starting STARK OS."""

import os
import secrets
import sys
from pathlib import Path

TOKEN_FILE = Path("/app/data/api-token")


def main():
    token = os.environ.get("STARK_API_TOKEN", "").strip()

    if not token:
        if TOKEN_FILE.exists():
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(32)

            # Exclusive creation prevents overwriting an existing token.
            fd = os.open(
                TOKEN_FILE,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(token + "\n")

            print("Generated a new persistent STARK API token.")

    if len(token) < 24:
        raise RuntimeError("STARK_API_TOKEN must be at least 24 characters")

    os.environ["STARK_API_TOKEN"] = token

    os.execv(
        sys.executable,
        [sys.executable, "-m", "stark.main"],
    )


if __name__ == "__main__":
    main()
