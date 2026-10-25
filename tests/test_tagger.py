# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for stdlib-only tag matching + RAKE suggestion (keepfor/utils/tagger.py)."""

from keepfor.utils import tagger


def test_normalize_tag_lowercases_and_strips():
    assert tagger.normalize_tag("  PyThon ") == "python"
    assert tagger.normalize_tag("") == ""


def test_normalize_tag_applies_nfc_without_ascii_folding():
    # Composed (\u00fc) and decomposed (u + \u0308) normalize to the same NFC string.
    composed = "konsumgüterbranche"
    decomposed = "konsumgu\u0308terbranche"
    assert composed != decomposed
    assert tagger.normalize_tag(decomposed) == composed
    assert tagger.normalize_tag("U\u0308bung") == "übung"
    assert tagger.normalize_tag("Cafe\u0301") == "café"
    # Must not fold umlauts to ASCII or eszett to 'ss'.
    assert tagger.normalize_tag("Straße") == "straße"
    assert tagger.normalize_tag("übung") != "ubung"


def test_validate_tag_name_accepts_sane_names():
    assert tagger.validate_tag_name("machine-learning") == "machine-learning"
    assert tagger.validate_tag_name("  AI ") == "ai"
    assert tagger.validate_tag_name("konsumgüterbranche") == "konsumgüterbranche"
    assert tagger.validate_tag_name("konsumgu\u0308terbranche") == "konsumgüterbranche"
    assert tagger.validate_tag_name("straße") == "straße"
    assert tagger.validate_tag_name("café") == "café"
    assert tagger.validate_tag_name("cafe\u0301") == "café"
    assert tagger.validate_tag_name("Freie Hansestadt") == "freie hansestadt"


def test_validate_tag_name_rejects_bad_names():
    for bad in [
        "",
        "  ",
        "a",
        "x" * 65,
        "has/slash",
        "#hashtag",
        "a#b",
        "semi;colon",
        'quote"x',
        "\U0001f680\U0001f680",
        "ai\U0001f680",
        "\U0001f680 rocket",
        "a\x00b",
        "a\nb",
        "a\tb",
        "a\x1fb",
        "a  b",
        "a--b",
        "a__b",
        "a -b",
        "a_ b",
        "_leading",
        "trailing_",
        "-leading",
        "trailing-",
    ]:
        assert tagger.validate_tag_name(bad) is None, bad


def test_match_existing_tags_title_hit_auto_applies():
    res = tagger.match_existing_tags(
        title="Postgres 16 features",
        excerpt="",
        url="https://example.com/x",
        body="unrelated text here",
        user_tags=["postgres", "cooking"],
    )
    assert "postgres" in res.auto_apply
    assert "cooking" not in res.auto_apply
    assert "cooking" not in res.suggest_only


def test_match_existing_tags_single_body_mention_is_suggest_only():
    res = tagger.match_existing_tags(
        title="Unrelated title here",
        excerpt="Unrelated excerpt here",
        url="https://example.com/unrelated",
        body="this article mentions kubernetes once among many other words here",
        user_tags=["kubernetes"],
    )
    assert res.auto_apply == []
    assert "kubernetes" in res.suggest_only


def test_match_existing_tags_two_body_hits_auto_apply():
    body = "kubernetes clusters are great. we run kubernetes in production."
    res = tagger.match_existing_tags(
        title="Unrelated title here",
        excerpt="Unrelated excerpt here",
        url="https://example.com/unrelated",
        body=body,
        user_tags=["kubernetes"],
    )
    assert "kubernetes" in res.auto_apply


def test_match_existing_tags_short_token_needs_title():
    # 'go' the verb in body must not auto-apply the 'go' language tag.
    res = tagger.match_existing_tags(
        title="A walk in the park",
        excerpt="We go places",
        url="https://example.com/walk",
        body="let us go to the park today and have fun together",
        user_tags=["go"],
    )
    assert "go" not in res.auto_apply


def test_match_existing_tags_slug_hit_auto_applies():
    res = tagger.match_existing_tags(
        title="Unrelated title",
        excerpt="Unrelated excerpt",
        url="https://example.com/rust-async-guide",
        body="nothing relevant in the body text here at all",
        user_tags=["rust"],
    )
    assert "rust" in res.auto_apply


