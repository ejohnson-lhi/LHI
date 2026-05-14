"""Mine recorded transcripts for cacheable user-question → assistant-answer pairs.

Scope: STATIC-FACT responses only — anything where the answer would be identical
regardless of date or which caller is on the line. Excludes:
- Reservation lookups / bookings (caller-specific)
- Availability queries (date-specific)
- Anything referencing "your room", "your reservation", specific dates
- Transfer-flow strings (already in PERSISTENT_OPENERS)
- Tool-using responses (LLM made a function call before answering)

Output: a flat list of (user_text, assistant_text, source_transcript) so the
intent_cache.json can be hand-curated from observed phrasing rather than
guessed.
"""
import json
import re
from collections import defaultdict
from pathlib import Path

RECORDINGS = Path(__file__).parent.parent / "recordings"
OUT = Path(__file__).parent.parent / "agent" / "intent_cache_seed.json"
SUMMARY = Path(__file__).parent.parent / "agent" / "intent_cache_seed_summary.txt"

# Substrings that mark a response as date- or caller-dependent → SKIP.
EXCLUDE_TOKENS = (
    "your reservation", "your room", "your stay", "your booking",
    "today", "tomorrow", "yesterday",
    "this week", "next week", "this weekend", "next weekend",
    "this month", "next month",
    "expedia", "booking.com", "airbnb", "vrbo",
    "confirmation number", "confirmation code", "reservation id",
    "check that", "let me check", "i'll check", "checking that",
    "transfer", "front desk", "eric",
    "saturday, may", "sunday, may", "monday, may", "tuesday, may",
    "wednesday, may", "thursday, may", "friday, may",
    "saturday, june", "sunday, june",
    "the 1st", "the 2nd", "the 3rd", "the 4th", "the 5th",
    "the 6th", "the 7th", "the 8th", "the 9th", "the 10th",
    "the 11th", "the 12th", "the 13th", "the 14th", "the 15th",
    "the 16th", "the 17th", "the 18th", "the 19th", "the 20th",
    "the 21st", "the 22nd", "the 23rd", "the 24th", "the 25th",
    "the 26th", "the 27th", "the 28th", "the 29th", "the 30th",
)

# Static-fact "anchors" — at least one must appear to be cacheable. Loose net.
STATIC_KEYWORDS = (
    "pet", "dog", "cat",
    "check-in", "check in", "checkin",
    "check-out", "check out", "checkout",
    "wifi", "wi-fi", "internet",
    "breakfast", "coffee",
    "parking", "park",
    "pool", "hot tub", "jacuzzi",
    "amenit", "wheelchair", "elevator", "stairs",
    "address", "located", "location",
    "hour", "open", "close",
    "smoke", "smoking",
    "credit card", "payment",
    "age", "minimum age",
    "kid", "child", "infant",
    "iron", "ironing", "hair dryer", "microwave", "fridge",
    "ac ", "air condition", "heat", "heater",
    "$", "dollar", "fee", "rate", "price", "cost",
    "available", "vacanc",  # only if no date keyword (caught by EXCLUDE)
    "11 ", "2 pm", "8 pm", "2 a", "11 a",
)


def is_cacheable(assistant_text: str) -> bool:
    al = assistant_text.lower()
    if any(t in al for t in EXCLUDE_TOKENS):
        return False
    if not any(k in al for k in STATIC_KEYWORDS):
        return False
    # Skip very long multi-paragraph responses — usually reservation summaries.
    if len(assistant_text) > 400:
        return False
    return True


def main():
    pairs = []
    transcripts = sorted(RECORDINGS.glob("transcript_*.json"))
    print(f"Scanning {len(transcripts)} transcripts...")

    for tp in transcripts:
        try:
            data = json.loads(tp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  skip {tp.name}: {e}")
            continue
        items = data.get("items", [])
        for i, item in enumerate(items):
            if item.get("type") != "ChatMessage" or item.get("role") != "user":
                continue
            user_text = (item.get("content") or "").strip()
            if not user_text or len(user_text) < 4:
                continue
            # Find the next assistant ChatMessage (which may follow a function
            # call). Reject ONLY caller-specific tools (lookup_reservation,
            # check_availability, book_reservation, modify_reservation).
            # `inn_info` is a static-fact KB lookup → its response IS cacheable.
            CALLER_SPECIFIC_TOOLS = {
                "lookup_reservation", "check_availability",
                "book_reservation", "modify_reservation",
                "cancel_reservation",
            }
            saw_caller_specific_tool = False
            for j in range(i + 1, min(i + 8, len(items))):
                nxt = items[j]
                if nxt.get("type") == "FunctionCall":
                    if nxt.get("name") in CALLER_SPECIFIC_TOOLS:
                        saw_caller_specific_tool = True
                        break
                    # inn_info or other static-fact tool — keep walking.
                    continue
                if nxt.get("type") == "FunctionCallOutput":
                    continue
                if nxt.get("type") == "ChatMessage" and nxt.get("role") == "assistant":
                    if saw_caller_specific_tool:
                        break
                    a_text = (nxt.get("content") or "").strip()
                    if not a_text or nxt.get("interrupted"):
                        break
                    if is_cacheable(a_text):
                        pairs.append({
                            "user": user_text,
                            "assistant": a_text,
                            "source": tp.name,
                        })
                    break

    OUT.write_text(json.dumps(pairs, indent=2), encoding="utf-8")
    print(f"Wrote {len(pairs)} cacheable Q->A pairs to {OUT}")

    # Build a summary grouped by approximate intent keyword.
    buckets: dict[str, list[dict]] = defaultdict(list)
    intent_keywords = [
        ("pet_fee",       ("pet fee", "dollar pet", "$20", "twenty dollar")),
        ("pet_dogs",      ("allow dog", "welcome dog", "bring a dog", "accept dog", "dogs are welcome", "we allow dogs")),
        ("pet_cats",      ("don't accept cat", "no cat", "we don't have cat", "cats", "without cat")),
        ("checkin_time",  ("check in", "check-in")),
        ("checkout_time", ("check out", "check-out", "11 am", "11 a.m")),
        ("wifi",          ("wifi", "wi-fi", "internet")),
        ("breakfast",     ("breakfast", "coffee")),
        ("parking",       ("parking", "park")),
        ("pool",          ("pool", "hot tub")),
        ("smoking",       ("smoke", "smoking")),
        ("amenities",     ("amenit", "iron", "hair dryer", "fridge", "microwave")),
        ("address",       ("address", "located", "location")),
    ]
    for p in pairs:
        al = p["assistant"].lower()
        ul = p["user"].lower()
        bucket = None
        for name, kws in intent_keywords:
            if any(k in al for k in kws) or any(k in ul for k in kws):
                bucket = name
                break
        buckets[bucket or "other"].append(p)

    lines = [f"Total cacheable pairs: {len(pairs)}", ""]
    for name in [k for k, _ in intent_keywords] + ["other"]:
        items = buckets.get(name, [])
        lines.append(f"=== {name}: {len(items)} pair(s) ===")
        # Dedup assistant text within bucket
        seen = set()
        for p in items:
            key = p["assistant"]
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  Q: {p['user'][:100]}")
            lines.append(f"  A: {p['assistant'][:200]}")
            lines.append("")
        if not items:
            lines.append("  (none)")
            lines.append("")

    SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote summary to {SUMMARY}")


if __name__ == "__main__":
    main()
