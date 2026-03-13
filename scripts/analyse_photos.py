"""Phase 2b: Analyse photos with Claude API to generate keywords and descriptions.

Sends each downloaded image to Claude (Haiku 4.5) for analysis.
Only analyses photos that don't already have keywords.

Usage:
    python scripts/analyse_photos.py

Inputs:
    data/data.json            — from download_and_process.py
    data/thumbnails/          — JPEG thumbnails

Outputs:
    data/data.json            — updated with keywords, description, alt_text
"""

import base64
import json
import time
from pathlib import Path

import anthropic

from utils import get_env

DATA_DIR = Path(__file__).parent.parent / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
THUMBNAILS_DIR = DATA_DIR / "thumbnails"

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


def load_data():
    data_path = DATA_DIR / "data.json"
    with open(data_path) as f:
        return json.load(f)


def save_data(records):
    data_path = DATA_DIR / "data.json"
    with open(data_path, "w") as f:
        json.dump(records, f, indent=2)


def get_image_base64(slug: str) -> tuple[str, str] | None:
    """Find and encode a thumbnail image as base64.

    Uses thumbnails (always JPEG) instead of originals — smaller, faster,
    and avoids unsupported formats like HEIC.
    """
    thumb_path = THUMBNAILS_DIR / f"{slug}.jpg"
    if thumb_path.exists():
        data = base64.standard_b64encode(thumb_path.read_bytes()).decode("utf-8")
        return data, "image/jpeg"
    return None


def analyse_image(client: anthropic.Anthropic, image_b64: str, media_type: str) -> dict | None:
    """Send an image to Claude for analysis."""
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": ANALYSIS_PROMPT,
                        },
                    ],
                },
            ],
        )

        # Parse the response
        text = response.content[0].text.strip()
        # Handle potential markdown code block wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        result = json.loads(text)
        return result

    except json.JSONDecodeError as e:
        print(f"    Failed to parse JSON response: {e}", flush=True)
        return None
    except anthropic.RateLimitError:
        print("    Rate limited, waiting 60s...", flush=True)
        time.sleep(60)
        return analyse_image(client, image_b64, media_type)  # Retry once
    except anthropic.APIError as e:
        print(f"    API error: {e}", flush=True)
        return None


def main():
    records = load_data()
    print(f"Loaded {len(records)} records", flush=True)

    client = anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))

    # Collect all items to analyse (primaries + alternatives)
    tasks = []
    for i, record in enumerate(records):
        if not record.get("keywords"):
            slug = record["id"]
            tasks.append({"record_index": i, "slug": slug, "is_alternative": False})

    print(f"Photos needing analysis: {len(tasks)}", flush=True)

    if not tasks:
        print("All photos already analysed. Nothing to do.")
        return

    # Estimate cost
    # Haiku 4.5: ~$0.80/M input tokens, ~$4/M output tokens
    # Each image is ~1000 tokens input, ~200 tokens output
    est_input_cost = len(tasks) * 1000 * 0.80 / 1_000_000
    est_output_cost = len(tasks) * 200 * 4.0 / 1_000_000
    est_total = est_input_cost + est_output_cost
    print(f"Estimated API cost: ${est_total:.2f}", flush=True)

    success = 0
    failed = 0
    skipped = 0

    for task_num, task in enumerate(tasks):
        record = records[task["record_index"]]
        slug = task["slug"]

        print(f"  [{task_num+1}/{len(tasks)}] {record['original_filename']}...", end=" ", flush=True)

        # Load image (use thumbnail — always JPEG, smaller)
        img_data = get_image_base64(slug)
        if img_data is None:
            print("SKIP (no download)", flush=True)
            skipped += 1
            continue

        image_b64, media_type = img_data

        # Analyse
        result = analyse_image(client, image_b64, media_type)
        if result is None:
            print("FAILED", flush=True)
            failed += 1
            continue

        # Update record — write directly to keywords/description (no separate ai_ fields)
        record["keywords"] = result.get("keywords", [])
        if not record.get("description"):
            record["description"] = result.get("description", "")
        if not record.get("alt_text"):
            record["alt_text"] = result.get("alt_text", "")

        print(f"OK ({len(record['keywords'])} keywords)", flush=True)
        success += 1

        # Save periodically (every 25 photos) in case of interruption
        if (task_num + 1) % 25 == 0:
            save_data(records)
            print(f"  [checkpoint saved at {task_num+1}]", flush=True)

        # Rate limiting — small delay between requests
        time.sleep(0.2)

    # Final save
    save_data(records)

    print(f"\n{'='*60}")
    print("ANALYSIS SUMMARY")
    print(f"{'='*60}")
    print(f"Analysed: {success}")
    print(f"Failed: {failed}")
    print(f"Skipped: {skipped}")
    print(f"\ndata.json updated with AI metadata")
    print(f"Next step: python scripts/upload_to_r2.py")


if __name__ == "__main__":
    main()