def test_suggest_new_tags_finds_multiword_phrase():
    body = (
        "Vector databases power semantic search. "
        "A vector database stores embeddings for similarity lookup. "
        "Teams pick a vector database for edge caching of embeddings."
    )
    out = tagger.suggest_new_tags(
        title="Vector databases",
        excerpt="",
        url="https://example.com/x",
        body=body,
        user_tags=[],
        limit=5,
    )
    phrases = [p.phrase for p in out]
    assert any("vector database" in p for p in phrases)


def test_suggest_new_tags_excludes_existing_tags():
    body = "postgres tuning tips. postgres indexes matter. postgres vacuuming helps."
    out = tagger.suggest_new_tags(
        title="Postgres tuning",
        excerpt="",
        url="https://example.com/x",
        body=body,
        user_tags=["postgres"],
        limit=5,
    )
    assert all(p.phrase != "postgres" for p in out)


def test_suggest_new_tags_empty_body_returns_empty():
    assert (
        tagger.suggest_new_tags(
            title="t", excerpt="", url="https://example.com", body="", user_tags=[]
        )
        == []
    )


def test_heavy_nlp_libs_not_imported_by_tagger():
    import sys

    for mod in ("sklearn", "nltk", "spacy", "keybert"):
        assert mod not in sys.modules, mod
    assert "keepfor.utils.tagger" not in sys.modules or True
    import keepfor.utils.tagger  # noqa: F401

    for mod in ("sklearn", "nltk", "spacy", "keybert"):
        assert mod not in sys.modules, mod


# Three sample articles in the shape the extraction pipeline produces.
# `want` holds the phrases a reader would expect as tags.
ARTICLES = [
    {
        "title": "Why Postgres 16 parallel vacuum changed our write path",
        "excerpt": "A deep dive into autovacuum tuning and dead tuples.",
        "url": "https://example.com/postgres-16-parallel-vacuum",
        "body": """
            Postgres 16 introduced parallel vacuum, which changes the cost model for
            large tables. Before this release, a vacuum of a table with millions of
            dead tuples ran on a single worker and the transaction id wraparound risk
            grew quietly in the background. We operate a multi-tenant Postgres cluster
            where every tenant table is partitioned by customer id, and the autovacuum
            settings were tuned for a much smaller workload. Parallel vacuum let us
            lower the vacuum cost limit without falling behind on bloat. The real win
            was not the speed. It was that our dead tuple ratio finally fell below the
            threshold where the planner started choosing sequential scans over the
            index. We now run vacuum aggressively during low traffic hours and keep the
            autovacuum threshold low enough that bloat never accumulates.
        """,
        "want": {"postgres", "parallel vacuum"},
        "junk": {"parallel vacuum changed", "vacuum cost limit"},
    },
    {
        "title": "Freight operators rethink the supply chain",
        "excerpt": "Ocean carriers add capacity while shippers build resilience.",
        "url": "https://example.com/freight-supply-chain-2026",
        "body": """
            The shipping industry entered the year with freight rates down and capacity
            up. Carriers around the world ordered new vessels, and the resulting supply
            of container space has pushed spot rates lower across the major trade lanes.
            Shippers, for their part, have stopped treating the ocean as a single point
            of failure. Analysts in Rotterdam and Singapore describe the shift as a
            move from just in time to just in case, with importers holding more
            inventory and booking capacity earlier. The logistics providers in between
            are adapting with software that models arrival dates and reroutes cargo
            around port congestion. For the freight operators themselves, the margin
            story depends on how quickly they can turn capacity back around.
        """,
        "want": {"freight operators", "supply chain"},
        "junk": {"supply chain ocean", "carriers around"},
    },
    {
        "title": "Anarchism",
        "excerpt": "A political philosophy and a movement.",
        "url": "https://en.wikipedia.org/wiki/Anarchism",
        "body": """
            Anarchism is a political philosophy and a movement that advocates the
            absence of hierarchical domination. Anarchists argue that the state and
            coercive authority are undesirable, and they advocate the replacement of
            the state with a stateless society. The philosophy has a long and varied
            history, with roots in ancient Greek thinkers and in the religious
            anarchism of the nineteenth century. Anarchism is usually considered a
            radical left-wing political movement. During the twentieth century it was
            widely associated with protests, and many anarchist organisations were
            targeted by state repression.
        """,
        "want": {"anarchism", "political philosophy"},
        "junk": {"anarchism political philosophy", "philosophy and a movement"},
    },
]


