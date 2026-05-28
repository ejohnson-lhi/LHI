"""FAQ matcher -- token overlap with synonym expansion + IDF weighting.

Why not a fuzzy / ML approach:
  - rapidfuzz handles typos but not synonyms ("pup" vs "dog").
  - Sentence-transformer embeddings catch synonyms but pull in a heavy
    PyTorch dependency, which we want to avoid on the iris droplet
    (running ML batches there has bitten us before -- see
    `feedback_no_heavy_batch_on_iris_droplet`).

This module gets us the practical equivalent of fuzzy+synonyms via:
  1. Token-overlap scoring -- bag-of-words with stopwords removed.
  2. IDF weighting -- rare words ("blackberries") count more than
     common words ("the", "room"). Computed once over the KB at load.
  3. Synonym expansion -- a curated dict maps user vocabulary to
     canonical tokens. Grows over time as we see novel queries in the
     guest_qa log.

Output is a ranked list of (FaqEntry, score). Callers decide how many
to show and whether to fall through to the LLM.
"""
from __future__ import annotations

import math
import re
from collections import Counter

# Curated synonym groups. Key is the canonical token we want in the
# tokenset; values are alternate user phrasings. Both single words and
# multi-word phrases work; multi-word entries are matched as ngrams when
# the input is tokenized. Expand this list based on what we see in the
# guest_qa log.
#
# Editing rule: keep the CANONICAL token aligned with what already
# appears in the KB content. E.g. the KB says "dog", so the canonical
# is "dog" (not "pet"). Synonyms point AT it, not away from it.
_SYNONYM_GROUPS: dict[str, list[str]] = {
    "dog":        ["pup", "puppy", "pups", "puppies", "doggy", "doggie", "canine", "pet", "pets"],
    "breakfast":  ["brekkie", "morning meal", "morning food"],
    "checkin":    ["check-in", "check in", "arrive", "arrival", "checking in"],
    "checkout":   ["check-out", "check out", "depart", "departure", "leave", "leaving", "checking out"],
    "wifi":       ["wi-fi", "wireless", "internet", "network", "online", "connect"],
    "tv":         ["television", "cable", "channels"],
    "coffee":     ["espresso", "latte", "cappuccino", "caffeine"],
    "parking":    ["park", "car", "vehicle", "spaces"],
    "swim":       ["pool", "swimming"],
    "doorcode":   ["door code", "lock code", "keypad code", "key code", "access code"],
    "key":        ["lock", "door"],
    "ice":        ["ice maker", "ice machine", "freezer"],
    "smoke":      ["smoking", "cigarette", "cigarettes", "vape", "vaping", "weed", "marijuana", "cannabis"],
    "cancel":     ["refund", "cancellation", "refunds"],
    "kid":        ["child", "children", "kids", "infant", "baby", "toddler"],
    "trail":      ["hike", "hiking", "walk", "walking", "path"],
    "beach":      ["ocean", "shore", "sand", "coast"],
    "restaurant": ["dinner", "lunch", "food", "eat", "eating", "dine", "dining"],
    "discount":   ["deal", "promo", "promotion", "rate", "rates", "coupon"],
    "earlycheckin": ["arrive early", "early arrival", "before check"],
    "latecheckout": ["stay late", "late checkout", "late check-out"],
    "noise":      ["quiet", "noisy", "loud", "sound"],
    "view":       ["window", "scenery", "scenic"],
    "fee":        ["charge", "cost", "price", "fees", "charges"],
    "tax":        ["taxes", "lodging tax"],
    "bed":        ["beds", "mattress", "queen", "king", "twin"],
    "bathroom":   ["bath", "shower", "tub", "toilet", "restroom"],
    "towel":      ["towels", "linens", "sheets", "bedding"],
    "ada":        ["accessible", "accessibility", "wheelchair", "handicap", "disability"],
}

# Tokens that add no signal. Excluded from scoring but stay in the
# query-vs-entry comparison so we can later weight content words higher
# without losing context.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does",
    "for", "from", "have", "has", "i", "if", "in", "is", "it", "its",
    "me", "my", "of", "on", "or", "our", "that", "the", "their", "there",
    "to", "us", "was", "we", "what", "when", "where", "which", "who", "will",
    "with", "you", "your", "would", "could", "should", "can", "did", "this",
    "those", "these", "any", "some", "all", "any", "ya", "ok", "hi", "hello",
})


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _basic_tokens(text: str) -> list[str]:
    """Lowercase + split on non-alphanumeric. Empty strings discarded."""
    return _TOKEN_RE.findall(text.lower())


# Reverse-lookup table: any synonym (or canonical token) -> canonical
# token. Built once at import. Multi-word synonyms get their words
# mapped individually too, so a query like "morning meal" picks up
# the "breakfast" canonical via either "morning" or "meal".
_SYNONYM_LOOKUP: dict[str, str] = {}
for _canonical, _alts in _SYNONYM_GROUPS.items():
    _SYNONYM_LOOKUP[_canonical] = _canonical
    for _alt in _alts:
        for _word in _basic_tokens(_alt):
            _SYNONYM_LOOKUP.setdefault(_word, _canonical)


