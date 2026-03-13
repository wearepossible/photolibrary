"""Phase 2: Download images from Google Drive, generate thumbnails, build data.json.

Reads the scan report and review decisions to:
1. Build merged records (exact dupes → locations, perceptual dupes → alternatives)
2. Download the "best" copy of each unique image
3. Generate 400px-wide JPEG thumbnails
4. Build the initial data.json structure

Usage:
    python scripts/download_and_process.py

Inputs:
    data/drive_scan_report.json
    data/review_decisions.json

Outputs:
    data/downloads/           — full-resolution images (temporary, for AI analysis)
    data/thumbnails/          — 400px-wide JPEG thumbnails (to upload to R2)
    data/data.json            — initial data.json (before AI analysis)
    data/campaigns.json       — list of inferred campaign names
"""

import io
import json
import time
from collections import defaultdict
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import pillow_heif
from PIL import Image

# Register HEIC/HEIF support with Pillow
pillow_heif.register_heif_opener()

from utils import (
    format_file_size,
    get_env,
    infer_campaign_from_path,
    slugify,
    unique_slug,
)

DATA_DIR = Path(__file__).parent.parent / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
THUMBNAILS_DIR = DATA_DIR / "thumbnails"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

THUMBNAIL_WIDTH = 400
THUMBNAIL_QUALITY = 75


def build_drive_service():
    key_path = get_env("GOOGLE_SERVICE_ACCOUNT_KEY")
    creds = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds)


def load_inputs():
    """Load scan report and review decisions."""
    with open(DATA_DIR / "drive_scan_report.json") as f:
        scan = json.load(f)

    with open(DATA_DIR / "review_decisions.json") as f:
        decisions = json.load(f)

    return scan, decisions


def build_file_index(files: list[dict]) -> dict[str, dict]:
    """Build a lookup from file ID to file record."""
    return {f["id"]: f for f in files}


def build_records(files: list[dict], decisions: dict) -> list[dict]:
    """Build the data.json records from scan data + review decisions.

    Returns a list of photo records, each with locations and alternatives.
    """
    file_index = build_file_index(files)
    excluded_ids = set(decisions.get("exclude_files", []))

    # Track which file IDs have been assigned to a record
    assigned_ids = set()

    # Build merge groups: file_id → group of file_ids it belongs to
    merge_groups = []
    for group in decisions.get("merge_groups", []):
        group_file_ids = [fid for fid in group["file_ids"] if fid not in excluded_ids and fid in file_index]
        if group_file_ids:
            merge_groups.append({
                "type": group["type"],
                "file_ids": group_file_ids,
            })

    # Split groups: each file becomes its own record
    split_file_ids = set()
    for group in decisions.get("split_groups", []):
        for fid in group["file_ids"]:
            if fid not in excluded_ids and fid in file_index:
                split_file_ids.add(fid)

    records = []
    used_slugs = set()

    # Process merge groups
    for group in merge_groups:
        fids = group["file_ids"]
        group_files = [file_index[fid] for fid in fids if fid in file_index]
        if not group_files:
            continue

        for fid in fids:
            assigned_ids.add(fid)

        if group["type"] == "exact":
            # Exact dupes: same image in different locations
            # Sort by quality (largest file first)
            group_files.sort(key=lambda f: (f.get("size", 0), f.get("width") or 0), reverse=True)
            best = group_files[0]

            locations = []
            for f in group_files:
                locations.append({
                    "drive_file_id": f["id"],
                    "folder_path": f.get("folder_path", ""),
                    "folder_id": f.get("parent_id", ""),
                    "file_size_bytes": f.get("size", 0),
                    "width": f.get("width"),
                    "height": f.get("height"),
                })

            slug = unique_slug(slugify(best["name"]), used_slugs)
            used_slugs.add(slug)
            ext = Path(best["name"]).suffix.lower() or ".jpg"

            record = _make_record(
                best=best,
                slug=slug,
                ext=ext,
                locations=locations,
                alternatives=[],
                group_files=group_files,
            )
            records.append(record)

        else:
            # Perceptual or filename dupes: alternatives (different but similar photos)
            group_files.sort(key=lambda f: (f.get("size", 0), f.get("width") or 0), reverse=True)
            best = group_files[0]
            alt_files = group_files[1:]

            slug = unique_slug(slugify(best["name"]), used_slugs)
            used_slugs.add(slug)
            ext = Path(best["name"]).suffix.lower() or ".jpg"

            # Primary gets a single location
            locations = [{
                "drive_file_id": best["id"],
                "folder_path": best.get("folder_path", ""),
                "folder_id": best.get("parent_id", ""),
                "file_size_bytes": best.get("size", 0),
                "width": best.get("width"),
                "height": best.get("height"),
            }]

            # Others become alternatives
            alternatives = []
            for af in alt_files:
                alt_slug = unique_slug(slugify(af["name"]), used_slugs)
                used_slugs.add(alt_slug)
                alt_ext = Path(af["name"]).suffix.lower() or ".jpg"
                alternatives.append({
                    "drive_file_id": af["id"],
                    "filename": f"{alt_slug}{alt_ext}",
                    "original_filename": af["name"],
                    "thumbnail_url": "",  # filled later
                    "drive_file_url": f"https://drive.google.com/file/d/{af['id']}/view",
                    "folder_path": af.get("folder_path", ""),
                    "width": af.get("width"),
                    "height": af.get("height"),
                    "file_size_bytes": af.get("size", 0),
                    "_slug": alt_slug,
                    "_ext": alt_ext,
                })

            record = _make_record(
                best=best,
                slug=slug,
                ext=ext,
                locations=locations,
                alternatives=alternatives,
                group_files=group_files,
            )
            records.append(record)

    # Process standalone files (not in any merge/split group and not excluded)
    for f in files:
        if f["id"] in assigned_ids or f["id"] in excluded_ids:
            continue

        assigned_ids.add(f["id"])

        slug = unique_slug(slugify(f["name"]), used_slugs)
        used_slugs.add(slug)
        ext = Path(f["name"]).suffix.lower() or ".jpg"

        locations = [{
            "drive_file_id": f["id"],
            "folder_path": f.get("folder_path", ""),
            "folder_id": f.get("parent_id", ""),
            "file_size_bytes": f.get("size", 0),
            "width": f.get("width"),
            "height": f.get("height"),
        }]

        record = _make_record(
            best=f,
            slug=slug,
            ext=ext,
            locations=locations,
            alternatives=[],
            group_files=[f],
        )
        records.append(record)

    return records