def _suggest(article, user_tags=()):
    return tagger.suggest_new_tags(
        title=article["title"],
        excerpt=article["excerpt"],
        url=article["url"],
        body=article["body"],
        user_tags=list(user_tags),
        limit=5,
    )


# A German article in the shape the extraction pipeline produces
# (de.wikipedia.org/wiki/Hamburg). Its suggestions used to be
# "#hamburg, #abgerufen, #hamburg und": the citation boilerplate became a
# topic and a conjunction ended a phrase (issue #58).
GERMAN_ARTICLE = {
    "title": "Hamburg",
    "excerpt": "Stadt und Land der Bundesrepublik Deutschland",
    "url": "https://de.wikipedia.org/wiki/Hamburg",
    "body": """
        Hamburg (niederdeutsch Hamborg), amtlich Freie und Hansestadt Hamburg,
        ist als Stadtstaat ein Land der Bundesrepublik Deutschland. Der amtliche
        Name verweist auf die Geschichte Hamburgs als Freie Reichsstadt und als
        Mitglied des Handelsbundes der Hanse.

        Hamburg ist mit rund 1,87 Millionen Einwohnern die zweitgrößte Stadt
        Deutschlands und die drittgrößte im deutschen Sprachraum. Das Stadtgebiet
        ist in sieben Bezirke und 104 Stadtteile gegliedert, darunter mit dem
        Stadtteil Neuwerk eine in der Nordsee gelegene Inselgruppe.

        Der Hamburger Hafen zählt zu den größten Umschlaghäfen weltweit und macht
        Hamburg zusammen mit dem internationalen Flughafen zu einem bedeutenden
        Logistikstandort. Wirtschaftlich und wissenschaftlich ist die Metropole
        vor allem im Bereich der Luft- und Raumfahrttechnik, der
        Biowissenschaften und der Informationstechnik sowie für die
        Konsumgüterbranche und als Medienstandort bedeutend. Seit 1996 ist
        Hamburg zudem Sitz des Internationalen Seegerichtshofs.

        Die Speicherstadt und das benachbarte Kontorhausviertel sind seit 2015
        Teil des UNESCO-Weltkulturerbes. Weitere bekannte Kulturdenkmäler und
        Wahrzeichen sind das Hamburger Rathaus und die fünf Hauptkirchen.
        International bekannt sind auch das Vergnügungsviertel St. Pauli mit der
        Reeperbahn sowie das 2017 eröffnete Konzerthaus Elbphilharmonie.

        Hamburg liegt in Norddeutschland an den Mündungen der Bille und der
        Alster in die Unterelbe, die etwa 100 Kilometer weiter nordwestlich in
        die Nordsee mündet. An der Elbe erstreckt sich der Tidehafen etwa von
        der Veddel bis Finkenwerder, hauptsächlich auf dem Südufer der
        Norderelbe, gegenüber den Stadtteilen St. Pauli und Altona.

        ↑ § 23 HmbVwVfG – Amtssprache. Hamburgisches Verwaltungsvorschrif-
        tengesetz vom 9. November 1977. Abgerufen am 25. September 2014.

        1 2 3 Kaufkraft in Deutschland 2024. In: acxiom. Archiviert vom
        Original am 7. Juli 2025; abgerufen am 7. Juli 2025.

        ↑ BIP | Statistikportal.de. 30. März 2026, abgerufen am 4. September
        2026. Abgerufen am 4. September 2026.
    """,
}


def test_german_suggestions_never_hold_a_stopword():
    """A German page is cut at German function words (#58)."""
    phrases = [s.phrase for s in _suggest(GERMAN_ARTICLE)]
    assert phrases, "expected the German article to yield suggestions"
    for phrase in phrases:
        words = phrase.split()
        assert not [w for w in words if w in tagger.STOPWORDS_DE], phrase
        assert words[0] not in tagger.STOPWORDS_DE, phrase
        assert words[-1] not in tagger.STOPWORDS_DE, phrase


