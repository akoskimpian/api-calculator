import json
import sys
from datetime import datetime, timezone

import requests

PRICES_FILE = "prices.json"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Each target model is found by matching a substring against OpenRouter's
# "id" and "name" fields, not by hardcoding an exact slug. Slugs drift
# (models get renamed, deprecated, replaced) - matching by pattern and
# verifying the result is more durable than a static lookup table.
TARGET_MODELS = [
    {"name": "Claude Haiku 4.5",      "provider": "Anthropic",                        "match": ["claude-haiku-4.5", "claude-haiku-4-5"]},
    {"name": "Claude Sonnet 5",       "provider": "Anthropic, intro thru Aug 31 '26", "match": ["claude-sonnet-5"]},
    {"name": "Claude Opus 4.8",       "provider": "Anthropic",                        "match": ["claude-opus-4.8", "claude-opus-4-8"]},
    {"name": "Claude Fable 5",        "provider": "Anthropic",                        "match": ["claude-fable-5"]},
    {"name": "GPT-4.1 nano",          "provider": "OpenAI",                           "match": ["gpt-4.1-nano"]},
    {"name": "GPT-4.1 mini",          "provider": "OpenAI",                           "match": ["gpt-4.1-mini"]},
    {"name": "GPT-4o",                "provider": "OpenAI",                           "match": ["gpt-4o"], "exclude": ["mini", "nano", "search", "audio", "realtime"]},
    {"name": "GPT-5.4",               "provider": "OpenAI",                           "match": ["gpt-5.4"], "exclude": ["mini", "nano", "chat"]},
    {"name": "GPT-5.5",               "provider": "OpenAI",                           "match": ["gpt-5.5"], "exclude": ["mini", "nano", "chat"]},
    {"name": "Gemini 2.5 Flash-Lite", "provider": "Google",                           "match": ["gemini-2.5-flash-lite"]},
    {"name": "Gemini 3.5 Flash",      "provider": "Google",                           "match": ["gemini-3.5-flash"], "exclude": ["lite"]},
    {"name": "Gemini 3.1 Pro",        "provider": "Google",                           "match": ["gemini-3.1-pro"]},
    {"name": "Grok 4.20",             "provider": "xAI",                              "match": ["grok-4.20"], "exclude": ["multi-agent"]},
    {"name": "Grok 4.3",              "provider": "xAI",                              "match": ["grok-4.3", "grok-4-3"]},
    {"name": "DeepSeek V3.2",         "provider": "DeepSeek",                         "match": ["deepseek-v3.2"]},
    {"name": "Llama 3.3 70B",         "provider": "Meta, via Groq",                   "match": ["llama-3.3-70b"], "explore": "llama-3.3"},
    {"name": "Mistral Small",         "provider": "Mistral AI",                       "match": ["mistral-small"], "exclude": ["24b", "3.1", "22b"]},
]


def load_existing():
    """Last known-good prices, used as a fallback when a live match fails."""
    try:
        with open(PRICES_FILE) as f:
            data = json.load(f)
        return {m["name"]: m for m in data.get("models", [])}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def fetch_catalog():
    response = requests.get(OPENROUTER_MODELS_URL, timeout=20)
    response.raise_for_status()
    return response.json().get("data", [])


def find_match(target, catalog):
    patterns = [p.lower() for p in target["match"]]
    excludes = [e.lower() for e in target.get("exclude", [])]
    candidates = []
    for item in catalog:
        item_id = item.get("id", "")
        # Free-tier variants are rate-limited and priced at 0. Never a
        # useful production price, so they're skipped by default.
        if item_id.lower().endswith(":free"):
            continue
        haystack = f"{item_id} {item.get('name', '')}".lower()
        if any(p in haystack for p in patterns) and not any(e in haystack for e in excludes):
            candidates.append(item)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Multiple hits: prefer the one whose id most tightly matches the first pattern.
    for c in candidates:
        if patterns[0] in c.get("id", "").lower():
            return c
    return candidates[0]


def explore(keyword, catalog, limit=12):
    """When a target has zero matches, list nearby catalog entries so a
    human can spot the real current slug instead of guessing again."""
    keyword = keyword.lower()
    hits = [item.get("id", "") for item in catalog if keyword in item.get("id", "").lower()]
    return hits[:limit]


def main():
    existing = load_existing()

    try:
        catalog = fetch_catalog()
    except requests.RequestException as e:
        print(f"ERROR: could not reach OpenRouter ({e}). Leaving prices.json untouched.")
        sys.exit(1)

    updated_models = []
    warnings = []

    for target in TARGET_MODELS:
        match = find_match(target, catalog)
        fallback = existing.get(target["name"])
        in_cost = out_cost = None
        diagnostic = None

        if match:
            pricing = match.get("pricing", {})
            try:
                in_cost = round(float(pricing.get("prompt", 0)) * 1_000_000, 4)
                out_cost = round(float(pricing.get("completion", 0)) * 1_000_000, 4)
            except (TypeError, ValueError):
                in_cost = out_cost = None
            if not (in_cost and out_cost and in_cost > 0 and out_cost > 0):
                diagnostic = f"matched id='{match.get('id')}' but pricing looked like: {pricing}"
        else:
            nearby = explore(target.get("explore", target["match"][0]), catalog)
            diagnostic = f"no candidate matched. Nearby catalog ids: {nearby}" if nearby else "no candidate matched, and nothing nearby either"

        if in_cost and out_cost and in_cost > 0 and out_cost > 0:
            updated_models.append({
                "name": target["name"],
                "provider": target["provider"],
                "in": in_cost,
                "out": out_cost,
            })
        elif fallback:
            # Couldn't confidently verify a live price. Keep the last known
            # value instead of dropping the model or guessing.
            warnings.append(f"{target['name']}: kept last known price ({diagnostic})")
            updated_models.append(fallback)
        else:
            warnings.append(f"{target['name']}: no match AND no fallback, dropped from site ({diagnostic})")

    payload = {
        "lastUpdated": datetime.now(timezone.utc).strftime("%B %d, %Y"),
        "models": updated_models,
    }

    with open(PRICES_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(updated_models)} models to {PRICES_FILE}.")

    if warnings:
        print("\nWARNINGS (needs a human look):")
        for w in warnings:
            print(f"  - {w}")
        sys.exit(1)  # non-zero exit so the scheduled run shows as failed/flagged


if __name__ == "__main__":
    main()