# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Stdlib-only tag matching + keyphrase suggestion (no LLM, no new deps).

Two halves (hybrid tagging):
- match_existing_tags: conservative lexical match of the user's own tags
  against title/excerpt/URL-slug/body. Auto-apply only on a title, excerpt
  or slug hit, or 2+ body hits; a lone body mention is suggest-only.
- suggest_new_tags: keyphrase extraction over title + excerpt + body. Only
  significant words (repeated, or named by the title, the URL slug or a
  Title Case entity) form candidates, and a candidate is cut at sentence,
  stopword and rare-word boundaries and trimmed to its noun core, so the
  result is a noun phrase and never a fragment of a clause. A word that
  only ever shows up inside a collocation ("parallel" in "parallel
  vacuum") is not a tag of its own, and a phrase the user's vocabulary
  already covers is left to match_existing_tags.

Only `re`, `collections` and `urllib.parse` are used so this module never
grows the Worker cold-start import graph (same rule as the lazy parser
imports in keepfor/consumer/extractor.py).
"""

import re
from collections import Counter
from typing import NamedTuple
from urllib.parse import urlparse

# Compact stopword list (~240 words). Embedded so there is no data file to
# bundle and no NLTK download at runtime. It also carries the URL noise words
# that leak out of links in extracted prose.
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
    us let lets now around among onto upon across behind beyond near toward
    towards along despite except plus since versus regarding concerning
    including yet another either neither whether although though because
    unless https http www com org net html htm php aspx url href amp nbsp
    isn't aren't wasn't weren't don't doesn't didn't can't couldn't won't
    wouldn't shouldn't mustn't hasn't haven't hadn't it's that's there's
    here's what's let's i'm i've i'd i'll he's she's you're we're they're
    we've they've we'll you'll they'll
    """.split()
)

# Verbs that never head a tag. A candidate phrase is trimmed back to its
# noun-phrase core by dropping these from either end. Only unambiguous
# verbs are listed: a word that is also a common noun ("research", "work",
# "show") stays, because it may legitimately head a tag.
_VERBISH = frozenset(
    """
    allow appear arrive avoid become believe borrow breathe choose come
    create decide define delete deny depend describe develop disappear
    discuss eat emerge enable encourage ensure enter establish examine
    expect explain fail feel fly follow forget forgive gather generate hate
    hurt imagine improve indicate involve join jump kill laugh lend lose
    mention miss notice organize prefer protect prove provide pull push
    read refuse reject rethink seek seem sell send sleep solve suffer
    suppose survive throw understand visit warn write
    """.split()
)

MAX_TAG_LENGTH = 64
MIN_TAG_LENGTH = 2
# Cap on the number of tag suggestions handed to the user.
MAX_SUGGESTIONS = 5
_VALID_TAG_RE = re.compile(r"^[a-z0-9]+(?:[ _-][a-z0-9]+)*$")
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)?")
_ENTITY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b")
# Sentence and line boundaries: a phrase never spans one.
_SEGMENT_RE = re.compile(r"[^.!?\n]+")


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


def _segments(text: str) -> list[str]:
    """Lowercased sentence-ish chunks; a phrase never spans a boundary."""
    return [s.lower() for s in _SEGMENT_RE.findall(text or "")]


def _is_content(word: str) -> bool:
    """True for a word that may carry meaning (not a stopword, not a number)."""
    return word not in STOPWORDS and len(word) >= 2 and not word.isdigit()


def _trim(run: list[str], head_ok: set[str]) -> list[str]:
    """Drops the words that cannot head a tag, keeping the noun core.

    A head is a word the document repeats, or one the URL slug or a Title
    Case entity names; a verb is never a head. A one-off word at either end
    ("... vacuum changed") is the tail of a clause, not a tag.
    """
    while len(run) > 1 and not _can_head(run[-1], head_ok):
        run = run[:-1]
    while len(run) > 1 and not _can_head(run[0], head_ok):
        run = run[1:]
    return run


def _can_head(word: str, head_ok: set[str]) -> bool:
    """True when the word may end (or start) a tag."""
    return word not in _VERBISH and word in head_ok


def _runs(tokens: list[str], significant: set[str]) -> list[list[str]]:
    """Maximal runs of significant content words inside one segment."""
    runs: list[list[str]] = []
    current: list[str] = []
    for word in tokens:
        if not _is_content(word) or word not in significant:
            if current:
                runs.append(current)
                current = []
            continue
        current.append(word)
    if current:
        runs.append(current)
    return runs


