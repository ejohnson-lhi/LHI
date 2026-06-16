"""Custom STT vocabulary for Iris's Deepgram Nova-3 instance.

Deepgram's `keywords` parameter (livekit-plugins-deepgram) accepts a list
of `(term, boost)` tuples. Boost > 1 makes Nova-3 more likely to emit
that term; boost < 1 makes it less likely. Default boost (without this
list) is effectively 1.0.

These are terms that have shown up mis-transcribed in production
transcripts, or are property-specific proper nouns that Nova-3's general
model won't have strong priors for. Whenever a new STT miss is observed
in a real call, add the canonical spelling here.

Practical boost range:
  1.2 – 1.5  light nudge for already-plausible words
  1.6 – 2.0  proper nouns / rare terms where misses dominate
  2.0 – 2.5  upper bound; higher tends to OVER-trigger and cause
             false hears in noise. Don't go above 2.5 without
             evidence the misses are very expensive.

Format note: each entry should be a single word or a short phrase
(≤3 words). Longer phrases don't get boosted reliably by Nova-3 — split
into the component words. Keep canonical capitalization (Nova-3 echoes
it in the transcript).
"""
from __future__ import annotations

# Each (term, boost) tuple — boost is the Deepgram intensifier.
HOTEL_KEYWORDS: list[tuple[str, float]] = [
    # ── Room types / amenities ────────────────────────────────────────
    # "river view" → "rivalry" observed in 2026-06-16 transcripts (calls
    # iris-call-_+1529989800458_*). Confirmed by user on the same day.
    ("river view", 2.2),
    ("ocean view", 1.8),
    ("river", 1.5),
    ("ocean", 1.3),
    ("queen", 1.5),
    ("king", 1.5),
    ("twin", 1.5),
    ("two queens", 1.8),
    ("two kings", 1.8),
    ("family suite", 1.8),
    ("connecting suite", 1.5),
    ("continental breakfast", 1.5),
    ("homemade breakfast", 1.5),
    ("ground floor", 1.5),
    ("ADA accessible", 1.5),

    # ── Property + location proper nouns ──────────────────────────────
    # Nova-3's general model doesn't strongly prior these.
    ("Lighthouse Inn", 2.2),
    ("Florence", 1.5),
    ("Heceta", 2.5),               # Heceta Head Lighthouse — local landmark
    ("Heceta Head", 2.5),
    ("Old Town", 1.5),
    ("Old Town Florence", 1.8),
    ("Highway 101", 1.5),
    ("Siuslaw", 2.5),              # Siuslaw River — runs through Florence
    ("Sea Lion Caves", 2.0),       # local attraction often asked about
    ("dunes", 1.3),                # Oregon Dunes — nearby

    # ── People (agent + owner) ────────────────────────────────────────
    ("Iris", 1.8),
    ("Eric", 1.5),

    # ── OTAs / booking sites (drives confirmation-number lookups) ─────
    # "VRBO" was historically heard as "verbo" / "very bo".
    ("Booking.com", 2.0),
    ("Booking", 1.5),
    ("Expedia", 1.8),
    ("Hotels.com", 1.8),
    ("VRBO", 2.5),
    ("Airbnb", 1.5),
    ("Priceline", 1.8),
    ("Hopper", 1.5),
    ("Trivago", 1.8),
    ("Agoda", 2.0),
    ("Kayak", 1.5),
    ("Orbitz", 1.8),
    ("Travelocity", 1.8),

    # ── Reservation / front-desk vocabulary ───────────────────────────
    ("check-in", 1.5),
    ("check-out", 1.5),
    ("checkout", 1.3),
    ("checkin", 1.3),
    ("confirmation number", 1.5),
    ("reservation", 1.3),
    ("cancellation", 1.3),
    ("late check-out", 1.5),
    ("early check-in", 1.5),

    # ── Payment vocabulary ────────────────────────────────────────────
    ("MasterCard", 1.5),
    ("American Express", 1.5),
    ("Amex", 1.5),
    ("Discover", 1.3),
    ("CVV", 1.8),                  # often misheard as "CVB" / "CV"
    ("expiration", 1.3),
    ("zip code", 1.3),

    # ── Property operations (door codes, fees) ────────────────────────
    ("door code", 1.5),
    ("lockout", 1.5),
    ("pet fee", 1.8),
    ("dog fee", 1.8),
    ("pet-friendly", 1.5),

    # ── Connectivity / amenities ──────────────────────────────────────
    ("Wi-Fi", 1.5),
    ("WiFi", 1.5),

    # ── Software / vendor names (rare but useful in admin calls) ──────
    ("Cloudbeds", 2.0),
]