def test_german_suggestions_drop_citation_boilerplate():
    phrases = {s.phrase for s in _suggest(GERMAN_ARTICLE)}
    assert "abgerufen" not in phrases
    assert "archiviert" not in phrases
    assert not [p for p in phrases if "abgerufen" in p.split()], phrases


def test_german_suggestions_are_topical():
    phrases = {s.phrase for s in _suggest(GERMAN_ARTICLE)}
    assert "hamburg" in phrases
    assert not [p for p in phrases if p.endswith(" und")], phrases
    assert "hamburg und" not in phrases
    for phrase in phrases:
        assert tagger.validate_tag_name(phrase) == phrase, phrase


def test_detect_language_reads_the_stopwords_of_the_page():
    assert (
        tagger.detect_language(
            "Die Stadt Hamburg ist ein Stadtstaat und eine Hansestadt. " * 4
        )
        == "de"
    )
    assert (
        tagger.detect_language(
            "The city of Hamburg is a city state and a free city. " * 4
        )
        == "en"
    )


def test_the_declared_language_wins_over_the_guess():
    body = "Hamburg und der Hafen. " * 5
    german = tagger.suggest_new_tags(
        title="Hamburg",
        excerpt="",
        url="https://de.example.com/hamburg",
        body=body,
        user_tags=[],
        lang="de",
    )
    english = tagger.suggest_new_tags(
        title="Hamburg",
        excerpt="",
        url="https://de.example.com/hamburg",
        body=body,
        user_tags=[],
        lang="en",
    )
    assert {s.phrase for s in german} == {"hamburg", "hafen"}
    assert "hamburg und der" in {s.phrase for s in english}


def test_english_pages_keep_the_english_stopwords():
    """The German list must not leak into English suggestions."""
    for article in ARTICLES:
        for suggestion in _suggest(article):
            words = suggestion.phrase.split()
            assert not [w for w in words if w in tagger.STOPWORDS_DE], (
                article["title"],
                suggestion,
            )


def test_suggestions_never_start_or_end_with_a_stopword():
    for article in ARTICLES:
        for suggestion in _suggest(article):
            words = suggestion.phrase.split()
            assert words[0] not in tagger.STOPWORDS, (article["title"], suggestion)
            assert words[-1] not in tagger.STOPWORDS, (article["title"], suggestion)
            assert not [w for w in words if w in tagger.STOPWORDS], (
                article["title"],
                suggestion,
            )


def test_suggestions_are_topical_phrases_not_fragments():
    for article in ARTICLES:
        phrases = {s.phrase for s in _suggest(article)}
        assert phrases & article["want"], (article["title"], phrases)
        assert not (phrases & article["junk"]), (article["title"], phrases)


def test_suggestions_are_capped():
    for article in ARTICLES:
        assert len(_suggest(article)) <= 5


# Contraction forms of stopwords. The tokeniser keeps them as single words,
# so they must be stoplisted explicitly or they leak into suggestions
# ("tests aren't", "it's").
CONTRACTION_STOPWORDS = {
    "isn't",
    "aren't",
    "wasn't",
    "weren't",
    "don't",
    "doesn't",
    "didn't",
    "can't",
    "couldn't",
    "won't",
    "wouldn't",
    "shouldn't",
    "mustn't",
    "hasn't",
    "haven't",
    "hadn't",
    "it's",
    "that's",
    "there's",
    "here's",
    "what's",
    "let's",
    "i'm",
    "i've",
    "i'd",
    "i'll",
    "he's",
    "she's",
    "you're",
    "we're",
    "they're",
    "we've",
    "they've",
    "we'll",
    "you'll",
    "they'll",
}


def test_contraction_stopwords_never_appear_in_suggestions():
    article = {
        "title": "Flaky Tests Aren't a Bug: It's the Clock",
        "excerpt": "We blamed the code, didn't we? The truth is subtler.",
        "url": "https://example.com/blog/flaky-tests",
        "body": (
            "The suite wasn't slow; it was nondeterministic. The scheduler "
            "wasn't the culprit. Fixing the clock skew turned everything "
            "green. Graceful, wasn't it?"
        ),
    }
    for suggestion in _suggest(article):
        words = suggestion.phrase.split()
        assert not (set(words) & CONTRACTION_STOPWORDS), suggestion