def _make_record(best, slug, ext, locations, alternatives, group_files):
    """Create a single data.json record."""
    # Collect all campaigns from all files in the group
    campaigns = []
    for f in group_files:
        c = infer_campaign_from_path(f.get("folder_path", ""))
        if c and c not in campaigns:
            campaigns.append(c)

    return {
        "id": slug,
        "filename": f"{slug}{ext}",
        "original_filename": best["name"],
        "thumbnail_url": "",  # filled after upload to R2
        "drive_file_url": f"https://drive.google.com/file/d/{best['id']}/view",
        "drive_folder_url": f"https://drive.google.com/drive/folders/{best.get('parent_id', '')}",
        "locations": locations,
        "alternatives": alternatives,
        "md5": best.get("md5", ""),
        "keywords": [],
        "description": "",
        "alt_text": "",
        "campaign": campaigns[0] if campaigns else "",
        "credit": "",
        "date_taken": "",
        "date_added": time.strftime("%Y-%m-%d"),
        "added_by": "",
        # Internal fields for processing (removed before final output)
        "_best_file_id": best["id"],
        "_slug": slug,
        "_ext": ext,
    }


def download_file(service, file_id: str, dest_path: Path, mime_type: str = "") -> bool:
    """Download a file from Google Drive."""
    if dest_path.exists():
        return True

    try:
        # For Google-native formats, we'd need to export, but images are regular files
        request = service.files().get_media(
            fileId=file_id,
            supportsAllDrives=True,
        )
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    except Exception as e:
        print(f"  ERROR downloading {file_id}: {e}", flush=True)
        return False


def generate_thumbnail(source_path: Path, thumb_path: Path) -> bool:
    """Generate a 400px-wide JPEG thumbnail."""
    if thumb_path.exists():
        return True

    try:
        img = Image.open(source_path)
        img = img.convert("RGB")

        # Resize to THUMBNAIL_WIDTH, maintaining aspect ratio
        ratio = THUMBNAIL_WIDTH / img.width
        new_height = int(img.height * ratio)
        img = img.resize((THUMBNAIL_WIDTH, new_height), Image.LANCZOS)

        img.save(thumb_path, "JPEG", quality=THUMBNAIL_QUALITY)
        return True
    except Exception as e:
        print(f"  ERROR generating thumbnail for {source_path.name}: {e}", flush=True)
        return False


