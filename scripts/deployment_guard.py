"""Refuse production startup from a mutable container image reference."""

from __future__ import annotations

import os
import re
import sys


IMMUTABLE_IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def require_immutable_image(value: str | None) -> str:
    image = (value or "").strip()
    if not IMMUTABLE_IMAGE_RE.fullmatch(image):
        raise RuntimeError(
            "BIBITASKS_DEPLOYMENT_IMAGE must be an immutable image reference "
            "ending in @sha256:<64 lowercase hex characters>"
        )
    return image


def main() -> None:
    require_immutable_image(os.environ.get("BIBITASKS_DEPLOYMENT_IMAGE"))
    command = sys.argv[1:]
    if not command:
        raise RuntimeError("deployment guard requires a command to execute")
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
