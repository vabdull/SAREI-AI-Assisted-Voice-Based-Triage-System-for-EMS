#!/usr/bin/env python3
"""Filter NeMo manifests to keep only selected Arabic dialects.

Reads dialect labels from the SADA metadata TSVs and copies matching
entries from the base manifests into a new directory.  Text is passed
through unchanged because prepare_manifests.py already uses cleaned_text.

Usage:
    python scripts/filter_dialect.py
    python scripts/filter_dialect.py --dialect Najdi --dialect Hijazi --dialect Khaleeji
"""

import argparse
import json
from collections import Counter
from pathlib import Path


DIALECT_ALIASES = {
    "najdi": "najdi",
    "hijazi": "hijazi",
    "hejazi": "hijazi",
    "khaleeji": "khaleeji",
    "khaliji": "khaleeji",
    "gulf": "khaleeji",
}


def parse_args():
    p = argparse.ArgumentParser(description="Filter manifests by dialect")
    p.add_argument("--dialect", action="append", default=[],
                   help="Dialect to keep (repeatable or comma-separated). "
                        "Default: Najdi, Hijazi, Khaleeji.")
    p.add_argument("--data-dir", type=str, default="data")
    p.add_argument("--manifest-dir", type=str, default="manifests")
    p.add_argument("--output-dir", type=str, default="manifests_ems_dialects")
    return p.parse_args()


def norm_dialect(value: str) -> str:
    return DIALECT_ALIASES.get(value.strip().lower(), value.strip().lower())


def resolve_targets(values: list[str]) -> set[str]:
    if not values:
        return {"najdi", "hijazi", "khaleeji"}
    out: set[str] = set()
    for v in values:
        for item in v.split(","):
            item = item.strip().lower()
            if item:
                out.add(DIALECT_ALIASES.get(item, item))
    return out


def load_dialect_index(data_dir: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for split in ["train", "validation", "test"]:
        tsv = data_dir / split / "metadata.tsv"
        if not tsv.exists():
            continue
        with open(tsv, "r", encoding="utf-8-sig") as f:
            header = f.readline().strip().split("\t")
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) != len(header):
                    continue
                row = dict(zip(header, parts))
                ap = row.get("audio_filepath", "")
                d = norm_dialect(row.get("dialect", "unknown"))
                index[ap] = d
                index[Path(ap).name] = d
    return index


def filter_manifest(src: Path, dst: Path, dialect_index: dict,
                    targets: set[str]) -> dict:
    stats = {"total": 0, "kept": 0, "duration_h": 0.0, "dialects": Counter()}

    with open(src, "r", encoding="utf-8") as f_in, \
         open(dst, "w", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            stats["total"] += 1
            entry = json.loads(line)
            ap = entry["audio_filepath"]
            dialect = dialect_index.get(ap) or dialect_index.get(
                Path(ap).name, "unknown")

            if dialect in targets:
                f_out.write(json.dumps(entry, ensure_ascii=False) + "\n")
                stats["kept"] += 1
                stats["duration_h"] += entry.get("duration", 0) / 3600
                stats["dialects"][dialect] += 1

    return stats


def main():
    args = parse_args()
    data_dir = Path(args.data_dir)
    manifest_dir = Path(args.manifest_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = resolve_targets(args.dialect)
    dialect_index = load_dialect_index(data_dir)

    print(f"Filtering for dialects: {', '.join(sorted(targets))}")
    print(f"  Source   : {manifest_dir.resolve()}")
    print(f"  Output   : {out_dir.resolve()}")
    print(f"  Index    : {len(dialect_index)} entries\n")

    for split in ["train", "validation", "test"]:
        src = manifest_dir / f"{split}_manifest.json"
        if not src.exists():
            print(f"  [SKIP] {src}")
            continue
        dst = out_dir / f"{split}_manifest.json"
        s = filter_manifest(src, dst, dialect_index, targets)
        print(f"  {split:12s}: {s['kept']:>7,} / {s['total']:>7,} "
              f"({s['duration_h']:.1f}h)")
        for d, c in s["dialects"].most_common():
            print(f"    {d:20s}: {c:>7,}")

    print(f"\nDone. Output: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
