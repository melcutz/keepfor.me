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

The stopword list follows the page language (issue #58): a German page is
cut at German function words, so "Hamburg und" cannot be a tag, and
German citation boilerplate ("Abgerufen am ...") is never a topic. The
language comes from the page's own <html lang> when the caller has it and
from a stopword-ratio guess otherwise.

Only `re`, `unicodedata`, `collections` and `urllib.parse` are used so this
module never grows the Worker cold-start import graph (same rule as the lazy
parser imports in keepfor/consumer/extractor.py).
"""

import re
import unicodedata
from collections import Counter
from typing import NamedTuple
from urllib.parse import urlparse

# URL noise words: they leak out of links in extracted prose and are
# meaningless in every language.
_URL_FRAGMENTS = frozenset(
    """
    http https www www2 com org net edu gov mil int
    html htm php aspx asp jsp cgi
    url href amp nbsp ftp cdn uri src xml json css js
    """.split()
)
_URL_NOISE = _URL_FRAGMENTS

# Month names and abbreviations (English and German). Citation/metadata noise.
_MONTHS = frozenset(
    """
    january jan
    february feb
    march mar
    april apr
    may
    june jun
    july jul
    august aug
    september sep sept
    october oct
    november nov
    december dec
    januar februar märz mär mai juni juli august
    september oktober november dezember okt dez
    """.split()
)

# Day names and abbreviations (English and German). Date noise.
_DAYS = frozenset(
    """
    monday mon
    tuesday tue tues
    wednesday wed
    thursday thu thur thurs
    friday fri
    saturday sat
    sunday sun
    montag dienstag mittwoch donnerstag freitag samstag sonnabend sonntag
    mo di mi do fr sa so
    """.split()
)

# Generic verbs and past participles that leak from citation boilerplate,
# publishing metadata or web actions.
_GENERIC_VERBS = frozenset(
    """
    retrieved retrieve retrieves retrieving
    archived archive archives archiving
    accessed access accesses accessing
    published publish publishes publishing
    updated update updates updating
    posted post posts posting
    modified modify modifies modifying
    edited edit edits editing
    viewed view views viewing
    downloaded download downloads downloading
    uploaded upload uploads uploading
    cited cite cites citing
    shared share shares sharing
    recorded record records recording
    printed print prints printing
    subscribed subscribe subscribes subscribing
    submitted submit submits submitting
    abgerufen aufgerufen archiviert zitiert
    """.split()
)

_JUNK_WORDS = frozenset(_GENERIC_VERBS | _MONTHS | _DAYS | _URL_FRAGMENTS | _URL_NOISE)

# Compact stopword list (~240 words). Embedded so there is no data file to
# bundle and no NLTK download at runtime.
STOPWORDS = (
    frozenset(
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
    unless isn't aren't wasn't weren't don't doesn't didn't can't couldn't
    won't wouldn't shouldn't mustn't hasn't haven't hadn't it's that's there's
    here's what's let's i'm i've i'd i'll he's she's you're we're they're
    we've they've we'll you'll they'll
    """.split()
    )
    | _URL_NOISE
    | _MONTHS
    | _DAYS
)

# German function words: articles, pronouns, prepositions, conjunctions,
# adverbs and the auxiliary verbs. A phrase edge is never one of these, so
# "Hamburg und" or "der Hafen" cannot become a tag. "de" is only a URL
# label (de.wikipedia.org), never a German word.
STOPWORDS_DE = frozenset(
    """
    ab aber alle allem allen aller alles als also am an ander andere anderem
    anderen anderer anderes andern anderr anders ans auch auf aus auseinander
    ausschließlich außerdem bei beim bereits beziehungsweise bezüglich bin
    bis bist da dabei dadurch dafür daher damit danach dann daran darauf
    darin darum das dass dazu de dein deine deinem deinen deiner deines dem
    demselben den denn denselben der derer derselbe derselben des desselben
    dessen dich die dies diese dieselbe dieselben diesem diesen dieser dieses
    dir doch dort du durch durften dürfen ebenfalls ein eine einem einen
    einer eines einig einige einigem einigen einiger einiges einmal er es
    etwa etwas euch euer eure eurem euren eurer eures ferner für
    gegebenenfalls gegen gegenüber gewesen ggf hab habe haben hat hatte
    hatten hier hierbei hierfür hierher hierin hierzu hin hinter hätte hätten
    höchstens ich ihm ihn ihnen ihr ihre ihrem ihren ihrer ihres im in indem
    ins insbesondere insgesamt insofern inwiefern ist ja jede jedem jeden
    jeder jedes jene jenem jenen jener jenes jetzt kann kein keine keinem
    keinen keiner keines konnte konnten können könnte könnten lassen laufend
    längs lässt mag man manche manchem manchen mancher manches mehr mein
    meine meinem meinen meiner meines meist meistens mich mir mit mochte muss
    musste möchten mögen nach nachdem nacheinander nein neulich nicht nichts
    noch nun nur ob oberhalb obwohl oder ohne schon sehr seien sein seine
    seinem seinen seiner seines seit selbst sich sie sind so solange solch
    solche solchem solchen solcher solches soll solleten sollte sondern sonst
    sowie sowohl um und uns unser unsere unserem unseren unserer unseres
    unter unterhalb viel vom von vor war waren warst was weg wegen weil
    weitere welchem welchen welcher welches wenn werde werden wie wieder will
    wir wird wirst wo woher wohin wollen wollte wollten womit worden wurde
    wurden während wären würde würden zu zuletzt zum zur zwar zwischen über
    """.split()
)

