"""Phase 1b: Deduplicate and filter scanned Drive images, produce a review report.

Three-pass deduplication:
  1. Exact duplicates (by MD5 hash)
  2. Near-duplicates (by perceptual hash of thumbnails)
  3. Filename pattern duplicates

Plus filtering for logos/designed material.

Usage:
    python scripts/dedup_and_filter.py

Inputs:
    data/drive_scan_report.json (from scan_drive.py)

Outputs:
    data/dedup_report.html — human-readable review report (open in browser)
"""

import io
import json
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

import imagehash
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from PIL import Image

from utils import (
    extract_base_filename,
    format_file_size,
    get_env,
)

DATA_DIR = Path(__file__).parent.parent / "data"
THUMB_CACHE_DIR = DATA_DIR / "thumbnails_cache"
THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

PHASH_DISTANCE_THRESHOLD = 8


def load_scan_report() -> dict:
    """Load the scan report from Phase 1a."""
    report_path = DATA_DIR / "drive_scan_report.json"
    if not report_path.exists():
        raise FileNotFoundError(
            f"No scan report found at {report_path}. Run scan_drive.py first."
        )
    with open(report_path) as f:
        return json.load(f)


def build_drive_service():
    """Authenticate and return a Google Drive API service."""
    key_path = get_env("GOOGLE_SERVICE_ACCOUNT_KEY")
    creds = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds)


def pass1_exact_duplicates(files: list[dict]) -> list[dict]:
    """Group files with identical MD5 checksums.

    Returns a list of duplicate groups, each with a recommended keeper.
    """
    by_md5 = defaultdict(list)
    for f in files:
        if f["md5"]:
            by_md5[f["md5"]].append(f)

    groups = []
    for md5, group in by_md5.items():
        if len(group) < 2:
            continue

        # Pick the "best" copy: prefer deeper folder path, then longer filename
        scored = sorted(
            group,
            key=lambda f: (
                f["folder_path"].count("/"),  # deeper = more specific
                len(f["name"]),  # longer name = more descriptive
                f["size"],  # larger = higher quality
            ),
            reverse=True,
        )
        keeper = scored[0]
        duplicates = scored[1:]

        # Collect all unique folder paths (for campaign merging)
        all_paths = list({f["folder_path"] for f in group if f["folder_path"]})
        all_campaigns = list({
            f["inferred_campaign"] for f in group
            if f["inferred_campaign"]
        })

        groups.append({
            "type": "exact",
            "md5": md5,
            "keeper": keeper,
            "duplicates": duplicates,
            "all_folder_paths": all_paths,
            "all_campaigns": all_campaigns,
        })

    return groups


def download_thumbnail(service, file_id: str, thumbnail_link: str) -> Image.Image | None:
    """Download a thumbnail image, with caching.

    Uses the Drive API thumbnail link if available, otherwise exports a small version.
    """
    cache_path = THUMB_CACHE_DIR / f"{file_id}.jpg"

    if cache_path.exists():
        try:
            return Image.open(cache_path)
        except Exception:
            cache_path.unlink(missing_ok=True)

    # Try the thumbnail link first (fastest)
    if thumbnail_link:
        # The thumbnail link requires auth — use the service's credentials
        try:
            # Get an authorized http object
            authed_http = service._http
            response, content = authed_http.request(thumbnail_link)
            if response.status == 200 and content:
                img = Image.open(io.BytesIO(content))
                img.save(cache_path, "JPEG")
                return img
        except Exception:
            pass

    # Fall back to exporting a small version via the API
    try:
        request = service.files().get_media(
            fileId=file_id,
            supportsAllDrives=True,
        )
        content = request.execute()
        if content:
            img = Image.open(io.BytesIO(content))
            # Resize to thumbnail for hashing (don't need full res)
            img.thumbnail((200, 200))
            img = img.convert("RGB")
            img.save(cache_path, "JPEG")
            return img
    except Exception:
        pass

    return None


