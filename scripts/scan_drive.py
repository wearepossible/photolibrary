"""Phase 1a: Scan a Google Workspace Shared Drive and catalogue all image files.

Usage:
    python scripts/scan_drive.py

Outputs:
    data/drive_scan_report.json — full catalogue of every image found

Requires:
    GOOGLE_SERVICE_ACCOUNT_KEY and SHARED_DRIVE_ID in .env
"""

import json
import sys
import time
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils import (
    LOGO_FOLDER_PATTERNS,
    format_file_size,
    get_env,
    infer_campaign_from_path,
    is_image_file,
    is_vector_or_design_file,
)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Fields to request from the Drive API
FILE_FIELDS = (
    "id, name, mimeType, size, md5Checksum, createdTime, modifiedTime, "
    "parents, thumbnailLink, imageMediaMetadata, webContentLink"
)


def build_drive_service():
    """Authenticate and return a Google Drive API service."""
    key_path = get_env("GOOGLE_SERVICE_ACCOUNT_KEY")
    creds = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds)


def get_folder_tree(service, drive_id: str) -> dict[str, str]:
    """Build a mapping of folder_id -> full folder path for the entire drive.

    This lets us reconstruct the full path for any file without repeated API calls.
    """
    folders = {}  # id -> {name, parent_id}
    page_token = None

    print("Building folder tree...", flush=True)
    while True:
        try:
            response = service.files().list(
                q="mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                corpora="drive",
                driveId=drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields=f"nextPageToken, files(id, name, parents)",
                pageSize=1000,
                pageToken=page_token,
            ).execute()
        except HttpError as e:
            if e.resp.status == 429:
                print("  Rate limited, waiting 10s...", flush=True)
                time.sleep(10)
                continue
            raise

        for folder in response.get("files", []):
            parent_id = folder.get("parents", [None])[0]
            folders[folder["id"]] = {
                "name": folder["name"],
                "parent_id": parent_id,
            }

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # Resolve full paths
    def resolve_path(folder_id):
        parts = []
        current = folder_id
        seen = set()
        while current and current in folders and current not in seen:
            seen.add(current)
            parts.append(folders[current]["name"])
            current = folders[current]["parent_id"]
        parts.reverse()
        return "/".join(parts)

    folder_paths = {fid: resolve_path(fid) for fid in folders}
    print(f"  Found {len(folder_paths)} folders", flush=True)
    return folder_paths


def scan_all_images(service, drive_id: str, folder_paths: dict[str, str]) -> list[dict]:
    """Scan the entire shared drive for image files."""
    image_mimetypes = [
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
        "image/bmp",
        "image/heic",
        "image/heif",
        "image/svg+xml",
    ]
    query = (
        "trashed = false and ("
        + " or ".join(f"mimeType = '{mt}'" for mt in image_mimetypes)
        + ")"
    )

    all_files = []
    page_token = None
    page_count = 0

    print("Scanning for image files...", flush=True)
    while True:
        try:
            response = service.files().list(
                q=query,
                corpora="drive",
                driveId=drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields=f"nextPageToken, files({FILE_FIELDS})",
                pageSize=1000,
                pageToken=page_token,
            ).execute()
        except HttpError as e:
            if e.resp.status == 429:
                print("  Rate limited, waiting 10s...", flush=True)
                time.sleep(10)
                continue
            raise

        files = response.get("files", [])
        page_count += 1

        for f in files:
            parent_id = f.get("parents", [None])[0]
            folder_path = folder_paths.get(parent_id, "")

            # Extract image dimensions from metadata if available
            img_meta = f.get("imageMediaMetadata", {})
            width = img_meta.get("width")
            height = img_meta.get("height")

            # Flag potential logos/design material
            flags = []

            if is_vector_or_design_file(f["name"]):
                flags.append("vector_format")

            if width and height and (width < 200 or height < 200):
                flags.append("small_dimensions")

            if LOGO_FOLDER_PATTERNS.search(folder_path):
                flags.append("logo_folder")

            if f.get("mimeType") == "image/png":
                # We can't check for alpha without downloading, but flag PNGs
                # in logo-related folders as higher likelihood
                if "logo_folder" in flags:
                    flags.append("likely_logo_png")

            if f.get("mimeType") == "image/svg+xml":
                flags.append("svg_always_designed")

            record = {
                "id": f["id"],
                "name": f["name"],
                "mime_type": f.get("mimeType", ""),
                "size": int(f.get("size", 0)),
                "md5": f.get("md5Checksum", ""),
                "created_time": f.get("createdTime", ""),
                "modified_time": f.get("modifiedTime", ""),
                "folder_path": folder_path,
                "parent_id": parent_id,
                "thumbnail_link": f.get("thumbnailLink", ""),
                "download_link": f.get("webContentLink", ""),
                "width": width,
                "height": height,
                "camera_make": img_meta.get("cameraMake", ""),
                "camera_model": img_meta.get("cameraModel", ""),
                "exif_time": img_meta.get("time", ""),
                "flags": flags,
                "inferred_campaign": infer_campaign_from_path(folder_path),
            }
            all_files.append(record)

        print(f"  Page {page_count}: found {len(files)} files (total: {len(all_files)})", flush=True)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return all_files


def print_summary(files: list[dict]):
    """Print a summary of the scan results."""
    total_size = sum(f["size"] for f in files)
    by_type = {}
    for f in files:
        ext = Path(f["name"]).suffix.lower()
        by_type.setdefault(ext, []).append(f)

    flagged = [f for f in files if f["flags"]]
    campaigns = {}
    for f in files:
        c = f["inferred_campaign"] or "(no campaign)"
        campaigns.setdefault(c, []).append(f)

    print("\n" + "=" * 60)
    print("SCAN SUMMARY")
    print("=" * 60)
    print(f"Total image files found: {len(files)}")
    print(f"Total size: {format_file_size(total_size)}")
    print()

    print("By file type:")
    for ext, ext_files in sorted(by_type.items(), key=lambda x: -len(x[1])):
        print(f"  {ext:8s}  {len(ext_files):4d} files  ({format_file_size(sum(f['size'] for f in ext_files))})")
    print()

    print(f"Flagged for review (possible logos/design): {len(flagged)}")
    for flag_type in ["small_dimensions", "logo_folder", "vector_format", "svg_always_designed"]:
        count = sum(1 for f in flagged if flag_type in f["flags"])
        if count:
            print(f"  {flag_type}: {count}")
    print()

    print(f"Inferred campaigns: {len(campaigns)}")
    for campaign, camp_files in sorted(campaigns.items(), key=lambda x: -len(x[1])):
        print(f"  {campaign}: {len(camp_files)} files")

    if len(files) > 500:
        print(f"\n⚠  Found {len(files)} files — more than the expected 300-500.")
        print("   This will increase Claude API costs for Phase 3 analysis.")

    print("=" * 60)


def main():
    drive_id = get_env("SHARED_DRIVE_ID")
    service = build_drive_service()

    # Build folder tree first (needed for path resolution)
    folder_paths = get_folder_tree(service, drive_id)

    # Scan all images
    files = scan_all_images(service, drive_id, folder_paths)

    # Save full report
    output_path = DATA_DIR / "drive_scan_report.json"
    with open(output_path, "w") as f:
        json.dump(
            {
                "scan_date": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "shared_drive_id": drive_id,
                "total_files": len(files),
                "files": files,
            },
            f,
            indent=2,
        )

    print_summary(files)
    print(f"\nFull report saved to: {output_path}")
    print("Next step: run `python scripts/dedup_and_filter.py` to check for duplicates")


if __name__ == "__main__":
    main()