def main():
    scan, decisions = load_inputs()
    files = scan["files"]
    print(f"Loaded {len(files)} files from scan report", flush=True)

    # Build records
    print("Building records from review decisions...", flush=True)
    records = build_records(files, decisions)
    print(f"  Created {len(records)} unique photo records", flush=True)

    # Count alternatives
    total_alts = sum(len(r.get("alternatives", [])) for r in records)
    print(f"  Total alternatives: {total_alts}", flush=True)

    # Count total locations
    total_locs = sum(len(r.get("locations", [])) for r in records)
    print(f"  Total locations: {total_locs}", flush=True)

    # Collect all file IDs we need to download (primary + alternatives)
    download_tasks = []
    for record in records:
        # Primary image
        download_tasks.append({
            "file_id": record["_best_file_id"],
            "slug": record["_slug"],
            "ext": record["_ext"],
        })
        # Alternative images
        for alt in record.get("alternatives", []):
            download_tasks.append({
                "file_id": alt["drive_file_id"],
                "slug": alt["_slug"],
                "ext": alt["_ext"],
            })

    print(f"\nDownloading {len(download_tasks)} images...", flush=True)
    service = build_drive_service()

    success = 0
    failed = 0
    skipped = 0

    for i, task in enumerate(download_tasks):
        dest = DOWNLOADS_DIR / f"{task['slug']}{task['ext']}"
        thumb = THUMBNAILS_DIR / f"{task['slug']}.jpg"

        if dest.exists() and thumb.exists():
            skipped += 1
            continue

        if (i - skipped) % 20 == 0:
            print(f"  {i+1}/{len(download_tasks)} (downloaded: {success}, skipped: {skipped}, failed: {failed})...", flush=True)

        # Download
        if not dest.exists():
            ok = download_file(service, task["file_id"], dest)
            if not ok:
                failed += 1
                continue

            # Rate limiting
            if (i - skipped) % 50 == 49:
                time.sleep(1)

        # Generate thumbnail
        if not thumb.exists():
            ok = generate_thumbnail(dest, thumb)
            if not ok:
                failed += 1
                continue

        success += 1

    print(f"\nDownload complete: {success} succeeded, {skipped} skipped, {failed} failed", flush=True)

    # Build campaigns list
    campaigns = set()
    for record in records:
        if record.get("campaign"):
            campaigns.add(record["campaign"])
    campaigns = sorted(campaigns)

    # Clean up internal fields before saving
    for record in records:
        record.pop("_best_file_id", None)
        record.pop("_slug", None)
        record.pop("_ext", None)
        for alt in record.get("alternatives", []):
            alt.pop("_slug", None)
            alt.pop("_ext", None)

    # Save data.json
    data_path = DATA_DIR / "data.json"
    with open(data_path, "w") as f:
        json.dump(records, f, indent=2)
    print(f"\nSaved {len(records)} records to {data_path}", flush=True)

    # Save campaigns.json
    campaigns_path = DATA_DIR / "campaigns.json"
    with open(campaigns_path, "w") as f:
        json.dump(campaigns, f, indent=2)
    print(f"Saved {len(campaigns)} campaigns to {campaigns_path}", flush=True)

    # Summary
    print(f"\n{'='*60}")
    print("PROCESSING SUMMARY")
    print(f"{'='*60}")
    print(f"Unique photos: {len(records)}")
    print(f"With alternatives: {sum(1 for r in records if r.get('alternatives'))}")
    print(f"With multiple locations: {sum(1 for r in records if len(r.get('locations', [])) > 1)}")
    print(f"Campaigns: {len(campaigns)}")
    for c in campaigns:
        count = sum(1 for r in records if r.get("campaign") == c)
        print(f"  {c}: {count}")
    no_campaign = sum(1 for r in records if not r.get("campaign"))
    if no_campaign:
        print(f"  (no campaign): {no_campaign}")
    print(f"\nNext step: python scripts/analyse_photos.py")


if __name__ == "__main__":
    main()
