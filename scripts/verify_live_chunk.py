"""End-to-end smoke test for the live-chunk path on 127.0.0.1:8010.

Runs entirely against the HTTP API (no DB introspection beyond what the
endpoints return). It:
  1. Logs in as the default admin.
  2. Creates a fresh case.
  3. Uploads several real recorded webm files to /inference/live-chunk in
     sequence, simulating rolling cumulative snapshots.
  4. Prints every per-chunk result + the authoritative live transcript
     returned by the backend.
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8010/api/v1"
USERNAME = "verify_live_chunk"
EMAIL = "verify_live_chunk@test.com"
PASSWORD = "VerifyPass123!"


def login_or_register() -> str:
    resp = requests.post(
        f"{BASE}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=20,
    )
    if resp.status_code == 401:
        reg = requests.post(
            f"{BASE}/auth/register",
            json={
                "username": USERNAME,
                "email": EMAIL,
                "full_name": "Verify Live Chunk",
                "password": PASSWORD,
                "role": "dispatcher",
            },
            timeout=20,
        )
        reg.raise_for_status()
        resp = requests.post(
            f"{BASE}/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=20,
        )
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_case(token: str) -> int:
    resp = requests.post(
        f"{BASE}/cases",
        headers={"Authorization": f"Bearer {token}"},
        json={"location_text": "verify-live-chunk", "notes": "smoke test"},
        timeout=20,
    )
    resp.raise_for_status()
    return int(resp.json()["id"])


def live_chunk(token: str, case_id: int, path: Path) -> dict:
    with path.open("rb") as f:
        files = {"file": (path.name, f, "audio/webm")}
        data = {"case_id": str(case_id)}
        resp = requests.post(
            f"{BASE}/inference/live-chunk",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files=files,
            timeout=120,
        )
    if resp.status_code >= 400:
        print(f"  !! {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json()


def main() -> int:
    recordings_dir = Path("/mnt/c/Users/user/Desktop/SAREI/data/recordings")
    candidates = sorted(recordings_dir.rglob("*.webm"))[:4]
    if not candidates:
        print("No webm fixtures found under data/recordings")
        return 2

    token = login_or_register()
    case_id = create_case(token)
    print(f"[verify] case_id={case_id}")

    final_live_text = ""
    for path in candidates:
        size = path.stat().st_size
        print(f"[verify] uploading {path.name} ({size} bytes)")
        result = live_chunk(token, case_id, path)
        text = (result.get("text") or "").strip()
        live = (result.get("live_transcript_text") or "").strip()
        print(f"  chunk_text={text!r}")
        print(f"  live_transcript_text={live!r}")
        final_live_text = live

    print()
    print(f"[verify] FINAL live transcript: {final_live_text!r}")
    return 0 if final_live_text else 1


if __name__ == "__main__":
    sys.exit(main())
