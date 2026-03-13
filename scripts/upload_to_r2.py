"""Phase 3: Upload thumbnails, data.json, and campaigns.json to Cloudflare R2.

Usage:
    python scripts/upload_to_r2.py

Inputs:
    data/thumbnails/      — generated thumbnails
    data/data.json        — photo records with AI metadata
    data/campaigns.json   — campaign list

Outputs:
    Files uploaded to R2 bucket:
      thumbnails/*.jpg
      data.json
      campaigns.json
    data/data.json updated with thumbnail_url fields
"""

import json
import os
from pathlib import Path

import boto3
from botocore.config import Config

from utils import format_file_size, get_env

DATA_DIR = Path(__file__).parent.parent / "data"
THUMBNAILS_DIR = DATA_DIR / "thumbnails"


def get_r2_client():
    """Create an S3-compatible client for Cloudflare R2.

    Uses R2_JURISDICTION env var (e.g. 'eu') for jurisdiction-specific buckets.
    """
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


def get_public_url(bucket_name: str) -> str:
    """Get the public URL base for the R2 bucket.

    Uses the R2_PUBLIC_URL env var if set, otherwise constructs the default .r2.dev URL.
    """
    import os
    public_url = os.getenv("R2_PUBLIC_URL")
    if public_url:
        return public_url.rstrip("/")
    # Default r2.dev URL (must enable public access in Cloudflare dashboard)
    return f"https://pub-{get_env('R2_ACCOUNT_ID')}.r2.dev"


def upload_file(client, bucket: str, local_path: Path, r2_key: str, content_type: str):
    """Upload a file to R2."""
    client.upload_file(
        str(local_path),
        bucket,
        r2_key,
        ExtraArgs={"ContentType": content_type},
    )


def main():
    bucket = get_env("R2_BUCKET_NAME")
    client = get_r2_client()
    public_url = get_public_url(bucket)

    print(f"R2 bucket: {bucket}", flush=True)
    print(f"Public URL: {public_url}", flush=True)

    # Load data.json
    with open(DATA_DIR / "data.json") as f:
        records = json.load(f)

    # Upload thumbnails
    thumb_files = sorted(THUMBNAILS_DIR.glob("*.jpg"))
    print(f"\nUploading {len(thumb_files)} thumbnails...", flush=True)

    uploaded = 0
    total_size = 0
    for i, thumb in enumerate(thumb_files):
        r2_key = f"thumbnails/{thumb.name}"
        upload_file(client, bucket, thumb, r2_key, "image/jpeg")
        total_size += thumb.stat().st_size
        uploaded += 1

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(thumb_files)} uploaded ({format_file_size(total_size)})...", flush=True)

    print(f"  All {uploaded} thumbnails uploaded ({format_file_size(total_size)})", flush=True)

    # Update thumbnail_url in records
    for record in records:
        slug = record["id"]
        record["thumbnail_url"] = f"{public_url}/thumbnails/{slug}.jpg"

        for alt in record.get("alternatives", []):
            # Alternative thumbnails use the filename (without extension) + .jpg
            alt_name = Path(alt["filename"]).stem
            alt["thumbnail_url"] = f"{public_url}/thumbnails/{alt_name}.jpg"

    # Upload data.json
    data_json_path = DATA_DIR / "data.json"
    with open(data_json_path, "w") as f:
        json.dump(records, f, indent=2)

    print("\nUploading data.json...", flush=True)
    upload_file(client, bucket, data_json_path, "data.json", "application/json")
    print(f"  data.json uploaded ({format_file_size(data_json_path.stat().st_size)})", flush=True)

    # Upload campaigns.json
    campaigns_path = DATA_DIR / "campaigns.json"
    if campaigns_path.exists():
        print("Uploading campaigns.json...", flush=True)
        upload_file(client, bucket, campaigns_path, "campaigns.json", "application/json")
        print(f"  campaigns.json uploaded", flush=True)

    print(f"\n{'='*60}")
    print("UPLOAD COMPLETE")
    print(f"{'='*60}")
    print(f"Thumbnails: {uploaded}")
    print(f"Total thumbnail size: {format_file_size(total_size)}")
    print(f"Data URL: {public_url}/data.json")
    print(f"\nIMPORTANT: Make sure to enable public access on the R2 bucket")
    print(f"in the Cloudflare dashboard, and configure CORS to allow")
    print(f"requests from your Netlify site domain.")
    print(f"\nNext step: Build and deploy the web interface")


if __name__ == "__main__":
    main()
