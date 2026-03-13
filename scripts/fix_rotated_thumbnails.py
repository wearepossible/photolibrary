"""Fix thumbnails that have incorrect rotation due to EXIF orientation.

Scans downloaded images for non-default EXIF orientation tags,
regenerates only those thumbnails with correct rotation applied.

Usage:
    python scripts/fix_rotated_thumbnails.py
"""

import json
from pathlib import Path

from PIL import Image, ImageOps

DATA_DIR = Path(__file__).parent.parent / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
THUMBNAILS_DIR = DATA_DIR / "thumbnails"
THUMBNAIL_WIDTH = 400
THUMBNAIL_QUALITY = 75

# EXIF orientation tag ID
ORIENTATION_TAG = 0x0112

# Orientation value 1 = normal (no rotation needed)
# Values 2-8 indicate various rotations/flips
NORMAL_ORIENTATION = 1


def check_exif_orientation(image_path: Path) -> int | None:
    """Return the EXIF orientation value, or None if not present."""
    try:
        img = Image.open(image_path)
        exif = img.getexif()
        if exif and ORIENTATION_TAG in exif:
            return exif[ORIENTATION_TAG]
        return None
    except Exception:
        return None


def regenerate_thumbnail(source_path: Path, thumb_path: Path) -> bool:
    """Regenerate a thumbnail with EXIF orientation applied."""
    try:
        img = Image.open(source_path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")

        ratio = THUMBNAIL_WIDTH / img.width
        new_height = int(img.height * ratio)
        img = img.resize((THUMBNAIL_WIDTH, new_height), Image.LANCZOS)

        img.save(thumb_path, "JPEG", quality=THUMBNAIL_QUALITY)
        return True
    except Exception as e:
        print(f"  ERROR: {source_path.name}: {e}", flush=True)
        return False


def main():
    # Build a map of slug -> download filename
    # Thumbnails are named {slug}.jpg, downloads may have various extensions
    download_files = {f.stem: f for f in DOWNLOADS_DIR.iterdir() if f.is_file()}
    thumb_files = list(THUMBNAILS_DIR.glob("*.jpg"))

    print(f"Checking {len(download_files)} downloaded images for EXIF rotation...", flush=True)

    needs_fix = []

    for thumb in thumb_files:
        slug = thumb.stem
        source = download_files.get(slug)
        if not source:
            continue

        orientation = check_exif_orientation(source)
        if orientation is not None and orientation != NORMAL_ORIENTATION:
            needs_fix.append((source, thumb, orientation))

    print(f"\nFound {len(needs_fix)} images with non-default EXIF orientation.", flush=True)

    if not needs_fix:
        print("Nothing to fix!")
        return

    print(f"Regenerating {len(needs_fix)} thumbnails...\n", flush=True)

    fixed = 0
    for i, (source, thumb, orientation) in enumerate(needs_fix):
        # Delete existing thumbnail so we can regenerate
        thumb.unlink()
        if regenerate_thumbnail(source, thumb):
            fixed += 1

        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(needs_fix)} done...", flush=True)

    print(f"\nFixed {fixed} of {len(needs_fix)} thumbnails.", flush=True)
    print(f"Next step: re-upload thumbnails to R2 with upload_to_r2.py")


if __name__ == "__main__":
    main()