# Citation boilerplate of German articles: "Abgerufen am 25. September
# 2014", "Archiviert vom Original ...". Metadata, never a topic. The month
# names live here too: in a German article they are the date of a citation.
BOILERPLATE_DE = frozenset(
    """
    abgerufen aufgerufen archiv archiviert archivierung original webarchiv
    zitiert einzelbeleg einzelbelege einzelnachweis einzelnachweise literatur
    fundstelle abruf
    januar februar märz april mai juni juli august september oktober november
    dezember
    """.split()
)
STOPWORDS_DE |= BOILERPLATE_DE | _URL_NOISE | _MONTHS | _DAYS

# The stopword lists the tagger knows about, by language code.
LANGUAGE_STOPWORDS = {"en": STOPWORDS, "de": STOPWORDS_DE}

# Verbs that never head a tag. A candidate phrase is trimmed back to its
# noun-phrase core by dropping these from either end. Only unambiguous
# verbs are listed: a word that is also a common noun ("research", "work",
# "show") stays, because it may legitimately head a tag.
_VERBISH = (
    frozenset(
        """
    allow appear arrive avoid become believe borrow breathe choose come
    create decide define delete deny depend describe develop disappear
    discuss eat emerge enable encourage ensure enter establish examine
    expect explain fail feel fly follow forget forgive gather generate hate
    hurt imagine improve indicate involve join jump kill laugh lend lose
    mention miss notice organize prefer protect prove provide pull push
    read refuse reject rethink seek seem sell send sleep solve suffer
    suppose survive throw understand visit warn write
    see sees saw seen seeing
    weave weaves wove woven weaving
    build builds built building
    change changes changed changing
    look looks looked looking
    think thinks thought thinking
    know knows knew known knowing
    find finds found finding
    give gives gave given giving
    take takes took taken taking
    make makes made making
    show shows showed shown showing
    want wants wanted wanting
    need needs needed needing
    call calls called calling
    keep keeps kept keeping
    created creates creating
    decided decides deciding
    defined defines defining
    described describes describing
    developed develops developing
    followed follows following
    generated generates generating
    improved improves improving
    involved involves involving
    provided provides providing
    expected expects expecting
    explained explains explaining
    """.split()
    )
    | _GENERIC_VERBS
)

MAX_TAG_LENGTH = 64
MIN_TAG_LENGTH = 2
# Cap on the number of tag suggestions handed to the user.
MAX_SUGGESTIONS = 5
MIN_SUGGESTION_SCORE = 1.5
_VALID_TAG_RE = re.compile(r"^[^\W_]+(?:[ _-][^\W_]+)*$")
# Word tokens: Unicode letters (German umlauts and eszett are letters, not
# noise) plus digits, keeping apostrophes and hyphens inside a word.
_WORD_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)?")
# Title Case entities: an uppercase letter followed by lowercase ones. The
# ranges cover the Latin-1 letters, so "Bürgerschaft" is an entity like
# "Hamburg" is.
_ENTITY_RE = re.compile(
    r"\b([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿß]+(?:\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿß]+){0,2})\b"
)
# Sentence and line boundaries: a phrase never spans one.
_SEGMENT_RE = re.compile(r"[^.!?\n]+")


