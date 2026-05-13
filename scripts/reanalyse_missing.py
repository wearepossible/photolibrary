"""One-off: re-run AI analysis on records with missing metadata.

Useful when a previous sync added records but their AI analysis failed
(e.g. Anthropic credits exhausted partway through). This script finds
records whose keywords/description/alt_text are empty, downloads their
thumbnail from R2 (no Drive round-trip needed), runs Claude on it, and
writes the updated data.json back to R2.

Usage:
    python scripts/reanalyse_missing.py --dry-run           # list candidates
    python scripts/reanalyse_missing.py                     # process all
    python scripts/reanalyse_missing.py --limit 50          # process up to 50

Environment variables: same as sync.py.
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

from sync import (
    PARALLEL_WORKERS,
    analyse_image,
    fetch_data_json,
    get_env,
    get_r2_client,
    upload_campaigns_json,
    upload_data_json,
)


def is_missing_metadata(record):
    """A record needs re-analysis if its AI-generated fields are empty."""
    if not record.get("keywords"):
        return True
    if not (record.get("description") or "").strip():
        return True
    if not (record.get("alt_text") or "").strip():
        return True
    return False


def fetch_thumbnail_bytes(r2, bucket, slug):
    res = r2.get_object(Bucket=bucket, Key=f"thumbnails/{slug}.jpg")
    return res["Body"].read()


def reanalyse_one(r2, bucket, ai_client, record):
    """Download thumbnail from R2, run Claude, return updated record fields."""
    try:
        thumb_bytes = fetch_thumbnail_bytes(r2, bucket, record["id"])
    except Exception as e:
        print(f"    {record['id']}: THUMB FETCH FAILED ({e})", flush=True)
        return None

    result = analyse_image(ai_client, thumb_bytes)
    if result:
        kw = len(result.get("keywords", []))
        print(f"    {record['id']}: OK ({kw} keywords)", flush=True)
    else:
        print(f"    {record['id']}: AI FAILED", flush=True)
    return result


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    limit = None
    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            limit = int(args[idx + 1])

    r2 = get_r2_client()
    bucket = get_env("R2_BUCKET_NAME")

    api_key = get_env("ANTHROPIC_API_KEY")
    ai_client = anthropic.Anthropic(api_key=api_key)

    print("Fetching data.json from R2...", flush=True)
    records = fetch_data_json(r2, bucket)
    print(f"  {len(records)} total records", flush=True)

    candidates = [r for r in records if is_missing_metadata(r)]
    print(f"  {len(candidates)} records missing AI metadata", flush=True)

    if limit is not None and len(candidates) > limit:
        print(f"  Limiting to {limit} per --limit flag", flush=True)
        candidates = candidates[:limit]

    if not candidates:
        print("\nNothing to do.", flush=True)
        return

    if dry_run:
        print("\nFirst 20 candidates:", flush=True)
        for r in candidates[:20]:
            print(f"  - {r['id']} ({r.get('original_filename', '')})", flush=True)
        if len(candidates) > 20:
            print(f"  ... and {len(candidates) - 20} more", flush=True)
        print("\nDry run — no changes made.", flush=True)
        return

    print(f"\nRe-analysing {len(candidates)} records with {PARALLEL_WORKERS} workers...", flush=True)
    started = time.time()
    updated = 0
    failed = 0

    # Build an id -> record map so we can update in place efficiently.
    by_id = {r["id"]: r for r in records}

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
        futures = {
            pool.submit(reanalyse_one, r2, bucket, ai_client, r): r["id"]
            for r in candidates
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            rec_id = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                print(f"    {rec_id}: worker error: {e}", flush=True)
                result = None

            if result:
                target = by_id[rec_id]
                if result.get("keywords"):
                    target["keywords"] = result["keywords"]
                if result.get("description"):
                    target["description"] = result["description"]
                if result.get("alt_text"):
                    target["alt_text"] = result["alt_text"]
                updated += 1
            else:
                failed += 1

            if i % 25 == 0:
                print(f"  Progress: {i}/{len(candidates)} ({updated} updated, {failed} failed)", flush=True)

    print(f"\nUploading updated data.json ({len(records)} records)...", flush=True)
    upload_data_json(r2, bucket, records)
    upload_campaigns_json(r2, bucket, records)

    duration = int(time.time() - started)
    print(f"\n{'='*60}", flush=True)
    print("RE-ANALYSIS COMPLETE", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Candidates: {len(candidates)}", flush=True)
    print(f"  Updated: {updated}", flush=True)
    print(f"  Failed: {failed}", flush=True)
    print(f"  Duration: {duration}s", flush=True)


if __name__ == "__main__":
    main()
