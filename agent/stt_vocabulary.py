"""Custom STT vocabulary for Iris's Deepgram Nova-3 instance.

Deepgram Nova-3 uses **Keyterm Prompting** (parameter name `keyterm`),
which is fundamentally different from the legacy `keywords` parameter
used by Nova-2 and older models:
  - Nova-2 / earlier: `keywords` = list of (term, boost) tuples, where
    boost > 1 raises the term's likelihood at the n-gram level.
  - Nova-3: `keyterm` = list of plain string terms. The model attends
    to them semantically. NO BOOST VALUES — passing tuples to Nova-3
    via `keywords=` triggers a ValueError at STT init that crashes
    the entrypoint and silently drops the call. Production was broken
    by this exact mistake for 4 days (2026-06-16 to 2026-06-20).

These are terms that have shown up mis-transcribed in production
transcripts, or are property-specific proper nouns that Nova-3's general
model won't have strong priors for. Whenever a new STT miss is observed
in a real call, add the canonical spelling here.

Format note: each entry should be a single word or a short phrase
(<=3 words). Longer phrases don't get attended to reliably by Nova-3 —
split into the component words. Keep canonical capitalization (Nova-3
echoes it in the transcript).

Deepgram limits the number of keyterms per request (currently 100 for
Nova-3 standard plans). We're well under that ceiling.
"""
from __future__ import annotations

# Plain string list — Nova-3 keyterm prompting format.
HOTEL_KEYTERMS: list[str] = [
    # ── Room types / amenities ────────────────────────────────────────
    # "river view" → "rivalry" observed in 2026-06-16 transcripts (calls
    # iris-call-_+1529989800458_*). Confirmed by user on the same day.
    "river view",
    "ocean view",
    "river",
    "ocean",
    "queen",
    "king",
    "twin",
    "two queens",
    "two kings",
    "family suite",
    "connecting suite",
    "continental breakfast",
    "homemade breakfast",
    "ground floor",
    "ADA accessible",

    # ── Property + location proper nouns ──────────────────────────────
    # Nova-3's general model doesn't strongly prior these.
    "Lighthouse Inn",
    "Florence",
    "Heceta",               # Heceta Head Lighthouse — local landmark
    "Heceta Head",
    "Old Town",
    "Old Town Florence",
    "Highway 101",
    "Siuslaw",              # Siuslaw River — runs through Florence
    "Sea Lion Caves",       # local attraction often asked about
    "dunes",                # Oregon Dunes — nearby

    # ── People (agent + owner) ────────────────────────────────────────
    "Iris",
    "Eric",

    # ── OTAs / booking sites (drives confirmation-number lookups) ─────
    # "VRBO" was historically heard as "verbo" / "very bo".
    "Booking.com",
    "Booking",
    "Expedia",
    "Hotels.com",
    "VRBO",
    "Airbnb",
    "Priceline",
    "Hopper",
    "Trivago",
    "Agoda",
    "Kayak",
    "Orbitz",
    "Travelocity",

    # ── Reservation / front-desk vocabulary ───────────────────────────
    "check-in",
    "check-out",
    "checkout",
    "checkin",
    "confirmation number",
    "reservation",
    "cancellation",
    "late check-out",
    "early check-in",

    # ── Payment vocabulary ────────────────────────────────────────────
    "MasterCard",
    "American Express",
    "Amex",
    "Discover",
    "CVV",                  # often misheard as "CVB" / "CV"
    "expiration",
    "zip code",

    # ── Property operations (door codes, fees) ────────────────────────
    "door code",
    "lockout",
    "pet fee",
    "dog fee",
    "pet-friendly",

    # ── Connectivity / amenities ──────────────────────────────────────
    "Wi-Fi",
    "WiFi",

    # ── Software / vendor names (rare but useful in admin calls) ──────
    "Cloudbeds",
]
