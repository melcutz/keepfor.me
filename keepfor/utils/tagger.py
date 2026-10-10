# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Stdlib-only tag matching + keyphrase suggestion (no LLM, no new deps).

Two halves (hybrid tagging):
- match_existing_tags: conservative lexical match of the user's own tags
  against title/excerpt/URL-slug/body. Auto-apply only on a title, excerpt
  or slug hit, or 2+ body hits; a lone body mention is suggest-only.
- suggest_new_tags: RAKE-style keyphrase extraction over title + excerpt +
  body with title/slug/entity bonuses. Returns top phrases not already in
  the user's tag vocabulary.

Only `re`, `math` and `collections` are used so this module never grows
the Worker cold-start import graph (same rule as the lazy parser imports
in keepfor/consumer/extractor.py).
"""

import re
from collections import Counter
from typing import NamedTuple
from urllib.parse import urlparse

# Compact stopword list (~150 words). Embedded so there is no data file to
# bundle and no NLTK download at runtime.
STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because
    been before being below between both but by can cannot could did do does
    doing down during each few for from further had has have having he her
    here hers herself him himself his how i if in into is it its itself me
    more most my myself no nor not of off on once only or other ought our
    ours ourselves out over own same she should so some such than that the
    their theirs them themselves then there these they this those through to
    too under until up very was we were what when where which while who whom
    why with would you your yours yourself yourselves will just can also
    into over per via per using use used one two new may many much like get
    got make made still even ever never always often well within without
    """.split()
)

MAX_TAG_LENGTH = 64
MIN_TAG_LENGTH = 2
_VALID_TAG_RE = re.compile(r"^[a-z0-9]+(?:[ _-][a-z0-9]+)*$")
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)?")
_ENTITY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b")


class MatchResult(NamedTuple):
    auto_apply: list[str]
    suggest_only: list[str]


class Suggestion(NamedTuple):
    phrase: str
    score: float


def normalize_tag(tag: str) -> str:
    """Lowercase + strip (matches keepfor/models/items.py save normalization)."""
    return tag.strip().lower()


def validate_tag_name(raw: str) -> str | None:
    """Normalize and validate a user-supplied tag name for /tags forms.

    Returns the clean name, or None when invalid (empty, too short/long,
    or containing characters outside [a-z0-9 _-]).
    """
    clean = normalize_tag(raw)
    if not (MIN_TAG_LENGTH <= len(clean) <= MAX_TAG_LENGTH):
        return None
    if not _VALID_TAG_RE.match(clean):
        return None
    return clean


def _fold(word: str) -> str:
    """Naive singular fold for matching only (never for storage)."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _variants(tag: str) -> set[str]:
    folded = _fold(tag)
    out = {tag}
    if folded != tag:
        out.add(folded)
    return out


def _count_whole_word(text: str, tag: str) -> int:
    if not text:
        return 0
    total = 0
    for variant in _variants(tag):
        total += len(
            re.findall(r"\b" + re.escape(variant) + r"\b", text, flags=re.IGNORECASE)
        )
    return total


def slug_tokens(url: str) -> set[str]:
    """Lowercase alphanumeric tokens from the URL path + host."""
    try:
        parts = urlparse(url)
    except Exception:
        return set()
    text = f"{parts.netloc} {parts.path}".lower().replace("-", " ").replace("_", " ")
    return {w for w in _WORD_RE.findall(text) if len(w) >= 2} - STOPWORDS


def match_existing_tags(
    title: str,
    excerpt: str,
    url: str,
    body: str,
    user_tags: list[str],
) -> MatchResult:
    """Conservative lexical match of existing user tags.

    Auto-apply iff the tag hits in the title, excerpt or URL slug, or has
    2+ whole-word body hits. A single stray body mention is suggest-only.
    Tags shorter than 3 chars only auto-apply on a title hit (kills
    verb false positives like 'go').
    """
    auto: list[str] = []
    suggest: list[str] = []
    slugs = slug_tokens(url or "")
    for raw in user_tags:
        tag = normalize_tag(raw)
        if not tag:
            continue
        title_hits = _count_whole_word(title or "", tag)
        excerpt_hits = _count_whole_word(excerpt or "", tag)
        body_hits = _count_whole_word(body or "", tag)
        slug_hit = tag in slugs or _fold(tag) in slugs
        if len(tag) < 3:
            if title_hits >= 1:
                auto.append(tag)
            elif excerpt_hits >= 1 or body_hits >= 1 or slug_hit:
                suggest.append(tag)
            continue
        if title_hits >= 1 or excerpt_hits >= 1 or slug_hit or body_hits >= 2:
            auto.append(tag)
        elif body_hits >= 1:
            suggest.append(tag)
    return MatchResult(auto_apply=auto, suggest_only=suggest)


def _rake_candidates(words: list[str]) -> list[list[str]]:
    """Split a word stream into stopword-delimited phrases (max 3 words)."""
    phrases: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if word in STOPWORDS or len(word) < 2 or word.isdigit():
            if current:
                phrases.append(current)
                current = []
            continue
        current.append(word)
        if len(current) >= 3:
            phrases.append(current)
            current = []
    if current:
        phrases.append(current)
    return phrases


def suggest_new_tags(
    title: str,
    excerpt: str,
    url: str,
    body: str,
    user_tags: list[str],
    limit: int = 5,
) -> list[Suggestion]:
    """RAKE-style keyphrase extraction; stdlib only.

    Scores candidate phrases by sum of member-word degree/frequency, with
    bonuses for title phrases (x2), URL-slug overlap (x1.5) and Title Case
    entities (x1.25). Phrases already in the user's vocabulary are
    excluded (those go through match_existing_tags instead).
    """
    title = title or ""
    excerpt = excerpt or ""
    body = body or ""
    if not (title.strip() or excerpt.strip() or body.strip()):
        return []
    existing = {normalize_tag(t) for t in user_tags}

    combined = f"{title} {excerpt} {body}"
    lowered = combined.lower()
    words = _WORD_RE.findall(lowered)
    phrases = _rake_candidates(words)
    if not phrases:
        return []

    # Word degree/frequency over candidate phrases.
    freq: Counter[str] = Counter()
    degree: Counter[str] = Counter()
    for phrase in phrases:
        uniq = set(phrase)
        for word in uniq:
            freq[word] += 1
            degree[word] += len(phrase)
    word_score = {
        word: (degree[word] / freq[word]) if freq[word] else 0.0 for word in freq
    }

    title_lower = title.lower()
    slugs = slug_tokens(url or "")
    entities = {m.group(1).lower() for m in _ENTITY_RE.finditer(f"{title} {body}")}

    scored: dict[str, float] = {}
    for phrase in phrases:
        text = " ".join(phrase)
        if len(text) < 3 or text in existing:
            continue
        score = sum(word_score.get(w, 0.0) for w in phrase)
        if text in title_lower:
            score *= 2.0
        if any(w in slugs or _fold(w) in slugs for w in phrase):
            score *= 1.5
        if text in entities:
            score *= 1.25
        if text not in scored or score > scored[text]:
            scored[text] = score

    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    return [Suggestion(phrase=p, score=round(s, 4)) for p, s in ranked[:limit]]
