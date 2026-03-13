"""Shared utilities for photo library migration scripts."""

import hashlib
import os
import re
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env", override=True)


def get_env(key: str) -> str:
    """Get a required environment variable or raise an error."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Missing required environment variable: {key}")
    return value


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    Handles unicode, strips non-alphanumeric characters, collapses hyphens.
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    # Remove file extension if present
    text = re.sub(r"\.[a-z0-9]+$", "", text)
    # Replace non-alphanumeric with hyphens
    text = re.sub(r"[^a-z0-9]+", "-", text)
    # Collapse multiple hyphens and strip leading/trailing
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "untitled"


def unique_slug(slug: str, existing_slugs: set) -> str:
    """Return a unique slug by appending a numeric suffix if needed."""
    if slug not in existing_slugs:
        return slug
    counter = 2
    while f"{slug}-{counter}" in existing_slugs:
        counter += 1
    return f"{slug}-{counter}"


def is_image_file(filename: str) -> bool:
    """Check if a filename has a supported image extension."""
    extensions = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp", ".bmp", ".heic"}
    return Path(filename).suffix.lower() in extensions


def is_vector_or_design_file(filename: str) -> bool:
    """Check if a file is a vector/design format (never a photo)."""
    extensions = {".svg", ".ai", ".eps", ".pdf"}
    return Path(filename).suffix.lower() in extensions


def md5_from_file(filepath: str) -> str:
    """Compute MD5 hash of a local file."""
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# Patterns that suggest a file is a logo or designed material
LOGO_FOLDER_PATTERNS = re.compile(
    r"(logo|brand|icon|assets|design|template|graphic|banner|favicon)",
    re.IGNORECASE,
)

# Patterns that suggest filename variants (duplicates)
DUPLICATE_SUFFIX_PATTERN = re.compile(
    r"^(.+?)[\s_-]*"
    r"(?:\(\d+\)|"  # photo (1), photo (2)
    r"_?(?:final|v\d+|edited|copy|small|thumb|web|large|medium|low|high|hq|lq|crop|cropped))"
    r"(\.[a-z0-9]+)$",
    re.IGNORECASE,
)


def extract_base_filename(filename: str):
    """Extract the base filename, stripping common duplicate suffixes.

    Returns (base_name_with_extension, was_modified) or None if no pattern matched.
    """
    match = DUPLICATE_SUFFIX_PATTERN.match(filename)
    if match:
        base = match.group(1).rstrip("_- ")
        ext = match.group(2)
        return f"{base}{ext}", True
    return filename, False


def format_file_size(size_bytes: int) -> str:
    """Format bytes as a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# Folder names that are not campaigns — these are generic containers
NON_CAMPAIGN_FOLDERS = {
    "photos", "images", "pictures", "media", "assets",
    "campaign assets", "shared drive", "root",
    "general good photos", "staff and fun pics",
    "background footage", "monthly round ups",
}

# Campaign name merges — map variant names to canonical names
CAMPAIGN_MERGES = {
    "Ride the Change 2021": "Ride the Change",
}


def infer_campaign_from_path(folder_path: str) -> str | None:
    """Try to infer a campaign name from a Google Drive folder path.

    Looks for the first meaningful folder name in the path that isn't
    a generic container. Applies campaign name merges for consistency.
    """
    parts = [p.strip() for p in folder_path.split("/") if p.strip()]

    for part in parts:
        if part.lower() not in NON_CAMPAIGN_FOLDERS:
            # Apply merges
            return CAMPAIGN_MERGES.get(part, part)
    return None
