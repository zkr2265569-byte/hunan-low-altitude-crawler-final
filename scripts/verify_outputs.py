# -*- coding: utf-8 -*-
"""Verify crawler outputs for CI/cloud runs."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REQUIRED_FILES = [
    "hunan_low_altitude_fulltext.txt",
    "articles.csv",
    "articles.jsonl",
    "links_raw.csv",
    "crawl.log",
]
REQUIRED_FIELDS = {"title", "publish_time", "source", "url", "body"}


def fail(message: str) -> bool:
    print(f"[verify][ERROR] {message}")
    return False


def check_required_files(output_dir: Path) -> bool:
    ok = True
    for name in REQUIRED_FILES:
        path = output_dir / name
        if not path.exists():
            ok = fail(f"Missing required file: {name}") and ok
        else:
            print(f"[verify][OK] {name}: {path.stat().st_size} bytes")
    return ok


def check_articles_csv(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = set(reader.fieldnames or [])
            missing_fields = sorted(REQUIRED_FIELDS - fieldnames)
            if missing_fields:
                return fail(f"articles.csv missing fields: {', '.join(missing_fields)}")

            row_count = 0
            for _ in reader:
                row_count += 1
            if row_count == 0:
                return fail("articles.csv has no data rows.")

            print(f"[verify][OK] articles.csv rows: {row_count}")
            return True
    except Exception as exc:
        return fail(f"Failed to read articles.csv: {exc}")


def check_articles_jsonl(path: Path) -> bool:
    found_record = False
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                found_record = True
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    return fail(f"articles.jsonl invalid JSON at line {line_no}: {exc}")

                if not isinstance(obj, dict):
                    return fail(f"articles.jsonl line {line_no} is not an object.")

                missing_fields = sorted(REQUIRED_FIELDS - set(obj.keys()))
                if missing_fields:
                    return fail(
                        "articles.jsonl line "
                        f"{line_no} missing fields: {', '.join(missing_fields)}"
                    )
    except Exception as exc:
        return fail(f"Failed to read articles.jsonl: {exc}")

    if not found_record:
        return fail("articles.jsonl has no records.")

    print("[verify][OK] articles.jsonl fields verified.")
    return True


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output")
    print(f"[verify] output_dir={output_dir.resolve()}")

    if not output_dir.exists() or not output_dir.is_dir():
        fail("output directory does not exist.")
        return 1

    ok = True
    ok = check_required_files(output_dir) and ok

    csv_path = output_dir / "articles.csv"
    jsonl_path = output_dir / "articles.jsonl"
    if csv_path.exists():
        ok = check_articles_csv(csv_path) and ok
    if jsonl_path.exists():
        ok = check_articles_jsonl(jsonl_path) and ok

    if not ok:
        print("[verify] output verification failed.")
        return 1

    print("[verify] output verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
