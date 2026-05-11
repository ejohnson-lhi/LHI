"""Knowledge-base lookup for Iris.

Replaces the previous practice of inlining knowledge_base.md (~7K tokens)
into every system prompt. The agent's `inn_info(question)` tool calls
`lookup()` here on demand; only the relevant entries land in the LLM
context for that turn.

Search is intentionally simple: tokenize the question, count distinct
non-stopword tokens that appear in each KB entry, return the top N. No
embedding model, no external service — 159 entries, in-process.
"""
from __future__ import annotations

import re
from pathlib import Path

KB_PATH = Path(__file__).resolve().parent.parent / "AI_Prompts" / "knowledge_base.md"

# Words too common to be useful signals; dropping them avoids "the" or "a"
# inflating every score equally.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "am", "of", "in", "on", "at", "to", "for", "with", "by", "from", "and",
    "or", "but", "if", "do", "does", "did", "have", "has", "had", "i",
    "we", "you", "they", "he", "she", "it", "this", "that", "these",
    "those", "what", "which", "who", "whom", "whose", "where", "when",
    "why", "how", "any", "some", "all", "no", "not", "my", "your", "their",
    "our", "his", "her", "its", "can", "could", "should", "would", "will",
    "may", "might", "yes", "yours", "ours", "theirs", "mine", "as",
})


def _tokens(text: str) -> set[str]:
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in raw if t not in _STOPWORDS and len(t) > 1}


def _parse_kb(text: str) -> list[tuple[str, str]]:
    """Parse `### Header` / body pairs from the KB markdown.

    Drops HTML comments (the design-note blocks in the source file) so
    they don't leak into tool responses.
    """
    entries: list[tuple[str, str]] = []
    header: str | None = None
    body: list[str] = []
    in_comment = False
    for line in text.splitlines():
        s = line.strip()
        if not in_comment and s.startswith("<!--"):
            in_comment = True
            if s.endswith("-->"):
                in_comment = False
            continue
        if in_comment:
            if s.endswith("-->"):
                in_comment = False
            continue
        if line.startswith("### "):
            if header is not None:
                joined = "\n".join(body).strip()
                if joined:
                    entries.append((header, joined))
            header = line[4:].strip()
            body = []
        elif header is not None:
            body.append(line)
    if header is not None:
        joined = "\n".join(body).strip()
        if joined:
            entries.append((header, joined))
    return entries


# Loaded once at module import; cheap (<30 KB file).
_ENTRIES: list[tuple[str, str]] = _parse_kb(KB_PATH.read_text(encoding="utf-8"))


def lookup(question: str, max_results: int = 3) -> str:
    """Return up to `max_results` KB entries scored against `question`.

    Score is the number of distinct non-stopword tokens from `question`
    that appear in the entry's header or body. Ties broken by KB order.
    """
    if not question or not question.strip():
        return "Please specify what to look up."

    q = _tokens(question)
    if not q:
        return f"No searchable terms in: {question!r}"

    scored: list[tuple[int, str, str]] = []
    for h, b in _ENTRIES:
        score = len(q & (_tokens(h) | _tokens(b)))
        if score > 0:
            scored.append((score, h, b))

    if not scored:
        return f"No knowledge base entry found for: {question}"

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_results]
    return "\n\n".join(f"## {h}\n{b}" for _, h, b in top)
