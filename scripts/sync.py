"""Sync: scan Google Drive for new/removed files and update R2.

Designed to run both locally and in GitHub Actions. For new files:
downloads, generates thumbnails, runs AI analysis, uploads to R2.
For removed files: cleans up records and thumbnails from R2.

Usage:
    python scripts/sync.py              # full sync
    python scripts/sync.py --dry-run    # report only, no changes

Environment variables (see .env or GitHub Actions secrets):
    GOOGLE_SERVICE_ACCOUNT_KEY or GOOGLE_SERVICE_ACCOUNT_KEY_JSON
    SHARED_DRIVE_ID
    ANTHROPIC_API_KEY
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    R2_BUCKET_NAME, R2_JURISDICTION, R2_PUBLIC_URL
"""

import base64
import io
import json
import os
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import boto3
from botocore.config import Config
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from PIL import Image, ImageOps

# Allow running from repo root or scripts dir
SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))
from utils import get_env, infer_campaign_from_path, slugify, unique_slug

THUMBNAIL_WIDTH = 400
THUMBNAIL_QUALITY = 75

# Number of new photos to process in parallel. Bounded by Anthropic rate
# limits (Haiku tier) and Drive download bandwidth. 5 is a safe default that
# gives a ~5x speedup over serial without tripping rate limits.
PARALLEL_WORKERS = 5

IMAGE_MIMES = {
    "image/jpeg", "image/png", "image/gif",
    "image/bmp", "image/tiff", "image/webp", "image/heic", "image/heif",
}

ANALYSIS_PROMPT = """Analyse this photo for a climate charity's photo library.

Return a JSON object with exactly these fields:
- "keywords": an array of 10-20 descriptive keywords/tags. Include objects, settings, activities, weather, mood, and environmental context. Be specific to climate/environment topics where relevant (e.g. "solar panels" not just "panels", "cycling infrastructure" not just "road").
- "description": 2-3 sentences describing the photo in detail. Mention what's happening, the setting, and any notable details. Write plainly and specifically — describe what you actually see, not what you infer.
- "alt_text": accessible alt text for screen readers, 1 sentence, concise but descriptive.

Style rules for description and alt_text:
- Do NOT use these overused words/phrases: diverse, vibrant, bustling, lush, verdant, nestled, picturesque, serene, captivating, striking, dedicated, passionate, hands-on, collaborative effort, environmental stewardship, showcasing, highlighting, symbolizing, fostering, embodying, evoking, exuding, underscoring, conveying, a sense of, community spirit, multigenerational, multicultural, multifaceted
- Avoid hedging like "appears to be", "suggesting", "hinting at", "what appears to be", "can be seen". Just state what you see.
- Avoid "engaged in [activity]" — just name the activity directly.
- Avoid "participating in" — say what people are doing.
- Don't narrate the photo as a photo ("this image captures", "the photo shows"). Just describe the scene.
- Be concrete and specific, not abstract or editorialising.

Return ONLY the JSON object, no other text."""


# --- Service clients ---