def pass2_perceptual_duplicates(
    files: list[dict],
    exact_dup_ids: set[str],
    service,
) -> list[dict]:
    """Find near-duplicates using perceptual hashing on thumbnails.

    Skips files already identified as exact duplicates.
    """
    # Only process files that aren't already flagged as exact dups
    candidates = [f for f in files if f["id"] not in exact_dup_ids]

    print(f"Pass 2: Computing perceptual hashes for {len(candidates)} files...", flush=True)

    hashes = {}  # file_id -> (phash, file_record)
    for i, f in enumerate(candidates):
        if i % 20 == 0:
            print(f"  Processing {i+1}/{len(candidates)}...", flush=True)

        img = download_thumbnail(service, f["id"], f.get("thumbnail_link", ""))
        if img is None:
            continue

        try:
            phash = imagehash.phash(img)
            hashes[f["id"]] = (phash, f)
        except Exception:
            continue

        # Be nice to the API
        if i % 50 == 49:
            time.sleep(1)

    # Compare all pairs (O(n^2) but n should be <500)
    hash_list = list(hashes.items())
    groups = []
    seen = set()

    for i in range(len(hash_list)):
        if hash_list[i][0] in seen:
            continue

        fid_a, (hash_a, file_a) = hash_list[i]
        similar = []

        for j in range(i + 1, len(hash_list)):
            if hash_list[j][0] in seen:
                continue
            fid_b, (hash_b, file_b) = hash_list[j]

            distance = hash_a - hash_b
            if distance <= PHASH_DISTANCE_THRESHOLD and distance > 0:
                similar.append({"file": file_b, "distance": distance})
                seen.add(fid_b)

        if similar:
            seen.add(fid_a)
            # Sort similar files by size (largest first = highest quality)
            all_in_group = [{"file": file_a, "distance": 0}] + similar
            all_in_group.sort(key=lambda x: x["file"]["size"], reverse=True)

            groups.append({
                "type": "perceptual",
                "files": all_in_group,
                "recommended_keeper": all_in_group[0]["file"],
            })

    print(f"  Found {len(groups)} near-duplicate groups", flush=True)
    return groups


def pass3_filename_duplicates(
    files: list[dict],
    already_flagged_ids: set[str],
) -> list[dict]:
    """Find files that look like renamed/edited versions of each other."""
    candidates = [f for f in files if f["id"] not in already_flagged_ids]

    by_base = defaultdict(list)
    for f in candidates:
        base, was_modified = extract_base_filename(f["name"])
        # Only group if the filename actually had a duplicate-style suffix
        key = base.lower()
        by_base[key].append({"file": f, "was_modified": was_modified})

    groups = []
    for base, group_files in by_base.items():
        # Only flag if there are multiple files AND at least one has a variant suffix
        if len(group_files) < 2:
            continue
        if not any(gf["was_modified"] for gf in group_files):
            continue

        # The "original" is likely the one without a suffix modification
        originals = [gf for gf in group_files if not gf["was_modified"]]
        variants = [gf for gf in group_files if gf["was_modified"]]

        groups.append({
            "type": "filename",
            "base_name": base,
            "originals": [gf["file"] for gf in originals],
            "variants": [gf["file"] for gf in variants],
            "all_files": [gf["file"] for gf in group_files],
        })

    return groups


def get_flagged_logos(files: list[dict]) -> list[dict]:
    """Return files flagged as potential logos/design material."""
    return [f for f in files if f["flags"]]