class MatchResult(NamedTuple):
    auto_apply: list[str]
    suggest_only: list[str]


class Suggestion(NamedTuple):
    phrase: str
    score: float


def normalize_tag(tag: str) -> str:
    """NFC-normalize, strip and lowercase a tag name."""
    return unicodedata.normalize("NFC", tag).strip().lower()


def validate_tag_name(raw: str) -> str | None:
    """Normalize and validate a user-supplied tag name for /tags forms.

    Returns the clean name, or None when invalid (empty, too short/long,
    or containing characters outside Unicode letters, digits, space, _ and -).
    """
    clean = normalize_tag(raw)
    if not (MIN_TAG_LENGTH <= len(clean) <= MAX_TAG_LENGTH):
        return None
    if not _VALID_TAG_RE.match(clean):
        return None
    return clean


HARD_BLOCKED_TAGS = frozenset({"www", "http", "https", "com"})


def is_hard_blocked_tag(tag: str) -> bool:
    """True when a tag is always blocked, even for a user's existing tags.

    Hard-blocked: www, http, https, com, and pure-number tokens (e.g. 2024, 123).
    """
    clean = normalize_tag(tag)
    if not clean:
        return True
    if clean in HARD_BLOCKED_TAGS or clean.isdigit():
        return True
    tokens = _WORD_RE.findall(clean)
    if not tokens:
        return True
    if len(tokens) == 1:
        w = tokens[0]
        return w in HARD_BLOCKED_TAGS or w.isdigit()
    return all(w in HARD_BLOCKED_TAGS or w.isdigit() for w in tokens)


def is_junk_tag(tag: str) -> bool:
    """True when a tag is noise/junk and must not be offered as a suggestion.

    Filters out:
    - Generic verbs and past participles (retrieved, archived, accessed,
      published, updated, posted...)
    - Month names and abbreviations (april, apr...)
    - Day names and abbreviations (monday, mon...)
    - URL fragments (www, http, https, com, org, net, html...)
    - Very short tokens (length <= 2) and pure numbers
    """
    clean = normalize_tag(tag)
    if not clean:
        return True
    if is_hard_blocked_tag(clean):
        return True
    if len(clean) <= 2:
        return True
    tokens = _WORD_RE.findall(clean)
    if not tokens:
        return True
    if len(tokens) == 1:
        w = tokens[0]
        return len(w) <= 2 or w.isdigit() or w in _JUNK_WORDS
    # Multi-token tag: junk if all tokens are short/digits/junk words, or if it
    # starts/ends with a junk word or number, or contains a URL fragment.
    if all(len(w) <= 2 or w.isdigit() or w in _JUNK_WORDS for w in tokens):
        return True
    if (
        tokens[0] in _JUNK_WORDS
        or tokens[-1] in _JUNK_WORDS
        or tokens[0].isdigit()
        or tokens[-1].isdigit()
    ):
        return True
    if any(w in _URL_FRAGMENTS for w in tokens):
        return True
    return False


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


def slug_tokens(url: str, stopwords: frozenset[str] = STOPWORDS) -> set[str]:
    """Lowercase alphanumeric tokens from the URL path + host."""
    try:
        parts = urlparse(url)
    except Exception:
        return set()
    text = f"{parts.netloc} {parts.path}".lower().replace("-", " ").replace("_", " ")
    return {
        w for w in _WORD_RE.findall(text) if len(w) >= 2 and not is_hard_blocked_tag(w)
    } - stopwords


def detect_language(text: str) -> str:
    """Guess the document language from its stopwords (no model, no deps).

    German wins only when its stopwords clearly outnumber the English
    ones, so an English page keeps the English list.
    """
    tokens = _WORD_RE.findall((text or "").lower())
    if not tokens:
        return "en"
    de = sum(1 for t in tokens if t in STOPWORDS_DE)
    en = sum(1 for t in tokens if t in STOPWORDS)
    return "de" if de >= 3 and de > 2 * en else "en"


def stopwords_for(lang: str | None, text: str = "") -> frozenset[str]:
    """The stopword list of a page: its declared language, else a guess."""
    code = (lang or "").strip().lower().split("-")[0]
    if code in LANGUAGE_STOPWORDS:
        return LANGUAGE_STOPWORDS[code]
    return LANGUAGE_STOPWORDS[detect_language(text)]


