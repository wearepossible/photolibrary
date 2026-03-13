"""Clean up AI-generated descriptions and alt text.

Removes overused Claude-isms like "diverse", "vibrant", "appears to be",
"environmental stewardship", "hands-on", etc. Applies regex substitutions
to make descriptions more natural and concise.

Usage:
    python scripts/cleanup_descriptions.py          # dry run (shows changes)
    python scripts/cleanup_descriptions.py --apply   # apply changes to data.json
"""

import json
import re
import sys
from pathlib import Path

DATA_PATH = Path(__file__).parent.parent / "data" / "data.json"

# Each rule is (pattern, replacement). Applied in order.
# Patterns are case-insensitive. Replacement preserves first-letter case where possible.
RULES = [
    # --- Remove filler adjectives before groups of people ---
    (r'\ba diverse group\b', 'a group'),
    (r'\ba diverse team\b', 'a team'),
    (r'\ba diverse crowd\b', 'a crowd'),
    (r'\ba diverse mix\b', 'a mix'),
    (r'\ba diverse collection\b', 'a collection'),
    (r'\bdiverse\b', ''),  # catch remaining uses

    # --- Remove "vibrant" as filler ---
    (r'\bvibrant\b', ''),

    # --- Remove "lush" and "verdant" ---
    (r'\blush green\b', 'green'),
    (r'\blush\b', ''),
    (r'\bverdant\b', 'green'),

    # --- Hedging language (specific patterns first, then general) ---
    (r'\bat what appears to be an\b', 'at an'),
    (r'\bat what appears to be a\b', 'at a'),
    (r'\bin what appears to be an\b', 'in an'),
    (r'\bin what appears to be a\b', 'in a'),
    (r'\bon what appears to be an\b', 'on an'),
    (r'\bon what appears to be a\b', 'on a'),
    (r'\bwhat appears to be an\b', 'an'),
    (r'\bwhat appears to be a\b', 'a'),
    (r'\bappears to be\b', 'is'),
    (r'\bcan be seen\b', 'are visible'),
    (r'\bseems to be\b', 'is'),

    # --- Overused phrases ---
    (r'\benvironmental stewardship\b', 'environmental action'),
    (r'\bhands-on\b', ''),
    (r'\bcollaborative effort\b', 'teamwork'),
    (r'\bcommunity spirit\b', 'community'),
    (r'\ba sense of\b', ''),
    (r'\bengaged in\b', 'doing'),
    (r'\bparticipating in\b', 'at'),
    (r'\bcaptured mid-', 'mid-'),

    # --- Flowery verbs ---
    (r'\bshowcasing\b', 'showing'),
    (r'\bhighlighting\b', 'showing'),
    (r'\bunderscoring\b', 'showing'),
    (r'\bsymbolizing\b', 'representing'),
    (r'\bfostering\b', 'building'),
    (r'\beveryday\b', ''),
    (r'\bconveying\b', 'showing'),
    (r'\bevoking\b', 'suggesting'),
    (r'\bexuding\b', 'showing'),
    (r'\bembodying\b', 'showing'),

    # --- Archaic/flowery words ---
    (r'\bamidst\b', 'among'),
    (r'\bamongst\b', 'among'),
    (r'\badorned with\b', 'with'),
    (r'\bclad in\b', 'wearing'),
    (r'\bdonning\b', 'wearing'),

    # --- Overused adjectives ---
    (r'\bpicturesque\b', ''),
    (r'\bidyllic\b', ''),
    (r'\bquaint\b', ''),
    (r'\bserene\b', 'calm'),
    (r'\bsprawling\b', 'large'),
    (r'\bcaptivating\b', ''),
    (r'\bheartwarming\b', ''),
    (r'\bthought-provoking\b', ''),
    (r'\bpoignant\b', ''),
    (r'\bevocative\b', ''),
    (r'\bwhimsical\b', ''),
    (r'\bcharming\b', ''),
    (r'\bbustling\b', 'busy'),
    (r'\bdedicated\b', ''),
    (r'\bpassionate\b', ''),

    # --- Compound AI-isms ---
    (r'\bmultigenerational\b', 'mixed-age'),
    (r'\bmultifaceted\b', ''),
    (r'\bmulticultural\b', ''),

    # --- "approximately N people" -> "about N people" ---
    (r'\bapproximately (\d+)\b', r'about \1'),

    # --- "of varying ages" ---
    (r'\bof varying ages\b', ''),
    (r'\bof various ages\b', ''),

    # --- Cleanup double spaces, dangling commas, and leading/trailing spaces ---
    (r',\s*,', ','),           # double commas
    (r'  +', ' '),             # double spaces
    (r' ,', ','),              # space before comma
    (r' \.', '.'),             # space before period
    (r'\ba , ', 'a '),         # "a , group" -> "a group"
    (r', ,', ','),             # leftover comma pairs
    (r'(?<=\w), (?=[a-z])', ' '),  # dangling comma after adjective removal: "large, group" -> "large group"
]


def apply_rules(text):
    """Apply all cleanup rules to a text string."""
    if not text:
        return text

    original = text
    for pattern, replacement in RULES:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # Fix case: if replacement left a lowercase letter at start of sentence
    text = re.sub(r'(?:^|(?<=\. ))([a-z])', lambda m: m.group(1).upper(), text)

    # Clean up double spaces again after all transforms
    text = re.sub(r'  +', ' ', text).strip()

    return text


def main():
    apply = '--apply' in sys.argv

    with open(DATA_PATH) as f:
        data = json.load(f)

    changes = 0
    field_changes = {'description': 0, 'alt_text': 0}

    for record in data:
        for field in ['description', 'alt_text']:
            original = record.get(field, '')
            if not original:
                continue

            cleaned = apply_rules(original)
            if cleaned != original:
                changes += 1
                field_changes[field] += 1

                if not apply and changes <= 20:
                    print(f"[{record['id'][:20]}] {field}:")
                    print(f"  BEFORE: {original[:120]}")
                    print(f"  AFTER:  {cleaned[:120]}")
                    print()

                if apply:
                    record[field] = cleaned

    print(f"{'Applied' if apply else 'Would apply'} {changes} changes "
          f"({field_changes['description']} descriptions, {field_changes['alt_text']} alt texts)")

    if apply:
        with open(DATA_PATH, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"Saved to {DATA_PATH}")
        print("Next: re-upload data.json to R2")


if __name__ == "__main__":
    main()
