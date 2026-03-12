# CLAUDE.md — Photo Library

## Project Overview

Building a searchable, password-protected photo library for a climate charity. Photos are currently scattered across a shared Google Drive. The goal is to consolidate them into a single browsable archive with AI-generated keyword tags, campaign associations inferred from folder structure, and an admin interface that multiple non-technical people can use to upload new photos.

## Architecture

- **Static site**: HTML/JS single-page app hosted on Netlify (free tier)
- **Data**: `data.json` stored in Cloudflare R2 (not in Git — because multiple users can add photos via the web admin, and Netlify Functions can't push to a repo)
- **Image hosting**: Cloudflare R2 (free tier, 10GB storage, zero egress)
- **Thumbnails**: Generated during upload/migration, stored alongside originals in R2
- **Upload backend**: Netlify Functions (free tier: 125k invocations/month, 10-second timeout per invocation) — handles presigned URL generation, metadata writes, and triggers image analysis
- **Image analysis (bulk migration)**: Claude API (Haiku 4.5) — ~$1 for 300 images, or likely free with the $5 new-account credit
- **Image analysis (ongoing uploads)**: Cloudflare Workers AI (free tier, 10k neurons/day) using a vision model (e.g. `llama-3.2-11b-vision-instruct` or `uform-gen2-qwen-500m`)
- **Password protection**: Client-side JS gate (a simple login page that checks a hardcoded password before revealing content — not real security, just a deterrent for casual access)

## Data Model

Each photo record in `data.json`:

```json
{
  "id": "unique-id",
  "filename": "clean-slug.jpg",
  "original_filename": "IMG_20240315_142356.jpg",
  "thumbnail_url": "https://r2-bucket.example.com/thumbnails/clean-slug.jpg",
  "full_url": "https://r2-bucket.example.com/photos/clean-slug.jpg",
  "keywords": ["solar panels", "rooftop", "residential"],
  "description": "Solar panel installation on the Jönsson house in Malmö, funded by the 2024 grant.",
  "alt_text": "Solar panels arranged in rows on a sloped residential roof",
  "ai_keywords": ["solar panels", "rooftop", "residential", "installation", "blue sky", "suburban"],
  "ai_description": "Rooftop solar panel installation on a residential building against a clear sky",
  "campaign": "Clean Energy 2024",
  "source_folder": "Campaign Assets/Clean Energy 2024/Photos",
  "credit": "Jane Smith",
  "date_taken": "2024-03-15",
  "date_added": "2025-06-01",
  "width": 4032,
  "height": 3024,
  "file_size_bytes": 3456789,
  "added_by": ""
}
```

The human-authored fields (`keywords`, `description`, `alt_text`) are displayed by default. The AI-generated fields (`ai_keywords`, `ai_description`) are stored separately and are searchable but not prominently displayed — they serve as a fallback to ensure photos are findable even when human metadata is sparse. For migration-era photos where no human was present, the AI fields may be the only populated ones — in that case the AI description is promoted to the display description.

## Migration Plan

### Phase 1: Scan Google Drive

- Use the Google Drive API to recursively scan the Shared Drive
- Catalogue every image file (JPEG, PNG, TIFF, WEBP) — record file ID, name, path, size, modified date, `md5Checksum` (returned by the API), and any EXIF metadata
- Run the deduplication and filtering pipeline (see below)
- Produce a review report for Duncan before proceeding to download

**Filtering strategy for logos vs photos:**
- Flag files with very small dimensions (e.g. < 200px on any side) — likely icons/logos
- Flag files in folders whose names contain "logo", "brand", "icon", "assets", "design", "template"
- Flag PNG files with transparency (alpha channel) — more likely designed material than photos
- Flag SVG files — always designed, never photos
- Present flagged files for manual review rather than auto-excluding — false positives are likely

**Deduplication strategy (multi-pass):**

The shared drive will likely contain duplicates — the same photo in multiple folders, resized copies, and filename variants. Handle this in three passes, producing a review report rather than auto-deleting anything.

*Pass 1 — Exact duplicates (by md5Checksum):*
- The Google Drive API returns `md5Checksum` for every file — no download needed
- Group files with identical hashes
- For each group, designate the "best" copy as the keeper: prefer the one deepest in a campaign folder, or with the most informative filename
- **Crucially, merge folder paths from all copies into the keeper's metadata** — if the same photo appears in "Clean Air Campaign" and "Annual Report 2023", both campaign associations are preserved even though only one copy of the image is kept
- Flag duplicates as auto-resolvable in the report (safe to keep just the best copy)

*Pass 2 — Near-duplicates (perceptual hash):*
- Download thumbnails only (use the Drive API's `thumbnailLink` or export at reduced resolution — avoids downloading full-resolution files at this stage)
- Compare using perceptual hashing (`imagehash` Python library, e.g. `phash` with a distance threshold of ~8)
- Two images with very similar perceptual hashes but different md5 checksums are likely the same photo at different resolutions, with minor crops, or with colour adjustments
- Flag these as probable duplicates for manual review, noting which is the highest resolution
- Present side-by-side in the report with file sizes and paths

*Pass 3 — Filename pattern duplicates:*
- Flag files that look like edited versions of each other using common patterns:
  - `photo.jpg` / `photo (1).jpg` / `photo (2).jpg` (Drive's auto-rename on duplicate upload)
  - `photo.jpg` / `photo_final.jpg` / `photo_v2.jpg` / `photo-edited.jpg`
  - `photo.jpg` / `photo_small.jpg` / `photo_thumb.jpg` / `photo_web.jpg`
- Group these by base name and present for manual review

**The Phase 1 report should contain:**
1. Summary stats: total files found, total size, breakdown by file type and folder
2. Exact duplicates: groups with auto-recommended keeper (safe to auto-resolve)
3. Probable near-duplicates: side-by-side with resolution and size (needs quick human review)
4. Filename pattern duplicates: grouped by base name (needs quick human review)
5. Files flagged as logos/designed material (needs human review)
6. Clean list: files that passed all checks, ready to download
7. Campaign inference preview: what campaign each file would be assigned to based on its folder path

Duncan reviews the report (~20-30 minutes), adjusts any auto-resolutions, and approves the final list before Phase 2 begins.

### Phase 2: Download and organise

- Download all non-excluded images from Google Drive
- Preserve the folder path as metadata (stored in `source_folder` field)
- Extract EXIF data where available: date taken, camera model, GPS coordinates (for date/location metadata, not displayed publicly)
- Extract any credit/author info from EXIF Artist or Copyright fields
- Attempt to infer campaign from folder path (e.g. if path contains a known campaign name, associate it)
- Generate a clean slug for each filename based on: the original filename if it's human-readable, or the folder name + a sequence number if the original is something like `IMG_20240315_142356.jpg`

### Phase 3: Analyse images for keywords

This is the **one-time bulk analysis** using the Claude API. Since these existing photos won't have a human present to describe them, the AI output needs to be more detailed and thorough than for ongoing uploads.

- Send each image to the Claude API (Haiku 4.5) with a prompt like:
  ```
  Analyse this photo for a climate charity's photo library.
  Return a JSON object with:
  - "keywords": an array of 10-20 descriptive keywords/tags (include objects, settings, activities, weather, mood, environmental context — be specific to climate/environment topics where relevant)
  - "description": 2-3 sentences describing the photo in detail
  - "alt_text": accessible alt text for screen readers (1 sentence)
  ```
- Save the AI output alongside existing metadata
- Generate thumbnails (e.g. 400px wide) using Pillow or similar
- Allow Duncan to review and bulk-edit the AI-generated metadata before publishing

### Phase 4: Upload to R2

- Create an R2 bucket with public read access via a custom domain or Cloudflare public bucket URL
- Upload all full-resolution photos to `photos/` prefix
- Upload all thumbnails to `thumbnails/` prefix
- Upload `data.json` to the bucket root
- Set correct content-types on all files
- Configure CORS headers on the R2 bucket to allow the Netlify-hosted site to fetch `data.json` and images

### Phase 5: Build the browsing interface

- Single-page app that fetches `data.json` from R2 on load
- Password gate: on first visit, show a simple password input; store the password in sessionStorage so it persists for the browser session but not permanently
- Universal search: single text input that searches across all fields — human-authored keywords/description, AI-generated keywords/description, campaign, credit, source folder, and original filename
- Results displayed as a thumbnail grid, ranked by relevancy
- Click a thumbnail to see full-resolution image, all metadata, and a download link
- Mobile-friendly responsive layout

### Phase 6: Build the admin interface

- A separate `/admin` page on the same Netlify site, behind the same password gate
- **Upload flow:**
  1. User fills in a form: selects a photo file, and provides **their own description, keywords, campaign, and credit** — these human-authored fields are the primary metadata
  2. Browser requests a presigned R2 upload URL from a Netlify Function
  3. Browser uploads the photo directly to R2 (bypasses Netlify's 6MB body limit — R2 accepts up to 5GB)
  4. Browser sends the R2 URL to a second Netlify Function, which calls Cloudflare Workers AI to generate **fallback** keywords and a suggested alt text
  5. The AI suggestions are shown below the user's own input — the user can pull in any AI suggestions they like, ignore them, or edit them. The AI output is stored separately as `ai_keywords` / `ai_description` so that even if the user ignores it, the fallback data is still searchable.
  6. User clicks save; a Netlify Function reads `data.json` from R2, appends the new record, and writes it back
- **Description philosophy:** For ongoing uploads, descriptions and keywords should be **primarily human-authored**. The AI provides a fallback that ensures photos are still findable even if a user provides minimal metadata. Both the human and AI fields are searchable. The admin interface should make it easy and inviting to write a good description (placeholder text, character count, etc.) rather than defaulting to "just let the AI do it".
- **Server-side validation:** Every Netlify Function must also check the password (sent as a header or in the request body) — the client-side gate alone is not sufficient, since anyone who discovers the function URLs could call them directly
- **Edit/delete flow:**
  - Admin page also shows existing records with an edit button
  - Editing updates the record in `data.json` in R2
  - Deletion removes the record from `data.json` and optionally deletes the image from R2
- **Bulk upload:**
  - Option to upload multiple photos at once
  - Each is analysed sequentially (to stay within Workers AI free tier limits)
  - User reviews all suggestions before saving

### Phase 7: Campaign mapping

- Maintain a `campaigns.json` file (also in R2) listing known campaign names
- During migration, infer campaign from folder paths
- In the admin interface, campaign is a dropdown populated from `campaigns.json`, with an option to add new campaigns
- This keeps campaign names consistent across the library

## Tech Stack

- **Migration scripts**: Python (google-api-python-client for Drive, anthropic SDK for Claude, Pillow for thumbnails, imagehash for perceptual deduplication, boto3 for R2)
- **Static site**: Vanilla HTML/CSS/JS (no build step — keep it simple and long-lived)
- **Backend functions**: Netlify Functions (JavaScript/Node.js)
- **Image hosting**: Cloudflare R2 via S3-compatible API
- **Image analysis (migration)**: Claude API, Haiku 4.5 ($1/M input, $5/M output tokens)
- **Image analysis (ongoing)**: Cloudflare Workers AI (free tier)
- **Deployment**: Netlify auto-deploy from GitHub

## Key Constraints

- Running cost must be strictly zero after the initial migration (the one-time Claude API cost of ~$1 for bulk analysis is acceptable)
- Prefer established, long-lived services (Cloudflare, Netlify, Google)
- Multiple non-technical users need to be able to upload and edit — the admin interface must be intuitive
- Password protection is a soft gate (client-side JS), not enterprise-grade auth — acceptable for a low-sensitivity internal photo archive
- Duncan is the sole technical maintainer — keep the stack simple

## Environment Variables

### For migration scripts (local `.env`)
- `GOOGLE_SERVICE_ACCOUNT_KEY` — path to Google service account JSON key file (for Drive API access)
- `ANTHROPIC_API_KEY` — Claude API key for bulk image analysis
- `R2_ACCOUNT_ID` — Cloudflare account ID
- `R2_ACCESS_KEY_ID` — R2 API access key
- `R2_SECRET_ACCESS_KEY` — R2 API secret key
- `R2_BUCKET_NAME` — R2 bucket name

### For Netlify Functions (set in Netlify dashboard)
- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`
- `R2_PUBLIC_URL` — public base URL for the R2 bucket
- `CF_ACCOUNT_ID` — Cloudflare account ID (for Workers AI)
- `CF_API_TOKEN` — Cloudflare API token (for Workers AI)
- `ADMIN_PASSWORD` — the password for the client-side gate (also used by functions to validate requests)
- `SHARED_DRIVE_ID` — ID of the Google Workspace Shared Drive (visible in the URL when viewing the drive root)

## File Structure (Target)

```
photo-library/
├── CLAUDE.md
├── .env                          # Local migration keys (gitignored)
├── scripts/
│   ├── scan_drive.py             # Phase 1: scan Shared Drive, catalogue all images
│   ├── dedup_and_filter.py       # Phase 1: run dedup passes + logo filtering, produce report
│   ├── download_photos.py        # Phase 2: download approved photos, extract EXIF
│   ├── analyse_photos.py         # Phase 3: Claude API keyword generation
│   ├── generate_thumbnails.py    # Phase 3: create thumbnail versions
│   ├── upload_to_r2.py           # Phase 4: upload photos + thumbnails + data.json
│   └── utils.py                  # Shared helpers (slug generation, EXIF extraction, perceptual hashing, etc.)
├── site/
│   ├── index.html                # Browsing interface (password-gated)
│   ├── admin.html                # Admin upload/edit interface
│   ├── style.css
│   ├── app.js                    # Search and gallery logic
│   └── admin.js                  # Upload form, AI analysis review, edit/delete
├── netlify/
│   └── functions/
│       ├── get-upload-url.js     # Returns a presigned R2 upload URL
│       ├── analyse-photo.js      # Calls Cloudflare Workers AI, returns suggestions
│       ├── save-record.js        # Reads data.json from R2, appends record, writes back
│       ├── update-record.js      # Updates an existing record in data.json
│       └── delete-record.js      # Removes record from data.json (and optionally image from R2)
├── data/
│   ├── drive_scan_report.json    # Phase 1 full scan results (intermediate)
│   ├── dedup_report.html         # Phase 1 human-readable dedup review report
│   ├── approved_files.json       # Phase 1 output: files approved for download after review
│   ├── exclusion_list.json       # Files to exclude (manually reviewed)
│   └── raw_metadata.json         # Downloaded metadata before AI analysis (intermediate)
└── netlify.toml                  # Netlify config (functions directory, redirects)
```

**In R2 bucket:**
```
photos/
  ├── rooftop-solar-installation.jpg
  ├── cycling-infrastructure-copenhagen.jpg
  └── ...
thumbnails/
  ├── rooftop-solar-installation.jpg
  ├── cycling-infrastructure-copenhagen.jpg
  └── ...
data.json
campaigns.json
```

## Potential Roadblocks and Difficulties

### 1. Google Drive API complexity
The Drive API requires OAuth2 or a service account. For a one-time migration script, a service account is simplest — but it needs to be explicitly granted access to the shared drive. The photos are in a **Google Workspace Shared Drive** (not a regular shared folder) — the script **must** use `supportsAllDrives=True`, `includeItemsFromAllDrives=True`, and the `driveId` parameter on all API calls, or it will silently return zero results. Pagination is required for large folder trees (100 files per page by default). Rate limits are generous (20,000 queries/day) but the script should still include backoff logic.

### 2. Distinguishing photos from logos/designed material
There is no perfect automated solution. The filtering heuristics (dimensions, folder names, transparency, file type) will catch most cases, but manual review of flagged files is essential. Expect 15-30 minutes of manual review time. Consider erring on the side of inclusion — it's easier to delete a logo from the library later than to discover a photo was missed.

### 3. Concurrent writes to data.json
If two people upload photos at the same time, both Netlify Functions will read `data.json` from R2, append their record, and write it back — the second write will overwrite the first. For a low-traffic archive this is unlikely, but it could happen. Mitigations:
- Use R2's `onlyIf` conditional headers (ETag-based optimistic locking) to detect conflicts
- If a conflict is detected, re-read and retry
- This adds complexity but is worth implementing to avoid silent data loss

### 4. Netlify Functions 10-second timeout
The presigned URL generation and data.json updates are fast. The Cloudflare Workers AI call for image analysis is the riskiest — if the model is slow, it could time out. Mitigations:
- Use a smaller/faster model like `uform-gen2-qwen-500m` for ongoing uploads (faster but less capable)
- If timeout is hit, return partial results and let the user enter keywords manually
- Consider the 26-second timeout available on Netlify's paid Background Functions — but this costs money, so try the free tier first

### 5. Cloudflare Workers AI model quality
The free-tier vision models (uform-gen2-qwen-500m, resnet-50) are significantly less capable than Claude for understanding climate-specific content. Keywords may be generic ("building", "sky") rather than specific ("solar panels", "air quality monitoring station"). This is less of a concern now that AI descriptions are positioned as a fallback:
- Human-authored descriptions are the primary metadata for ongoing uploads
- AI keywords still help with search discoverability even if they're generic
- The Claude API bulk analysis in the migration will set a high baseline for existing photos, where no human is available to describe them
- If AI quality proves too low to be useful even as a fallback, it can be removed without affecting the core workflow

### 6. R2 CORS configuration
The Netlify site (e.g. photos.example.com) needs to fetch `data.json` and images from the R2 bucket (e.g. r2.example.com). This requires CORS headers on the R2 bucket. Cloudflare R2 supports CORS rules but they must be configured explicitly — this is often forgotten and causes confusing "blocked by CORS policy" errors.

### 7. Client-side password is not secure
Anyone who views the page source can find the password or bypass the check. This is acceptable for a low-sensitivity internal archive, but everyone involved should understand this is a convenience gate, not real access control. If real security is ever needed, Cloudflare Access (free for up to 50 users) can be added later without changing the site architecture.

### 8. Thumbnail generation at upload time
During migration, thumbnails are generated locally with Pillow — easy. For ongoing uploads via the web admin, thumbnails need to be generated server-side. Options:
- Generate in a Netlify Function (requires an image processing library — possible but adds cold-start time)
- Generate client-side in the browser using Canvas API before uploading (simpler, no server dependency)
- Client-side generation is recommended: resize to 400px wide in a `<canvas>`, convert to JPEG, upload alongside the original

### 9. Photo file sizes
Some photos may be very large (10MB+ RAW exports, high-res DSLR shots). The presigned URL upload to R2 handles this fine (up to 5GB), but loading many large thumbnails could still be slow. Ensure thumbnails are aggressively compressed (JPEG quality 70-80, max 400px wide).

### 10. Google Drive may not have a clean folder structure
Campaign inference from folder paths depends on folders being named sensibly. If the Drive is chaotic (nested duplicates, ambiguous names, photos in root), the automatic campaign mapping may not work well. The migration script produces a clear report of inferred campaigns for manual review. The dedup pipeline handles the most common sources of messiness (duplicate uploads, resized copies, renamed variants), but truly chaotic folder structures may require more manual triage time.

### 11. Perceptual hash threshold tuning
The perceptual hash distance threshold for near-duplicate detection (default ~8) may need tuning. Too low and you'll miss resized copies; too high and you'll flag genuinely different photos as duplicates. The dedup report shows all flagged pairs for manual review, so false positives are caught — but if the number of flagged pairs is overwhelming, adjust the threshold up to reduce noise.

## Notes

- **Key architectural difference from the dataviz library:** In the dataviz project, `data.json` lives in Git and deploys with the site. Here, `data.json` lives in R2 because multiple users can add photos via the web admin without touching Git. This means data changes are live immediately (no deploy needed), but the site depends on R2 being reachable. If R2 is unreachable, the site shows nothing — an acceptable tradeoff for zero hosting cost.
- data.json will grow over time. At ~500 bytes per record and 300-500 photos, it will be ~150-250KB — small enough to fetch on every page load without caching concerns.
- Slug generation should handle collisions by appending a numeric suffix, same as the dataviz library.
- The admin interface should prevent uploading duplicate photos — check file hash (MD5) against existing records before accepting. For near-duplicates on ongoing uploads, a simple file-size + dimensions check is sufficient; full perceptual hashing is only needed during the migration.
- The Phase 1 dedup report (`dedup_report.html`) should be a self-contained HTML file that can be opened in a browser — show thumbnail previews side-by-side for near-duplicates, with checkboxes to approve/reject. This makes the 20-30 minute review session as painless as possible.
- EXIF data may contain GPS coordinates. These should be stored in metadata for internal reference but NEVER displayed publicly, for privacy reasons.
- The password should be shared with org members via existing internal channels (email, Slack, etc.), not embedded in any public documentation.