def generate_html_report(
    files: list[dict],
    exact_groups: list[dict],
    perceptual_groups: list[dict],
    filename_groups: list[dict],
    flagged_logos: list[dict],
    scan_date: str,
) -> str:
    """Generate a self-contained HTML report for manual review."""
    # Compute clean files (not involved in any dedup group or flagged)
    all_dup_ids = set()
    for g in exact_groups:
        all_dup_ids.add(g["keeper"]["id"])
        for d in g["duplicates"]:
            all_dup_ids.add(d["id"])
    for g in perceptual_groups:
        for f in g["files"]:
            all_dup_ids.add(f["file"]["id"])
    for g in filename_groups:
        for f in g["all_files"]:
            all_dup_ids.add(f["id"])
    flagged_ids = {f["id"] for f in flagged_logos}

    clean_files = [f for f in files if f["id"] not in all_dup_ids and f["id"] not in flagged_ids]

    # Inferred campaigns from clean files
    campaigns = defaultdict(list)
    for f in files:
        c = f.get("inferred_campaign") or "(no campaign)"
        campaigns[c].append(f)

    total_size = sum(f["size"] for f in files)

    def thumb_img(file_record, width=120):
        """Generate an img tag using the Google Drive thumbnail."""
        thumb = file_record.get("thumbnail_link", "")
        if thumb:
            return f'<img src="{thumb}" style="max-width:{width}px;max-height:{width}px;object-fit:contain;" loading="lazy" alt="{file_record["name"]}">'
        return f'<div style="width:{width}px;height:{width}px;background:#eee;display:flex;align-items:center;justify-content:center;font-size:11px;color:#999;">No preview</div>'

    def file_info_html(f, show_path=True):
        parts = [
            f'<strong>{f["name"]}</strong>',
            f'{format_file_size(f["size"])}',
        ]
        if f.get("width") and f.get("height"):
            parts.append(f'{f["width"]}x{f["height"]}')
        if show_path and f.get("folder_path"):
            parts.append(f'<span style="color:#666;font-size:12px;">{f["folder_path"]}</span>')
        return "<br>".join(parts)

    html_parts = [f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Photo Library Migration — Dedup Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; color: #333; }}
h1 {{ border-bottom: 2px solid #333; padding-bottom: 10px; }}
h2 {{ color: #555; margin-top: 40px; border-bottom: 1px solid #ddd; padding-bottom: 5px; }}
.summary {{ background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0; }}
.summary dt {{ font-weight: bold; }}
.summary dd {{ margin: 0 0 10px 0; }}
.group {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin: 15px 0; }}
.group-header {{ font-weight: bold; margin-bottom: 10px; display: flex; align-items: center; gap: 10px; }}
.file-row {{ display: flex; gap: 15px; align-items: flex-start; padding: 8px 0; border-top: 1px solid #eee; }}
.file-row:first-child {{ border-top: none; }}
.file-info {{ font-size: 13px; line-height: 1.5; }}
.keeper {{ background: #e8f5e9; padding: 5px 10px; border-radius: 4px; font-size: 12px; font-weight: bold; color: #2e7d32; display: inline-block; margin-bottom: 5px; }}
.duplicate {{ background: #fff3e0; padding: 5px 10px; border-radius: 4px; font-size: 12px; color: #e65100; display: inline-block; margin-bottom: 5px; }}
.flag {{ background: #fce4ec; padding: 2px 8px; border-radius: 4px; font-size: 11px; color: #c62828; display: inline-block; margin: 2px; }}
.campaign-list {{ columns: 2; }}
.campaign-list li {{ margin-bottom: 4px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #eee; }}
th {{ background: #f5f5f5; }}
.count {{ font-weight: bold; color: #1565c0; }}
.decision-toggle {{ font-size: 13px; padding: 4px 12px; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; background: #fff; }}
.decision-toggle.merge {{ background: #e8f5e9; border-color: #4caf50; color: #2e7d32; }}
.decision-toggle.split {{ background: #fff3e0; border-color: #ff9800; color: #e65100; }}
.decision-toggle.include {{ background: #e8f5e9; border-color: #4caf50; color: #2e7d32; }}
.decision-toggle.exclude {{ background: #fce4ec; border-color: #ef5350; color: #c62828; }}
.export-bar {{ position: sticky; top: 0; background: #1565c0; color: #fff; padding: 12px 20px; border-radius: 0 0 8px 8px; z-index: 100; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }}
.export-bar button {{ background: #fff; color: #1565c0; border: none; padding: 8px 20px; border-radius: 4px; font-size: 14px; font-weight: bold; cursor: pointer; }}
.export-bar button:hover {{ background: #e3f2fd; }}
.export-bar .status {{ font-size: 13px; }}
.collapsible {{ cursor: pointer; user-select: none; }}
.collapsible:hover {{ color: #1565c0; }}
.collapse-icon {{ font-size: 12px; margin-left: 5px; }}
</style>
</head>
<body>

<div class="export-bar">
<div class="status" id="review-status">Review the groups below, then export your decisions.</div>
<button onclick="exportDecisions()">Export Decisions</button>
</div>

<h1>Photo Library Migration — Dedup &amp; Filter Report</h1>
<p>Generated: {scan_date}</p>

<div class="summary">
<h2 style="margin-top:0">Summary</h2>
<dl>
<dt>Total image files found</dt>
<dd class="count">{len(files)}</dd>
<dt>Total size</dt>
<dd>{format_file_size(total_size)}</dd>
<dt>Exact duplicate groups</dt>
<dd class="count">{len(exact_groups)} groups ({sum(len(g["duplicates"]) for g in exact_groups)} redundant files — auto-merged)</dd>
<dt>Probable near-duplicate groups</dt>
<dd class="count">{len(perceptual_groups)} groups (review needed)</dd>
<dt>Filename variant groups</dt>
<dd class="count">{len(filename_groups)} groups (review needed)</dd>
<dt>Flagged as logos/design</dt>
<dd class="count">{len(flagged_logos)} files (review needed)</dd>
<dt>Clean files (no action needed)</dt>
<dd class="count">{len(clean_files)}</dd>
</dl>
</div>
"""]

    # Section 1: Exact duplicates (starts collapsed)
    html_parts.append(f"""
<h2 class="collapsible" onclick="toggleSection(this)">1. Exact Duplicates ({len(exact_groups)} groups) — auto-merged <span class="collapse-icon">&#9654;</span></h2>
<div class="section-content" style="display:none">
<p>These files have identical MD5 checksums — byte-for-byte identical. They will be merged
into single records with multiple locations listed. This is auto-approved (no action needed).</p>
""")

    for i, group in enumerate(exact_groups):
        file_ids_json = json.dumps([group["keeper"]["id"]] + [d["id"] for d in group["duplicates"]])
        html_parts.append(f'<div class="group" data-group-type="exact" data-group-id="exact-{i}" data-file-ids=\'{file_ids_json}\' data-decision="merge">')
        html_parts.append(f'<div class="group-header">Group {i+1} — MD5: {group["md5"][:12]}...')
        html_parts.append(f'<button class="decision-toggle merge" onclick="toggleDecision(this, \'exact-{i}\')">Merge</button>')
        html_parts.append('</div>')
        if group["all_campaigns"]:
            html_parts.append(f'<div style="font-size:12px;color:#666;margin-bottom:8px;">Campaigns: {", ".join(group["all_campaigns"])}</div>')

        # Keeper
        k = group["keeper"]
        html_parts.append(f'<div class="file-row">')
        html_parts.append(thumb_img(k))
        html_parts.append(f'<div class="file-info"><span class="keeper">BEST COPY</span><br>{file_info_html(k)}</div>')
        html_parts.append('</div>')

        # Duplicates
        for d in group["duplicates"]:
            html_parts.append(f'<div class="file-row">')
            html_parts.append(thumb_img(d))
            html_parts.append(f'<div class="file-info"><span class="duplicate">ALSO AT</span><br>{file_info_html(d)}</div>')
            html_parts.append('</div>')

        html_parts.append('</div>')

    html_parts.append('</div>')  # close section-content for exact dupes

    # Section 2: Perceptual near-duplicates
    html_parts.append(f"""
<h2 class="collapsible" onclick="toggleSection(this)">2. Probable Near-Duplicates ({len(perceptual_groups)} groups) <span class="collapse-icon">&#9660;</span></h2>
<div class="section-content">
<p>These files have very similar visual content but different checksums — likely
the same photo at different resolutions, with minor crops, or colour adjustments.
<strong>Review each group</strong>: click "Merge" to combine into one record, or "Split" to keep as separate photos.</p>
""")

    for i, group in enumerate(perceptual_groups):
        file_ids_json = json.dumps([entry["file"]["id"] for entry in group["files"]])
        html_parts.append(f'<div class="group" data-group-type="perceptual" data-group-id="perceptual-{i}" data-file-ids=\'{file_ids_json}\' data-decision="merge">')
        html_parts.append(f'<div class="group-header">Group {i+1}')
        html_parts.append(f'<button class="decision-toggle merge" onclick="toggleDecision(this, \'perceptual-{i}\')">Merge</button>')
        html_parts.append('</div>')

        for entry in group["files"]:
            f = entry["file"]
            is_keeper = f["id"] == group["recommended_keeper"]["id"]
            label = '<span class="keeper">HIGHEST RES</span>' if is_keeper else f'<span class="duplicate">SIMILAR (distance: {entry["distance"]})</span>'
            html_parts.append(f'<div class="file-row">')
            html_parts.append(thumb_img(f, width=150))
            html_parts.append(f'<div class="file-info">{label}<br>{file_info_html(f)}</div>')
            html_parts.append('</div>')

        html_parts.append('</div>')

    html_parts.append('</div>')  # close section-content for perceptual

    # Section 3: Filename pattern duplicates
    html_parts.append(f"""
<h2 class="collapsible" onclick="toggleSection(this)">3. Filename Variant Groups ({len(filename_groups)} groups) <span class="collapse-icon">&#9660;</span></h2>
<div class="section-content">
<p>These files have names suggesting they are edited/resized versions of each other
(e.g. photo.jpg / photo_final.jpg / photo (1).jpg).
<strong>Review each group</strong>: click "Merge" to combine into one record, or "Split" to keep as separate photos.</p>
""")

    for i, group in enumerate(filename_groups):
        file_ids_json = json.dumps([f["id"] for f in group["all_files"]])
        html_parts.append(f'<div class="group" data-group-type="filename" data-group-id="filename-{i}" data-file-ids=\'{file_ids_json}\' data-decision="merge">')
        html_parts.append(f'<div class="group-header">Group {i+1} — Base: {group["base_name"]}')
        html_parts.append(f'<button class="decision-toggle merge" onclick="toggleDecision(this, \'filename-{i}\')">Merge</button>')
        html_parts.append('</div>')

        for f in group["all_files"]:
            html_parts.append(f'<div class="file-row">')
            html_parts.append(thumb_img(f))
            html_parts.append(f'<div class="file-info">{file_info_html(f)}</div>')
            html_parts.append('</div>')

        html_parts.append('</div>')

    html_parts.append('</div>')  # close section-content for filename variants

    # Section 4: Flagged logos/design
    html_parts.append(f"""
<h2 class="collapsible" onclick="toggleSection(this)">4. Flagged as Logos/Design Material ({len(flagged_logos)} files) <span class="collapse-icon">&#9660;</span></h2>
<div class="section-content">
<p>These files were flagged based on small dimensions, folder names, or file type.
<strong>Review each file</strong>: click to toggle between "Include" (keep in library) and "Exclude" (remove).</p>
""")

    for j, f in enumerate(flagged_logos):
        flags_html = " ".join(f'<span class="flag">{fl}</span>' for fl in f["flags"])
        html_parts.append(f'<div class="group" data-group-type="flagged" data-group-id="flagged-{j}" data-file-ids=\'["{f["id"]}"]\' data-decision="exclude">')
        html_parts.append(f'<div class="file-row">')
        html_parts.append(thumb_img(f))
        html_parts.append(f'<div class="file-info">{file_info_html(f)}<br>{flags_html}<br>')
        html_parts.append(f'<button class="decision-toggle exclude" onclick="toggleFlagged(this, \'flagged-{j}\')">Exclude</button>')
        html_parts.append('</div></div>')
        html_parts.append('</div>')

    html_parts.append('</div>')

    # Section 5: Clean files
    html_parts.append(f"""
<h2 class="collapsible" onclick="toggleSection(this)">5. Clean Files ({len(clean_files)} files — no action needed) <span class="collapse-icon">&#9654;</span></h2>
<div class="section-content" style="display:none">
<p>These files passed all checks and are not involved in any duplicate group.</p>
<table>
<tr><th>Name</th><th>Size</th><th>Dimensions</th><th>Folder</th><th>Campaign</th></tr>
""")

    for f in sorted(clean_files, key=lambda x: x["folder_path"]):
        dims = f"{f['width']}x{f['height']}" if f.get("width") else ""
        campaign = f.get("inferred_campaign") or ""
        html_parts.append(
            f'<tr><td>{f["name"]}</td><td>{format_file_size(f["size"])}</td>'
            f'<td>{dims}</td><td style="font-size:12px;">{f["folder_path"]}</td>'
            f'<td>{campaign}</td></tr>'
        )

    html_parts.append('</table>')

    html_parts.append('</div>')  # close section-content for clean files

    # Section 6: Campaign inference preview
    html_parts.append(f"""
<h2 class="collapsible" onclick="toggleSection(this)">6. Campaign Inference Preview ({len(campaigns)} campaigns) <span class="collapse-icon">&#9660;</span></h2>
<div class="section-content">
<p>Campaign names inferred from folder paths. Review for accuracy.</p>
<ul class="campaign-list">
""")

    for campaign, camp_files in sorted(campaigns.items(), key=lambda x: -len(x[1])):
        html_parts.append(f'<li><strong>{campaign}</strong>: {len(camp_files)} files</li>')

    html_parts.append('</ul>')
    html_parts.append('</div>')  # close section-content for campaigns

    html_parts.append("""
<hr>
<p style="color:#666;font-size:13px;">Review the groups above, then click <strong>Export Decisions</strong> at the top.
Save the downloaded file as <code>data/review_decisions.json</code> in your project folder.</p>

<script>
function toggleDecision(btn, groupId) {
    const group = document.querySelector(`[data-group-id="${groupId}"]`);
    const current = group.dataset.decision;
    const next = current === 'merge' ? 'split' : 'merge';
    group.dataset.decision = next;
    btn.textContent = next.charAt(0).toUpperCase() + next.slice(1);
    btn.className = 'decision-toggle ' + next;
}

function toggleFlagged(btn, groupId) {
    const group = document.querySelector(`[data-group-id="${groupId}"]`);
    const current = group.dataset.decision;
    const next = current === 'exclude' ? 'include' : 'exclude';
    group.dataset.decision = next;
    btn.textContent = next.charAt(0).toUpperCase() + next.slice(1);
    btn.className = 'decision-toggle ' + next;
}

function toggleSection(header) {
    const content = header.nextElementSibling;
    const icon = header.querySelector('.collapse-icon');
    if (content.style.display === 'none') {
        content.style.display = '';
        icon.innerHTML = '&#9660;';
    } else {
        content.style.display = 'none';
        icon.innerHTML = '&#9654;';
    }
}

function exportDecisions() {
    const groups = document.querySelectorAll('[data-group-id]');
    const decisions = {
        merge_groups: [],
        split_groups: [],
        exclude_files: [],
        include_files: [],
        exported_at: new Date().toISOString()
    };

    groups.forEach(group => {
        const type = group.dataset.groupType;
        const id = group.dataset.groupId;
        const fileIds = JSON.parse(group.dataset.fileIds);
        const decision = group.dataset.decision;

        if (type === 'flagged') {
            if (decision === 'exclude') {
                decisions.exclude_files.push(...fileIds);
            } else {
                decisions.include_files.push(...fileIds);
            }
        } else {
            if (decision === 'merge') {
                decisions.merge_groups.push({ group_id: id, type: type, file_ids: fileIds });
            } else {
                decisions.split_groups.push({ group_id: id, type: type, file_ids: fileIds });
            }
        }
    });

    const blob = new Blob([JSON.stringify(decisions, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'review_decisions.json';
    a.click();
    URL.revokeObjectURL(url);

    document.getElementById('review-status').textContent =
        `Exported! ${decisions.merge_groups.length} merge, ${decisions.split_groups.length} split, ` +
        `${decisions.exclude_files.length} exclude, ${decisions.include_files.length} include.`;
}
</script>
</body>
</html>""")

    return "".join(html_parts)


def main():
    report = load_scan_report()
    files = report["files"]
    print(f"Loaded {len(files)} files from scan report", flush=True)

    # Pass 1: Exact duplicates (no downloads needed)
    print("\nPass 1: Checking exact duplicates by MD5...", flush=True)
    exact_groups = pass1_exact_duplicates(files)
    exact_dup_ids = set()
    for g in exact_groups:
        for d in g["duplicates"]:
            exact_dup_ids.add(d["id"])
    print(f"  Found {len(exact_groups)} groups ({len(exact_dup_ids)} redundant files)")

    # Pass 2: Perceptual duplicates (needs thumbnail downloads)
    print("\nPass 2: Checking perceptual near-duplicates...", flush=True)
    service = build_drive_service()
    perceptual_groups = pass2_perceptual_duplicates(files, exact_dup_ids, service)

    # Pass 3: Filename pattern duplicates
    perceptual_dup_ids = set()
    for g in perceptual_groups:
        for entry in g["files"]:
            perceptual_dup_ids.add(entry["file"]["id"])
    already_flagged = exact_dup_ids | perceptual_dup_ids

    print("\nPass 3: Checking filename pattern duplicates...", flush=True)
    filename_groups = pass3_filename_duplicates(files, already_flagged)
    print(f"  Found {len(filename_groups)} filename variant groups")

    # Flagged logos
    flagged_logos = get_flagged_logos(files)
    print(f"\nFlagged as logos/design: {len(flagged_logos)} files")

    # Generate HTML report
    print("\nGenerating HTML report...", flush=True)
    html = generate_html_report(
        files=files,
        exact_groups=exact_groups,
        perceptual_groups=perceptual_groups,
        filename_groups=filename_groups,
        flagged_logos=flagged_logos,
        scan_date=report.get("scan_date", "unknown"),
    )

    report_path = DATA_DIR / "dedup_report.html"
    with open(report_path, "w") as f:
        f.write(html)

    # Also save structured data for programmatic use
    structured = {
        "exact_duplicate_groups": len(exact_groups),
        "exact_redundant_files": len(exact_dup_ids),
        "perceptual_duplicate_groups": len(perceptual_groups),
        "filename_variant_groups": len(filename_groups),
        "flagged_logos": len(flagged_logos),
        "exact_groups_detail": [
            {
                "keeper_id": g["keeper"]["id"],
                "keeper_name": g["keeper"]["name"],
                "duplicate_ids": [d["id"] for d in g["duplicates"]],
                "all_campaigns": g["all_campaigns"],
            }
            for g in exact_groups
        ],
    }
    with open(DATA_DIR / "dedup_summary.json", "w") as f:
        json.dump(structured, f, indent=2)

    print(f"\nReport saved to: {report_path}")
    print("Open it in your browser to review.")
    print("\nAfter review, the next step is Phase 2: python scripts/download_photos.py")


if __name__ == "__main__":
    main()