def test_suggestions_prefer_the_users_existing_vocabulary():
    article = ARTICLES[0]
    match = tagger.match_existing_tags(
        title=article["title"],
        excerpt=article["excerpt"],
        url=article["url"],
        body=article["body"],
        user_tags=["postgres"],
    )
    assert "postgres" in match.auto_apply
    # An existing tag is never re-proposed as a new keyphrase.
    phrases = {s.phrase for s in _suggest(article, ["postgres"])}
    assert "postgres" not in phrases
    # A novel phrase that already contains an existing tag loses to it.
    assert not [p for p in phrases if "postgres" in p.split()], phrases


def test_a_repeated_word_is_not_a_phrase():
    body = (
        "Postgres postgres tuning is postgres postgres tuning for postgres "
        "users. Postgres postgres vacuum tuning keeps postgres postgres fast."
    )
    out = tagger.suggest_new_tags(
        title="Postgres tuning",
        excerpt="",
        url="https://example.com/postgres-tuning",
        body=body,
        user_tags=[],
        limit=5,
    )
    assert out, "expected the repeated keyphrase to still yield suggestions"
    assert all(len(set(s.phrase.split())) == len(s.phrase.split()) for s in out), [
        s.phrase for s in out
    ]


def test_suggestions_drop_verb_and_stopword_led_junk_and_prefer_entities():
    """Drop '#bookmark see' and '#woven silk'; prefer known tags & entities (#97)."""
    article = {
        "title": "Lyon textile history",
        "excerpt": "The story of French silk weavers",
        "url": "https://example.com/lyon-silk",
        "body": (
            "We visited the Lyon silk weavers. "
            "They produce fancy woven silk for royal houses. "
            "The technique of woven silk has a long history in France. "
            "Click here to bookmark see all products from Lyon. "
            "Bookmark see more about Lyon. "
            "Lyon is known for its textile heritage."
        ),
    }
    suggestions = tagger.suggest_new_tags(
        title=article["title"],
        excerpt=article["excerpt"],
        url=article["url"],
        body=article["body"],
        user_tags=["textile"],
    )
    phrases = {s.phrase for s in suggestions}
    assert "bookmark see" not in phrases
    assert "woven silk" not in phrases
    assert not any(p.startswith("woven ") for p in phrases)
    assert not any(p.endswith(" see") for p in phrases)
    assert "lyon" in phrases
    assert len(suggestions) <= tagger.MAX_SUGGESTIONS
    assert all(s.score >= tagger.MIN_SUGGESTION_SCORE for s in suggestions)


def test_is_junk_tag_filters_generic_verbs_and_participles():
    verbs = [
        "retrieved",
        "retrieve",
        "retrieves",
        "retrieving",
        "archived",
        "archive",
        "archives",
        "archiving",
        "accessed",
        "access",
        "accesses",
        "accessing",
        "published",
        "publish",
        "publishes",
        "publishing",
        "updated",
        "update",
        "updates",
        "updating",
        "posted",
        "post",
        "posts",
        "posting",
        "modified",
        "edited",
        "viewed",
        "downloaded",
        "uploaded",
        "cited",
        "shared",
        "recorded",
        "printed",
        "subscribed",
        "submitted",
        "abgerufen",
        "aufgerufen",
        "archiviert",
        "zitiert",
    ]
    for v in verbs:
        assert tagger.is_junk_tag(v) is True, f"expected {v} to be junk"
        assert tagger.is_junk_tag(f"  {v.upper()}  ") is True


def test_is_junk_tag_filters_months():
    months = [
        "january",
        "jan",
        "february",
        "feb",
        "march",
        "mar",
        "april",
        "apr",
        "may",
        "june",
        "jun",
        "july",
        "jul",
        "august",
        "aug",
        "september",
        "sep",
        "sept",
        "october",
        "oct",
        "november",
        "nov",
        "december",
        "dec",
        "januar",
        "februar",
        "märz",
        "mär",
        "mai",
        "juni",
        "juli",
        "oktober",
        "okt",
        "dezember",
        "dez",
    ]
    for m in months:
        assert tagger.is_junk_tag(m) is True, f"expected {m} to be junk"


