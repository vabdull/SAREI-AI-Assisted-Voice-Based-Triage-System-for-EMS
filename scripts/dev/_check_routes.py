"""Hit /openapi.json on the live backend and list routes that match a
substring. Cheap way to confirm a hot-reloaded backend has picked up a
new endpoint.
"""

from __future__ import annotations

import json
import sys
import urllib.request


def main(substrings: list[str], base: str = "http://localhost:8011") -> int:
    url = f"{base}/openapi.json"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            doc = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        print(f"[check_routes] cannot reach {url}: {exc}", file=sys.stderr)
        return 1

    paths = doc.get("paths", {})
    needle = [s.lower() for s in substrings] or [""]
    matches = sorted(
        p for p in paths if any(s in p.lower() for s in needle)
    )
    if not matches:
        print("[check_routes] no matching routes")
        return 2
    for p in matches:
        methods = sorted(m.upper() for m in paths[p] if m in {"get", "post", "patch", "put", "delete"})
        print(f"  {' '.join(methods):<20} {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