def _ngrams(run: list[str], max_words: int = 3):
    """Every contiguous 1..max_words slice of a run."""
    for i in range(len(run)):
        for n in range(1, min(max_words, len(run) - i) + 1):
            yield tuple(run[i : i + n])


def _is_subsequence(small: tuple[str, ...], big: tuple[str, ...]) -> bool:
    """True when `small` appears contiguously inside `big`."""
    n = len(small)
    return any(big[i : i + n] == small for i in range(len(big) - n + 1))


def suggest_new_tags(
    title: str,
    excerpt: str,
    url: str,
    body: str,
    user_tags: list[str],
    limit: int = MAX_SUGGESTIONS,
) -> list[Suggestion]:
    """Keyphrase extraction; stdlib only.

    Candidates are runs of *significant* words: words that repeat in the
    document, or that appear in the URL slug, a Title Case entity or the
    title. Runs are cut at sentence, stopword and rare-word boundaries and
    trimmed back to their noun core, so a suggestion is a noun phrase and
    never a fragment of a clause. A multi-word suggestion must be a repeated
    collocation or be anchored in the title/slug, a single word must stand on
    its own somewhere, and a phrase the user's vocabulary already covers is
    left to match_existing_tags.
    """
    title = title or ""
    excerpt = excerpt or ""
    body = body or ""
    if not (title.strip() or excerpt.strip() or body.strip()):
        return []

    existing = {normalize_tag(t) for t in user_tags}
    existing = {t for t in existing if t}
    existing_grams = {tuple(_fold(w) for w in _WORD_RE.findall(t)) for t in existing}
    existing_grams.discard(())
    slugs = slug_tokens(url or "")
    entities = {m.group(1).lower() for m in _ENTITY_RE.finditer(f"{title} {body}")}
    entity_words = {w for e in entities for w in e.split()}

    segments = _segments(f"{title}\n{excerpt}\n{body}")
    tokens = [_WORD_RE.findall(s) for s in segments]
    freq: Counter[str] = Counter()
    for seg in tokens:
        for word in seg:
            if _is_content(word):
                freq[word] += 1
    title_words = {w for w in _WORD_RE.findall(title.lower()) if _is_content(w)}
    repeated = {w for w, c in freq.items() if c >= 2}
    significant = repeated | slugs | entity_words | title_words
    # Words allowed to head a tag: the document repeats them, or the URL
    # slug / a Title Case entity names them.
    head_ok = repeated | slugs | entity_words

    runs: list[list[str]] = []
    for seg in tokens:
        for run in _runs(seg, significant):
            trimmed = _trim(run, head_ok)
            if trimmed:
                runs.append(trimmed)

    # How often a word stands on its own: "parallel" in "parallel vacuum" is
    # part of a collocation, not a tag of its own.
    alone: Counter[str] = Counter(run[0] for run in runs if len(run) == 1)
    occurrences: Counter[tuple[str, ...]] = Counter()
    for run in runs:
        for gram in _ngrams(run):
            occurrences[gram] += 1

    scored: dict[str, float] = {}
    for gram, count in occurrences.items():
        text = " ".join(gram)
        folded = tuple(_fold(w) for w in gram)
        if any(a == b for a, b in zip(gram, gram[1:])):
            # A repeated word ("postgres postgres") is not a phrase.
            continue
        if any(
            _is_subsequence(tag, folded) or _is_subsequence(folded, tag)
            for tag in existing_grams
        ):
            # The user's own vocabulary wins over a novel phrase near it.
            continue
        if len(gram) == 1:
            if gram[0] in _VERBISH or gram[0] not in head_ok or alone[gram[0]] < 1:
                continue
            base = alone[gram[0]]
        else:
            base = count
            anchored = any(w in title_words or w in slugs for w in gram)
            if count < 2 and not anchored:
                continue
        score = float(base)
        if all(w in title_words for w in gram):
            score *= 2.0
        if any(w in slugs or _fold(w) in slugs for w in gram):
            score *= 1.5
        if text in entities:
            score *= 1.25
        if len(gram) > 1:
            # A collocation says more than the words it is made of.
            score *= 1.5
        if text not in scored or score > scored[text]:
            scored[text] = score

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], -len(kv[0].split())))
    kept: list[Suggestion] = []
    kept_grams: list[tuple[str, ...]] = []
    for text, score in ranked:
        folded = tuple(_fold(w) for w in text.split())
        if any(_is_subsequence(folded, other) for other in kept_grams):
            # Already covered by a longer suggestion ("vacuum" under
            # "parallel vacuum").
            continue
        kept.append(Suggestion(phrase=text, score=round(score, 4)))
        kept_grams.append(folded)
        if len(kept) >= limit:
            break
    return kept