def test_is_junk_tag_filters_days():
    days = [
        "monday",
        "mon",
        "tuesday",
        "tue",
        "tues",
        "wednesday",
        "wed",
        "thursday",
        "thu",
        "thur",
        "thurs",
        "friday",
        "fri",
        "saturday",
        "sat",
        "sunday",
        "sun",
        "montag",
        "dienstag",
        "mittwoch",
        "donnerstag",
        "freitag",
        "samstag",
        "sonnabend",
        "sonntag",
    ]
    for d in days:
        assert tagger.is_junk_tag(d) is True, f"expected {d} to be junk"


def test_is_junk_tag_filters_url_fragments():
    fragments = [
        "www",
        "www2",
        "http",
        "https",
        "com",
        "org",
        "net",
        "edu",
        "gov",
        "mil",
        "int",
        "html",
        "htm",
        "php",
        "aspx",
        "asp",
        "jsp",
        "cgi",
        "url",
        "href",
        "amp",
        "nbsp",
        "ftp",
        "cdn",
        "uri",
        "src",
        "xml",
        "json",
        "css",
        "js",
    ]
    for frag in fragments:
        assert tagger.is_junk_tag(frag) is True, f"expected {frag} to be junk"


def test_is_junk_tag_filters_short_tokens_and_numbers():
    short_or_nums = ["", "a", "b", "ai", "go", "to", "in", "1", "42", "2024", "999"]
    for s in short_or_nums:
        assert tagger.is_junk_tag(s) is True, f"expected '{s}' to be junk"


def test_is_junk_tag_filters_multi_token_junk():
    junk_combos = [
        "april 2024",
        "retrieved 2024",
        "accessed april",
        "www example",
        "site com",
        "article 2024",
        "2024 article",
        "published monday",
        "http link",
    ]
    for j in junk_combos:
        assert tagger.is_junk_tag(j) is True, f"expected '{j}' to be junk"


def test_is_junk_tag_allows_valid_tags():
    valid = [
        "python",
        "machine-learning",
        "parallel vacuum",
        "data science",
        "kubernetes",
        "distributed systems",
    ]
    for v in valid:
        assert tagger.is_junk_tag(v) is False, f"expected '{v}' to be valid"


def test_suggest_new_tags_filters_junk_classes_and_caps_at_five():
    article = {
        "title": "Vector databases and distributed systems in 2024",
        "excerpt": "Published on Monday, April 15, 2024. Retrieved from https://www.example.com/guide.html",
        "url": "https://www.example.com/vector-databases-guide.html",
        "body": (
            "Vector databases are essential for AI applications. "
            "A vector database indexes high-dimensional vectors efficiently. "
            "Distributed systems scale vector database clusters. "
            "Published by author. Updated frequently. Accessed 42 times. "
            "Retrieved on Friday. Archived in October. Posted on blog. "
            "Visit www com or http net html for info. Go to site 2024."
        ),
    }
    suggestions = tagger.suggest_new_tags(
        title=article["title"],
        excerpt=article["excerpt"],
        url=article["url"],
        body=article["body"],
        user_tags=[],
    )
    phrases = {s.phrase for s in suggestions}

    # Verify no generic verbs/participles
    for verb in ["published", "updated", "retrieved", "archived", "posted", "accessed"]:
        assert verb not in phrases
        assert not any(verb in p.split() for p in phrases)

    # Verify no months or days
    for date_word in ["april", "october", "monday", "friday"]:
        assert date_word not in phrases
        assert not any(date_word in p.split() for p in phrases)

    # Verify no URL fragments
    for frag in ["www", "http", "https", "com", "net", "html"]:
        assert frag not in phrases
        assert not any(frag in p.split() for p in phrases)

    # Verify no short tokens or pure numbers
    for token in ["ai", "go", "42", "2024", "15"]:
        assert token not in phrases
        assert not any(token == p or token in p.split() for p in phrases)

    # Verify valid keyphrases made it
    assert "vector database" in phrases or "vector databases" in phrases
    # Verify max 5 suggestions
    assert len(suggestions) <= 5
    assert len(suggestions) > 0