def match_existing_tags(
    title: str,
    excerpt: str,
    url: str,
    body: str,
    user_tags: list[str],
    lang: str | None = None,
) -> MatchResult:
    """Conservative lexical match of existing user tags.

    Auto-apply iff the tag hits in the title, excerpt or URL slug, or has
    2+ whole-word body hits. A single stray body mention is suggest-only.
    Tags shorter than 3 chars only auto-apply on a title hit (kills
    verb false positives like 'go').
    """
    auto: list[str] = []
    suggest: list[str] = []
    slugs = slug_tokens(url or "", stopwords_for(lang, f"{title} {excerpt} {body}"))
    for raw in user_tags:
        tag = normalize_tag(raw)
        if not tag or is_hard_blocked_tag(tag):
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


def _is_content(word: str, stopwords: frozenset[str]) -> bool:
    """True for a word that may carry meaning (not a stopword, not a number)."""
    return (
        word not in stopwords
        and len(word) > 2
        and not word.isdigit()
        and word not in _VERBISH
        and word not in _JUNK_WORDS
    )


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
    return word not in _VERBISH and word not in _JUNK_WORDS and word in head_ok


def _runs(
    tokens: list[str], significant: set[str], stopwords: frozenset[str]
) -> list[list[str]]:
    """Maximal runs of significant content words inside one segment."""
    runs: list[list[str]] = []
    current: list[str] = []
    for word in tokens:
        if (
            not _is_content(word, stopwords)
            or word in _VERBISH
            or word not in significant
        ):
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
    lang: str | None = None,
    min_score: float = MIN_SUGGESTION_SCORE,
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

    `lang` is the page's own language code ("de"); without it the language
    is guessed from the text, so a German page is cut at German stopwords.
    """
    title = title or ""
    excerpt = excerpt or ""
    body = body or ""
    if not (title.strip() or excerpt.strip() or body.strip()):
        return []

    stopwords = stopwords_for(lang, f"{title}\n{excerpt}\n{body}")
    existing = {normalize_tag(t) for t in user_tags}
    existing = {t for t in existing if t}
    existing_grams = {tuple(_fold(w) for w in _WORD_RE.findall(t)) for t in existing}
    existing_grams.discard(())
    existing_words = {w for t in existing for w in _WORD_RE.findall(t)}
    slugs = slug_tokens(url or "", stopwords)
    entities = {m.group(1).lower() for m in _ENTITY_RE.finditer(f"{title} {body}")}
    entity_words = {w for e in entities for w in e.split()}

    segments = _segments(f"{title}\n{excerpt}\n{body}")
    tokens = [_WORD_RE.findall(s) for s in segments]
    freq: Counter[str] = Counter()
    for seg in tokens:
        for word in seg:
            if _is_content(word, stopwords):
                freq[word] += 1
    title_words = {
        w for w in _WORD_RE.findall(title.lower()) if _is_content(w, stopwords)
    }
    repeated = {w for w, c in freq.items() if c >= 2}
    significant = repeated | slugs | entity_words | title_words
    # Words allowed to head a tag: the document repeats them, or the URL
    # slug / a Title Case entity names them.
    head_ok = repeated | slugs | entity_words

    runs: list[list[str]] = []
    for seg in tokens:
        for run in _runs(seg, significant, stopwords):
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
        # Drop stopword-led, stopword-ended, verb-led, and verb-ended n-grams (#97)
        if (
            gram[0] in stopwords
            or gram[-1] in stopwords
            or gram[0] in _VERBISH
            or gram[-1] in _VERBISH
            or not _can_head(gram[0], head_ok)
            or not _can_head(gram[-1], head_ok)
            or any(w in stopwords for w in gram)
            or any(w in _VERBISH for w in gram)
            or is_junk_tag(text)
            or any(is_junk_tag(w) for w in gram)
        ):
            continue
        if len(gram) == 1:
            if alone[gram[0]] < 1:
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
            score *= 2.0
        elif any(w in entity_words for w in gram):
            score *= 1.5
        if any(w in existing_words for w in gram):
            score *= 1.5
        if len(gram) > 1:
            # A collocation says more than the words it is made of.
            score *= 1.5
        if score < min_score:
            continue
        if text not in scored or score > scored[text]:
            scored[text] = score

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], -len(kv[0].split())))
    kept: list[Suggestion] = []
    kept_grams: list[tuple[str, ...]] = []
    for text, score in ranked:
        if is_junk_tag(text):
            continue
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