def tokens_for_text(text: str, *, expand: bool) -> frozenset[str]:
    """Bag-of-words tokenization with optional synonym expansion.

    With expand=True, each token is run through the synonym lookup --
    so the resulting set contains canonical tokens regardless of which
    phrasing the source used. We tokenize the KB entries with expand=True
    at load time AND the query at match time, which means both sides land
    in the same vocabulary."""
    out: set[str] = set()
    for t in _basic_tokens(text):
        if t in _STOPWORDS:
            continue
        if expand:
            out.add(_SYNONYM_LOOKUP.get(t, t))
        else:
            out.add(t)
    return frozenset(out)


# IDF table -- built lazily on first match call from whatever KB is
# currently loaded. Resets when the KB is reloaded.
_idf_cache: dict[str, float] | None = None
_idf_kb_size: int = 0


def _idf_for_token(token: str) -> float:
    """Inverse document frequency of `token` across the loaded KB. Common
    tokens (in many entries) get a low score; rare tokens get a high
    score. We weight matches by IDF so a hit on "blackberries" counts
    more than a hit on "room"."""
    global _idf_cache, _idf_kb_size
    from app.services.faq_kb import get_faq_entries  # noqa: PLC0415
    entries = get_faq_entries()
    n = len(entries)
    if _idf_cache is None or _idf_kb_size != n:
        # (Re)compute. Combined question + answer tokens count once per
        # entry (a token in both Q and A is one document occurrence).
        df: Counter[str] = Counter()
        for e in entries:
            for t in (e.question_tokens | e.answer_tokens):
                df[t] += 1
        # Smooth: log((N + 1) / (df + 1)) + 1. Avoids div-by-zero and
        # keeps IDFs positive even for tokens in every entry.
        _idf_cache = {
            t: math.log((n + 1) / (count + 1)) + 1.0
            for t, count in df.items()
        }
        _idf_kb_size = n
    return _idf_cache.get(token, math.log(n + 1) + 1.0)  # unseen token: max IDF


# Matches in the question text count more than matches in the answer
# (the question is the "headline" and reflects guest intent more
# directly). Weights are arbitrary -- tune as we see real query
# patterns.
_QUESTION_WEIGHT = 2.5
_ANSWER_WEIGHT = 1.0

# Score below which a match is too weak to surface. Empirical -- raise
# if guests see noisy matches, lower if they see "no matches" for
# obvious queries.
MIN_MATCH_SCORE = 0.8

# Fraction of the query's content tokens that must overlap with the
# entry's tokens for the match to count. Defends against the "one rare
# word in common" pathology -- e.g. "what events are going on this
# weekend" hitting "is it busy during special events?" purely because
# both contain "events". Coverage=1/2=50% for that case, so a 60%
# threshold rejects it and the Ask-Iris button shows instead.
#
# For 1-token queries this is always 0 or 1 (binary), so the threshold
# doesn't matter. The threshold is meaningful at 2+ tokens, exactly
# where coincidental overlap becomes a risk.
MIN_QUERY_COVERAGE = 0.6

# Hard cap on results shown. We expect the UI to show 3-5 anyway; this
# caps the work done if a vague query matches half the KB.
MAX_RESULTS = 8


def rank_matches(query: str) -> list[tuple["object", float]]:
    """Return (entry, score) pairs in descending score order.

    Three filters applied to each candidate:
      1. The entry's question tokens must cover at least
         MIN_QUERY_COVERAGE of the query's content tokens. Coverage uses
         the ENTRY'S QUESTION ONLY, not its answer. This is the critical
         filter that distinguishes "FAQ has the answer" from "FAQ has a
         word that coincidentally appears in the query." Answer body
         text can mention all sorts of vocabulary incidentally (Rhody
         Fest answer happens to mention "weekend"), which is too noisy
         to count toward coverage.
      2. The raw IDF-weighted score (questions weighted more than
         answers) must clear MIN_MATCH_SCORE. Catches the edge case
         where a query technically covers an entry's question but only
         via stopwords or low-information tokens.

    Trade-off accepted: some plausibly-relevant entries fall through to
    LLM because the entry's question doesn't share enough tokens with
    the query (e.g. "can I bring my dog" doesn't match "Are pets
    allowed?" because none of the query content tokens are in that
    entry's question after stopword + synonym). The LLM has the full
    KB in its system prompt and will surface the right answer at a cost
    of ~$0.002/call. The throttle caps total spend. This is acceptable
    in exchange for far fewer false positives -- guests hate seeing the
    wrong FAQ entry more than they hate the LLM thinking for two
    seconds."""
    from app.services.faq_kb import get_faq_entries  # noqa: PLC0415
    q_tokens = tokens_for_text(query, expand=True)
    if not q_tokens:
        return []
    n_query_tokens = len(q_tokens)
    scored: list[tuple[object, float]] = []
    for e in get_faq_entries():
        q_overlap = q_tokens & e.question_tokens
        if not q_overlap:
            continue  # no question-token overlap = not on topic
        q_coverage = len(q_overlap) / n_query_tokens
        if q_coverage < MIN_QUERY_COVERAGE:
            continue
        a_overlap = q_tokens & e.answer_tokens
        score = (
            sum(_idf_for_token(t) for t in q_overlap) * _QUESTION_WEIGHT
            + sum(_idf_for_token(t) for t in a_overlap - q_overlap) * _ANSWER_WEIGHT
        )
        if score >= MIN_MATCH_SCORE:
            scored.append((e, score))
    scored.sort(key=lambda t: -t[1])
    return scored[:MAX_RESULTS]
