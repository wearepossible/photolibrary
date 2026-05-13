"""One-off: delete thumbnails in R2 that aren't referenced by data.json.

Orphan thumbnails accumulate when a sync run uploads a thumbnail but dies
before data.json gets updated (e.g. the workflow times out). This script
also runs at the end of every sync, but use it standalone to back-fill
existing orphans or to inspect with --dry-run.

Usage:
    python scripts/cleanup_orphan_thumbnails.py             # delete orphans
    python scripts/cleanup_orphan_thumbnails.py --dry-run   # list only

Environment variables: same as sync.py.
"""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

from sync import (
    cleanup_orphan_thumbnails,
    fetch_data_json,
    get_env,
    get_r2_client,
)


def main():
    dry_run = "--dry-run" in sys.argv

    r2 = get_r2_client()
    bucket = get_env("R2_BUCKET_NAME")

    print("Fetching data.json from R2...", flush=True)
    records = fetch_data_json(r2, bucket)
    print(f"  {len(records)} records.", flush=True)

    print("\nChecking for orphan thumbnails...", flush=True)
    count = cleanup_orphan_thumbnails(r2, bucket, records, dry_run=dry_run)

    if dry_run:
        print(f"\nDry run — {count} orphans would be deleted.", flush=True)
    else:
        print(f"\nDone. {count} orphans removed.", flush=True)


if __name__ == "__main__":
    main()