def get_drive_service():
    """Build Google Drive API client.

    Supports both file-path key (GOOGLE_SERVICE_ACCOUNT_KEY) for local use
    and inline JSON (GOOGLE_SERVICE_ACCOUNT_KEY_JSON) for CI.
    """
    key_json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY_JSON")
    if key_json_str:
        import json as _json
        info = _json.loads(key_json_str)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
    else:
        key_path = get_env("GOOGLE_SERVICE_ACCOUNT_KEY")
        if not Path(key_path).exists() and Path(key_path + ".json").exists():
            key_path = key_path + ".json"
        creds = service_account.Credentials.from_service_account_file(
            key_path, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
    return build("drive", "v3", credentials=creds)


def get_r2_client():
    account_id = get_env("R2_ACCOUNT_ID")
    jurisdiction = os.getenv("R2_JURISDICTION", "")
    if jurisdiction:
        endpoint = f"https://{account_id}.{jurisdiction}.r2.cloudflarestorage.com"
    else:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=get_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=get_env("R2_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def get_r2_public_url():
    url = os.getenv("R2_PUBLIC_URL")
    if url:
        return url.rstrip("/")
    return f"https://pub-{get_env('R2_ACCOUNT_ID')}.r2.dev"


# --- Drive scanning ---

def scan_drive(drive, drive_id):
    """Scan the shared Drive and return all image files with folder paths."""
    print("Scanning Google Drive...", flush=True)

    # First build a folder ID → name/parent map for path resolution
    folders = {}
    page_token = None
    while True:
        res = drive.files().list(
            q="mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            driveId=drive_id,
            corpora="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="nextPageToken, files(id, name, parents)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        for f in res.get("files", []):
            folders[f["id"]] = {
                "name": f["name"],
                "parent": (f.get("parents") or [None])[0],
            }
        page_token = res.get("nextPageToken")
        if not page_token:
            break

    def resolve_path(folder_id):
        parts = []
        seen = set()
        fid = folder_id
        while fid and fid in folders and fid not in seen:
            seen.add(fid)
            parts.append(folders[fid]["name"])
            fid = folders[fid]["parent"]
        return "/".join(reversed(parts)) if parts else ""

    # Now scan image files
    files = []
    page_token = None
    while True:
        res = drive.files().list(
            q="trashed = false",
            driveId=drive_id,
            corpora="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="nextPageToken, files(id, name, mimeType, md5Checksum, size, parents, imageMediaMetadata)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()

        for f in res.get("files", []):
            if f.get("mimeType") in IMAGE_MIMES:
                parent_id = (f.get("parents") or [""])[0]
                files.append({
                    "id": f["id"],
                    "name": f["name"],
                    "mimeType": f.get("mimeType", ""),
                    "md5": f.get("md5Checksum", ""),
                    "size": int(f.get("size", 0)),
                    "parent_id": parent_id,
                    "folder_path": resolve_path(parent_id),
                    "width": (f.get("imageMediaMetadata") or {}).get("width"),
                    "height": (f.get("imageMediaMetadata") or {}).get("height"),
                })

        page_token = res.get("nextPageToken")
        if not page_token:
            break

    print(f"  Found {len(files)} image files on Drive", flush=True)
    return files


# --- R2 data access ---

def fetch_data_json(r2, bucket):
    """Fetch current data.json from R2."""
    try:
        res = r2.get_object(Bucket=bucket, Key="data.json")
        body = res["Body"].read().decode("utf-8")
        return json.loads(body)
    except r2.exceptions.NoSuchKey:
        return []


def upload_data_json(r2, bucket, records):
    """Upload updated data.json to R2."""
    body = json.dumps(records, indent=2)
    r2.put_object(
        Bucket=bucket, Key="data.json",
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )


def upload_campaigns_json(r2, bucket, records):
    """Rebuild and upload campaigns.json to R2."""
    campaigns = sorted({r["campaign"] for r in records if r.get("campaign")})
    r2.put_object(
        Bucket=bucket, Key="campaigns.json",
        Body=json.dumps(campaigns, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


# --- Processing new files ---

def download_from_drive(drive, file_id):
    """Download a file from Drive, return bytes."""
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf.read()


def generate_thumbnail_bytes(image_bytes):
    """Generate a 400px-wide JPEG thumbnail, return bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    ratio = THUMBNAIL_WIDTH / img.width
    new_height = int(img.height * ratio)
    img = img.resize((THUMBNAIL_WIDTH, new_height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=THUMBNAIL_QUALITY)
    return buf.getvalue()


def analyse_image(client, thumb_bytes):
    """Send thumbnail to Claude for keyword/description generation."""
    image_b64 = base64.standard_b64encode(thumb_bytes).decode("utf-8")
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": ANALYSIS_PROMPT},
                ],
            }],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"    Failed to parse AI response: {e}", flush=True)
        return None
    except anthropic.RateLimitError:
        print("    Rate limited, waiting 60s...", flush=True)
        time.sleep(60)
        return analyse_image(client, thumb_bytes)
    except anthropic.APIError as e:
        print(f"    API error: {e}", flush=True)
        return None


def process_new_file(file_info, drive, r2, bucket, public_url, ai_client, slug):
    """Process a single new file: download, thumbnail, AI analysis, upload.

    The caller pre-allocates ``slug`` to keep this function free of shared
    mutable state so it can be run in parallel.
    """
    name = file_info["name"]
    try:
        image_bytes = download_from_drive(drive, file_info["id"])
    except Exception as e:
        print(f"    {name}: DOWNLOAD FAILED ({e})", flush=True)
        return None

    try:
        thumb_bytes = generate_thumbnail_bytes(image_bytes)
    except Exception as e:
        print(f"    {name}: THUMB FAILED ({e})", flush=True)
        return None

    thumb_key = f"thumbnails/{slug}.jpg"
    r2.put_object(
        Bucket=bucket, Key=thumb_key,
        Body=thumb_bytes, ContentType="image/jpeg",
    )
    thumb_url = f"{public_url}/{thumb_key}"

    ai_result = None
    if ai_client:
        ai_result = analyse_image(ai_client, thumb_bytes)
        if ai_result:
            print(f"    {name}: OK ({len(ai_result.get('keywords', []))} keywords)", flush=True)
        else:
            print(f"    {name}: AI failed, added without metadata", flush=True)
    else:
        print(f"    {name}: OK (no AI)", flush=True)

    # Build record
    campaign = infer_campaign_from_path(file_info.get("folder_path", ""))
    record = {
        "id": slug,
        "filename": f"{slug}.jpg",
        "original_filename": file_info["name"],
        "thumbnail_url": thumb_url,
        "drive_file_url": f"https://drive.google.com/file/d/{file_info['id']}/view",
        "drive_folder_url": f"https://drive.google.com/drive/folders/{file_info.get('parent_id', '')}",
        "locations": [{
            "drive_file_id": file_info["id"],
            "folder_path": file_info.get("folder_path", ""),
            "folder_id": file_info.get("parent_id", ""),
            "file_size_bytes": file_info.get("size", 0),
            "width": file_info.get("width"),
            "height": file_info.get("height"),
        }],
        "alternatives": [],
        "md5": file_info.get("md5", ""),
        "keywords": (ai_result or {}).get("keywords", []),
        "description": (ai_result or {}).get("description", ""),
        "alt_text": (ai_result or {}).get("alt_text", ""),
        "campaign": campaign or "",
        "credit": "",
        "date_taken": "",
        "date_added": time.strftime("%Y-%m-%d"),
        "added_by": "",
    }

    return record


# --- Sync status (consumed by the UI status icon) ---

def write_sync_status(r2, bucket, status):
    """Write the sync result summary to R2 for the UI to read."""
    try:
        r2.put_object(
            Bucket=bucket, Key="sync-status.json",
            Body=json.dumps(status, indent=2).encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-cache",
        )
    except Exception as e:
        print(f"  Warning: failed to write sync-status.json: {e}", flush=True)


# --- Orphan thumbnail cleanup ---

def list_r2_thumbnails(r2, bucket):
    """List every key under the thumbnails/ prefix in R2."""
    keys = []
    paginator = r2.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="thumbnails/"):
        for obj in page.get("Contents", []) or []:
            keys.append(obj["Key"])
    return keys


def referenced_thumbnail_keys(records):
    """Build the set of thumbnail R2 keys that are referenced by data.json."""
    referenced = set()
    for r in records:
        referenced.add(f"thumbnails/{r['id']}.jpg")
        for alt in r.get("alternatives", []):
            alt_filename = alt.get("filename", "")
            if alt_filename:
                stem = Path(alt_filename).stem
                if stem:
                    referenced.add(f"thumbnails/{stem}.jpg")
    return referenced


def cleanup_orphan_thumbnails(r2, bucket, records, dry_run=False):
    """Delete thumbnails in R2 that aren't referenced by any record."""
    all_keys = set(list_r2_thumbnails(r2, bucket))
    referenced = referenced_thumbnail_keys(records)
    orphans = sorted(all_keys - referenced)

    if not orphans:
        print("  No orphan thumbnails found.", flush=True)
        return 0

    print(f"  Found {len(orphans)} orphan thumbnails.", flush=True)
    if dry_run:
        for k in orphans[:20]:
            print(f"    would delete: {k}", flush=True)
        if len(orphans) > 20:
            print(f"    ... and {len(orphans) - 20} more", flush=True)
        return len(orphans)

    # S3 delete_objects takes up to 1000 keys per call
    deleted = 0
    for i in range(0, len(orphans), 1000):
        batch = orphans[i:i + 1000]
        r2.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch]},
        )
        deleted += len(batch)
    print(f"  Deleted {deleted} orphan thumbnails.", flush=True)
    return deleted


# --- Main sync ---

def main():
    dry_run = "--dry-run" in sys.argv

    started_at = datetime.now(timezone.utc)
    status = {
        "started_at": started_at.isoformat(),
        "completed_at": None,
        "duration_seconds": None,
        "status": "running",
        "added": 0,
        "removed": 0,
        "failed": 0,
        "orphans_cleaned": 0,
        "total_records": None,
        "error": None,
    }

    drive = get_drive_service()
    drive_id = get_env("SHARED_DRIVE_ID")
    r2 = get_r2_client()
    bucket = get_env("R2_BUCKET_NAME")
    public_url = get_r2_public_url()

    # Scan Drive
    drive_files = scan_drive(drive, drive_id)
    drive_file_ids = {f["id"] for f in drive_files}
    drive_md5s = {f["md5"] for f in drive_files if f.get("md5")}

    # Fetch current data
    print("Fetching data.json from R2...", flush=True)
    records = fetch_data_json(r2, bucket)
    print(f"  {len(records)} existing records", flush=True)

    # Build set of known file IDs
    known_file_ids = set()
    known_md5s = set()
    for r in records:
        for loc in r.get("locations", []):
            known_file_ids.add(loc["drive_file_id"])
        for alt in r.get("alternatives", []):
            known_file_ids.add(alt["drive_file_id"])
        if r.get("md5"):
            known_md5s.add(r["md5"])

    # Find new files (on Drive but not in data.json, and not an MD5 duplicate)
    new_files = []
    for f in drive_files:
        if f["id"] not in known_file_ids:
            # Check if this is an exact duplicate by MD5
            if f.get("md5") and f["md5"] in known_md5s:
                # Add as a new location to the existing record
                for r in records:
                    if r.get("md5") == f["md5"]:
                        r["locations"].append({
                            "drive_file_id": f["id"],
                            "folder_path": f.get("folder_path", ""),
                            "folder_id": f.get("parent_id", ""),
                            "file_size_bytes": f.get("size", 0),
                            "width": f.get("width"),
                            "height": f.get("height"),
                        })
                        known_file_ids.add(f["id"])
                        print(f"  Duplicate of existing record: {f['name']} → {r['id']}", flush=True)
                        break
            else:
                new_files.append(f)

    # Find removed records (all locations gone from Drive)
    removed_records = []
    kept_records = []
    for r in records:
        locs_on_drive = [l for l in r.get("locations", []) if l["drive_file_id"] in drive_file_ids]
        alts_on_drive = [a for a in r.get("alternatives", []) if a["drive_file_id"] in drive_file_ids]

        if not locs_on_drive and not alts_on_drive:
            removed_records.append(r)
        else:
            r["locations"] = locs_on_drive
            r["alternatives"] = alts_on_drive
            # Update top-level links if best location changed
            if locs_on_drive:
                r["drive_file_url"] = f"https://drive.google.com/file/d/{locs_on_drive[0]['drive_file_id']}/view"
                r["drive_folder_url"] = f"https://drive.google.com/drive/folders/{locs_on_drive[0].get('folder_id', '')}"
            kept_records.append(r)

    print(f"\nSync summary:", flush=True)
    print(f"  New files to process: {len(new_files)}", flush=True)
    print(f"  Records to remove: {len(removed_records)}", flush=True)

    if dry_run:
        if new_files:
            print("\nNew files:", flush=True)
            for f in new_files[:20]:
                print(f"  + {f['name']} ({f.get('folder_path', '')})", flush=True)
            if len(new_files) > 20:
                print(f"  ... and {len(new_files) - 20} more", flush=True)
        if removed_records:
            print("\nRemoved records:", flush=True)
            for r in removed_records[:20]:
                print(f"  - {r['original_filename']} ({r['id']})", flush=True)
            if len(removed_records) > 20:
                print(f"  ... and {len(removed_records) - 20} more", flush=True)
        print("\nDry run — no changes made.", flush=True)
        return

    # Process removals — delete thumbnails from R2
    for r in removed_records:
        slug = r["id"]
        try:
            r2.delete_object(Bucket=bucket, Key=f"thumbnails/{slug}.jpg")
        except Exception:
            pass
        for alt in r.get("alternatives", []):
            alt_name = Path(alt.get("filename", "")).stem
            if alt_name:
                try:
                    r2.delete_object(Bucket=bucket, Key=f"thumbnails/{alt_name}.jpg")
                except Exception:
                    pass

    if removed_records:
        print(f"\n  Removed {len(removed_records)} records and their thumbnails", flush=True)

    # Process new files
    records = kept_records
    used_slugs = {r["id"] for r in records}

    ai_client = None
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        ai_client = anthropic.Anthropic(api_key=api_key)
    else:
        print("  Warning: ANTHROPIC_API_KEY not set, skipping AI analysis", flush=True)

    # Pre-allocate slugs serially so the parallel workers don't race on the
    # used_slugs set. Each file gets a unique slug before any parallel work
    # starts.
    file_slugs = []
    for f in new_files:
        slug = unique_slug(slugify(f["name"]), used_slugs)
        used_slugs.add(slug)
        file_slugs.append((f, slug))

    added = 0
    failed = 0
    if file_slugs:
        print(f"\nProcessing {len(file_slugs)} new files with {PARALLEL_WORKERS} workers...", flush=True)

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
        futures = {
            pool.submit(
                process_new_file, f, drive, r2, bucket, public_url, ai_client, slug
            ): f
            for f, slug in file_slugs
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            try:
                record = fut.result()
            except Exception as e:
                print(f"    [{i}/{len(file_slugs)}] worker error: {e}", flush=True)
                record = None
            if record:
                records.append(record)
                added += 1
            else:
                failed += 1
            if i % 25 == 0:
                print(f"  Progress: {i}/{len(file_slugs)} ({added} added, {failed} failed)", flush=True)

    # Upload updated data.json and campaigns.json
    if new_files or removed_records:
        print(f"\nUploading updated data.json ({len(records)} records)...", flush=True)
        upload_data_json(r2, bucket, records)
        upload_campaigns_json(r2, bucket, records)

    # Clean up any orphan thumbnails (uploaded by previous runs that died
    # before data.json was written, or left behind after deletions).
    print("\nChecking for orphan thumbnails in R2...", flush=True)
    orphans_cleaned = cleanup_orphan_thumbnails(r2, bucket, records)

    completed_at = datetime.now(timezone.utc)
    status.update({
        "completed_at": completed_at.isoformat(),
        "duration_seconds": int((completed_at - started_at).total_seconds()),
        "status": "success" if failed == 0 else "partial",
        "added": added,
        "removed": len(removed_records),
        "failed": failed,
        "orphans_cleaned": orphans_cleaned,
        "total_records": len(records),
    })
    write_sync_status(r2, bucket, status)

    print(f"\n{'='*60}", flush=True)
    print("SYNC COMPLETE", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Drive files scanned: {len(drive_files)}", flush=True)
    print(f"  Records before: {len(kept_records) + len(removed_records)}", flush=True)
    print(f"  Added: {added}", flush=True)
    print(f"  Removed: {len(removed_records)}", flush=True)
    print(f"  Failed: {failed}", flush=True)
    print(f"  Orphan thumbnails cleaned: {orphans_cleaned}", flush=True)
    print(f"  Records now: {len(records)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # Best-effort: record the failure so the UI status icon can show it.
        print(f"\nSYNC FAILED: {e}", flush=True)
        traceback.print_exc()
        try:
            r2 = get_r2_client()
            bucket = get_env("R2_BUCKET_NAME")
            now = datetime.now(timezone.utc)
            write_sync_status(r2, bucket, {
                "started_at": now.isoformat(),
                "completed_at": now.isoformat(),
                "duration_seconds": 0,
                "status": "failed",
                "added": 0,
                "removed": 0,
                "failed": 0,
                "orphans_cleaned": 0,
                "total_records": None,
                "error": str(e),
            })
        except Exception as inner:
            print(f"  Also failed to write sync-status.json: {inner}", flush=True)
        sys.exit(1)
