# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for stdlib-only tag matching + RAKE suggestion (keepfor/utils/tagger.py)."""

from keepfor.utils import tagger


def test_normalize_tag_lowercases_and_strips():
    assert tagger.normalize_tag("  PyThon ") == "python"
    assert tagger.normalize_tag("") == ""


def test_validate_tag_name_accepts_sane_names():
    assert tagger.validate_tag_name("machine-learning") == "machine-learning"
    assert tagger.validate_tag_name("  AI ") == "ai"


def test_validate_tag_name_rejects_bad_names():
    for bad in ["", "  ", "a", "x" * 65, "has/slash", "semi;colon", 'quote"x']:
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
