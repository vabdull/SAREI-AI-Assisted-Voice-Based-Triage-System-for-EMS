"""Cross-platform path helpers for storage paths.

Storage paths are persisted in the database and need to survive when the
backend moves between Windows and WSL (where the same drive is visible at
both ``C:\\...`` and ``/mnt/c/...``). The helpers in this module convert
between the two representations and normalize new writes to a POSIX form
that is readable from either OS.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from backend.core.config import ROOT_DIR

_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def _running_on_posix() -> bool:
    return os.name == "posix"


def to_posix_storage_path(raw: str | os.PathLike[str]) -> str:
    """Return a POSIX-style absolute path suitable for DB storage.

    When running on Linux/WSL, a ``C:\\...`` path is translated to
    ``/mnt/c/...``. When running on Windows, WSL mount paths are kept as-is
    because Windows Python can still read them via the WSL filesystem bridge.
    """
    text = str(raw).strip()
    if not text:
        return text

    match = _WINDOWS_DRIVE_RE.match(text)
    if match and _running_on_posix():
        drive, remainder = match.group(1).lower(), match.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{remainder}"

    # Replace backslashes with forward slashes for consistency
    return text.replace("\\", "/")


def resolve_existing_storage_path(raw: str | os.PathLike[str]) -> Path:
    """Return a ``Path`` pointing at the stored file on the current OS.

    Tries candidate representations in order until one exists on disk:
    1. The raw path as-is.
    2. POSIX-normalized form (``C:\\...`` -> ``/mnt/c/...``).
    3. ``/mnt/<drive>/...`` expanded to `<drive>:\\...` when on Windows.
    4. Relative to the project ``ROOT_DIR`` (matching the last
       ``data/recordings/...`` segment).
    """

    candidates: list[Path] = []
    text = str(raw)

    candidates.append(Path(text))
    candidates.append(Path(to_posix_storage_path(text)))

    if not _running_on_posix():
        wsl_match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", text.replace("\\", "/"))
        if wsl_match:
            drive, remainder = wsl_match.group(1).upper(), wsl_match.group(2)
            candidates.append(Path(f"{drive}:/{remainder}"))

    marker = "data/recordings/"
    lowered = text.replace("\\", "/").lower()
    idx = lowered.find(marker)
    if idx >= 0:
        relative = text.replace("\\", "/")[idx:]
        candidates.append(Path(ROOT_DIR) / relative)

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue

    # No candidate resolved. Return the POSIX form so callers get a sensible
    # error message when they try to read it.
    return Path(to_posix_storage_path(text))
