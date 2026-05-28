"""Parser + in-memory cache of the Iris knowledge base.

The source-of-truth file is `AI_Prompts/knowledge_base.md` (shared with
Iris's voice flow). Format is one entry per H3:

    ### Question text?

    Multi-paragraph answer here...

    ### Next question?
    ...

HTML comments (`<!-- ... -->`) and the file's preamble (everything before
the first H3) are skipped. We load + parse once at first import, then
serve the parsed list from memory. To pick up edits without a restart,
call `reload_faq_entries()`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Knowledge base lives in the project root's AI_Prompts/ folder.
# backend/app/services/faq_kb.py -> backend/app/services -> backend/app
# -> backend -> project_root -> AI_Prompts/knowledge_base.md
_KB_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "AI_Prompts"
    / "knowledge_base.md"
)


@dataclass(frozen=True)
class FaqEntry:
    """One Q&A entry from the knowledge base. `slug` is a stable id derived
    from the question text (lowercased, non-alphanumerics collapsed) --
    URLs and log rows reference entries by slug, not by index, so reorders
    don't break references."""
    slug: str
    question: str
    answer: str
    # Pre-tokenized question + answer for fast matching. Includes synonym
    # expansion. Recomputed whenever the KB reloads.
    question_tokens: frozenset[str] = field(default_factory=frozenset)
    answer_tokens: frozenset[str] = field(default_factory=frozenset)


def _slugify(text: str) -> str:
    """Stable slug from a question. Collapses non-alphanumerics to dashes,
    keeps it short. Two entries with the same question would collide; the
    KB is curated to avoid that, but a numeric suffix at load time would
    be a safe future addition."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:80]


def _strip_html_comments(text: str) -> str:
    """Drop <!-- ... --> blocks. The KB uses them for design notes that
    should never reach the matcher or the LLM."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _parse_kb(raw: str) -> list[FaqEntry]:
    """Split the markdown body on H3 headers; everything before the first
    H3 is preamble and discarded. Entries with an empty answer body are
    dropped silently (a malformed header without content shouldn't crash
    startup)."""
    raw = _strip_html_comments(raw)
    parts = re.split(r"^###\s+", raw, flags=re.MULTILINE)
    entries: list[FaqEntry] = []
    for part in parts[1:]:  # skip preamble
        lines = part.split("\n", 1)
        if not lines:
            continue
        question = lines[0].strip()
        answer = (lines[1].strip() if len(lines) > 1 else "")
        if not question or not answer:
            continue
        slug = _slugify(question)
        if not slug:
            continue
        entries.append(FaqEntry(slug=slug, question=question, answer=answer))
    return entries


# Module-level cache. Lazy load on first access -- avoids import-order
# pain if anything imports this module before the KB file is in place.
_cached_entries: list[FaqEntry] | None = None


def get_faq_entries() -> list[FaqEntry]:
    """Return the parsed FAQ list, loading from disk on first call.
    Tokenization happens lazily here (one pass over all entries) so the
    matcher always sees up-to-date token sets without paying the parse
    cost on every match call."""
    global _cached_entries
    if _cached_entries is not None:
        return _cached_entries
    if not _KB_PATH.exists():
        log.warning("FAQ KB not found at %s -- returning empty list", _KB_PATH)
        _cached_entries = []
        return _cached_entries
    try:
        raw = _KB_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("FAQ KB read failed (%s): %s", _KB_PATH, e)
        _cached_entries = []
        return _cached_entries
    parsed = _parse_kb(raw)

    # Tokenize each entry now. Avoids a circular import by deferring the
    # import of the matcher (which depends on this module for FaqEntry).
    from app.services.faq_match import tokens_for_text  # noqa: PLC0415
    with_tokens = [
        FaqEntry(
            slug=e.slug,
            question=e.question,
            answer=e.answer,
            question_tokens=tokens_for_text(e.question, expand=True),
            answer_tokens=tokens_for_text(e.answer, expand=True),
        )
        for e in parsed
    ]
    log.info("FAQ KB loaded: %d entries from %s", len(with_tokens), _KB_PATH)
    _cached_entries = with_tokens
    return _cached_entries


def reload_faq_entries() -> int:
    """Drop the in-memory cache so the next get_faq_entries() re-reads
    from disk. Returns the new entry count. Use after editing the KB
    file in production without a service restart."""
    global _cached_entries
    _cached_entries = None
    return len(get_faq_entries())


def get_entry_by_slug(slug: str) -> FaqEntry | None:
    """Look up by slug. Linear scan over ~150 entries is fine; if the KB
    grows past a few thousand we'd want a dict cache."""
    for e in get_faq_entries():
        if e.slug == slug:
            return e
    return None
