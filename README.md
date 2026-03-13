# Possible Photo Library

A searchable, password-protected photo library for [Possible](https://www.wearepossible.org/). Browse 1800+ campaign photos with AI-generated keywords, descriptions, and alt text.

Photos live on a shared Google Drive. This library provides a browsable, searchable interface — Google Drive remains the source of truth for originals.

## How it works

- **Search** by keyword, description, campaign, credit, or folder path — results are relevancy-ranked
- **Browse** a grid of thumbnails with infinite scroll
- **View** full metadata, Drive locations, and similar photos in a detail modal
- **Edit** keywords, descriptions, alt text, campaigns, and credits inline
- **Sync** with Google Drive to pick up new uploads

## Stack

| Layer | Service |
|-------|---------|
| Frontend | Vanilla HTML/CSS/JS on [Netlify](https://www.netlify.com/) |
| Thumbnails + data | [Cloudflare R2](https://www.cloudflare.com/r2/) |
| Image originals | Google Drive (Shared Drive) |
| Backend functions | Netlify Functions |
| Image analysis | Claude API (Haiku 4.5) |

Running cost: **$0/month** (all free tiers).

## Setup

### Prerequisites

- Python 3.10+
- Node.js 18+
- A Google Cloud service account with access to the Shared Drive
- A Cloudflare R2 bucket
- A Netlify site linked to this repo

### Local development

```bash
# Install Python dependencies
pip install -r requirements.txt

# Copy .env.example to .env and fill in credentials
cp .env.example .env

# Run the local dev server
python3 -m http.server 8080 --directory site
```

### Initial data pipeline

Run these scripts in order:

```bash
# 1. Scan Google Drive and catalogue all images
python scripts/scan_drive.py

# 2. Deduplicate and filter (produces HTML review report)
python scripts/dedup_and_filter.py

# 3. Download images and generate thumbnails
python scripts/download_and_process.py

# 4. Analyse photos with Claude API
python scripts/analyse_photos.py

# 5. Upload thumbnails and data to R2
python scripts/upload_to_r2.py
```

### Environment variables

See [CLAUDE.md](CLAUDE.md) for the full list of environment variables needed for local scripts and Netlify Functions.

### Deployment

Push to `main` — Netlify auto-deploys from the `site/` directory. Set all environment variables in the Netlify dashboard.

## Architecture decisions

- **No build step** — vanilla HTML/CSS/JS keeps it simple for a solo maintainer
- **R2 for data** — `data.json` lives in R2 (not Git) because it's updated by sync functions and metadata edits
- **Thumbnails in R2** — Drive API thumbnail links require auth, so we host our own
- **Client-side password** — not real security, just a deterrent for casual access. Server-side validation on all Netlify Functions
- **AI metadata is editable** — Claude generates initial keywords/descriptions, but anyone with the password can overwrite them

## Brand

Built to match [Possible's visual identity](https://www.wearepossible.org/):

- **Deep purple** `#321D49`
- **Magenta** `#BF0978`
- **Poppins** font
- No rounded corners
