# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for punctuated, symbol-bearing, and single-letter search queries (#132)."""

import uuid

import pytest

from keepfor.auth.service import login_user, register_user
from keepfor.search.engine import (
    _content_terms,
    _query_terms,
    highlight_query_terms,
    hybrid_search,
    search_fts,
    search_fts_whole_words,
    search_tag_hits,
)

# Test library covering:
# - c++, c#, f# in title, content_text, and tags (including #c++, #c#)
# - false-positive traps: standalone "c", "f", "Music#1"
# - hyphenated/punctuated terms: self-hosted, tcp/ip, ci/cd, node.js, r&d
# - single-ASCII-letter prefix terms: z-wave, e-mail, x-ray
# - false-positive traps for single-letter prefixes: standalone "z", "e", "x", "r", "d"
# - single CJK character: "猫"
LIBRARY = (
    (
        "cpp_title",
        "Modern C++ Concurrency",
        "Patterns for safe multithreading",
        (),
    ),
    (
        "cpp_tag",
        "Systems Programming Notes",
        "Memory safety and RAII patterns",
        ("#c++",),
    ),
    (
        "csharp_text",
        "Dotnet Runtime Overview",
        "Writing async services in C# with minimal allocations",
        (),
    ),
    (
        "csharp_tag",
        "LINQ Deep Dive",
        "Query expressions and deferred execution",
        ("#c#",),
    ),
    (
        "fsharp_title",
        "Functional Domain Modeling in F#",
        "Algebraic data types for business logic",
        (),
    ),
    (
        "bare_c",
        "Copyright (C) 2026 Notice",
        "Press Ctrl+C to exit; section C covers licensing",
        ("c",),
    ),
    (
        "bare_f",
        "Keyboard Shortcut F",
        "Press F to toggle fullscreen mode",
        ("f",),
    ),
    (
        "music_sharp",
        "Music#1 Playlist",
        "Acoustic tracks in Music#1 collection",
        (),
    ),
    (
        "zwave_item",
        "Z-Wave Mesh Guide",
        "Configuring Z-Wave smart home sensors",
        (),
    ),
    (
        "bare_z",
        "Point Z Coordinates",
        "Axis z calibration in 3D space",
        (),
    ),
    (
        "email_item",
        "E-Mail Deliverability",
        "SPF and DKIM setup for e-mail servers",
        (),
    ),
    (
        "bare_e",
        "Vitamin E Research",
        "Notes on constant e in mathematics",
        (),
    ),
    (
        "xray_item",
        "X-Ray Diffraction",
        "Crystallography with x-ray beams",
        (),
    ),
    (
        "bare_x",
        "Algebraic Variable X",
        "Solving for x in linear equations",
        (),
    ),
    (
        "self_hosted",
        "Self-Hosted Bookmarks",
        "Running self-hosted web apps on a home server",
        (),
    ),
    (
        "hosted_only",
        "Hosted Cloud Services",
        "Managed cloud hosting platform",
        (),
    ),
    (
        "tcp_ip",
        "TCP/IP Illustrated",
        "Packet routing across TCP/IP networks",
        (),
    ),
    (
        "nodejs",
        "Node.js Event Loop",
        "Understanding timers and microtasks in Node.js",
        (),
    ),
    (
        "rd_both",
        "R&D Tax Credits",
        "Accounting rules for R&D teams",
        (),
    ),
    (
        "r_only",
        "The R Language",
        "Statistical computing with r packages",
        (),
    ),
    (
        "d_only",
        "Vitamin D Guide",
        "Daily intake of vitamin d",
        (),
    ),
    (
        "cjk_cat",
        "猫 について",
        "かわいい 猫 の写真集",
        (),
    ),
)


async def _insert_item(db, user_id, key, title, text, tags=()):
    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, content_text,"
        " status) VALUES (?, ?, ?, ?, ?, ?, 'saved');",
        (
            item_id,
            user_id,
            f"https://example.com/{key}",
            f"https://example.com/{key}",
            title,
            text,
        ),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item_id, user_id, title, text),
    )
    for tag in tags:
        tag_id = f"tag-{tag}"
        await db.execute(
            "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
            (tag_id, user_id, tag),
        )
        await db.execute(
            "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?);",
            (item_id, tag_id),
        )
    return item_id


@pytest.fixture
async def library(db):
    await register_user(db, "punct@keepfor.me", "password123")
    user, _ = await login_user(db, "punct@keepfor.me", "password123")
    ids = {}
    for key, title, text, tags in LIBRARY:
        ids[key] = await _insert_item(db, user["id"], key, title, text, tags)
    return {"db": db, "user": user, "ids": ids}


# --- Unit tests for _query_terms and _content_terms ---


def test_query_terms_splits_on_punctuation_separators():
    assert _query_terms("self-hosted") == ["self", "hosted"]
    assert _query_terms("tcp/ip") == ["tcp", "ip"]
    assert _query_terms("r&d") == ["r", "d"]
    assert _query_terms("node.js") == ["node", "js"]
    assert _query_terms("ci/cd") == ["ci", "cd"]
    assert _query_terms("foo_bar") == ["foo", "bar"]
    assert _query_terms("foo:bar") == ["foo", "bar"]


def test_content_terms_drops_single_ascii_letter_prefixes_with_main_word():
    assert _content_terms("z-wave") == ["wave"]
    assert _content_terms("e-mail") == ["mail"]
    assert _content_terms("x-ray") == ["ray"]


def test_content_terms_preserves_single_cjk_and_all_single_ascii_queries():
    assert _content_terms("猫") == ["猫"]
    assert _content_terms("r&d") == ["r", "d"]


def test_highlight_query_terms_does_not_highlight_bare_letter_for_symbol_query():
    text = "Copyright (C) 2026 and C++ guide"
    highlighted = highlight_query_terms(text, ["c++"])
    assert "<mark>C</mark>" not in highlighted
    assert "<mark>C++</mark>" in highlighted


# --- Integration tests for symbol queries (c++, c#, f#) ---


@pytest.mark.parametrize(
    ("query", "expected_keys", "excluded_keys"),
    [
        ("c++", {"cpp_title", "cpp_tag"}, {"bare_c", "csharp_text"}),
        ("c#", {"csharp_text", "csharp_tag"}, {"bare_c", "music_sharp", "cpp_title"}),
        ("f#", {"fsharp_title"}, {"bare_f", "csharp_text"}),
    ],
)
async def test_symbol_queries_match_literally_across_seams(
    library, query, expected_keys, excluded_keys
):
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    expected_ids = {ids[k] for k in expected_keys}
    excluded_ids = {ids[k] for k in excluded_keys}

    fts_rows = await search_fts(db, user_id, query)
    fts_ids = {r["item_id"] for r in fts_rows}
    assert expected_ids <= fts_ids
    assert not (fts_ids & excluded_ids)

    whole_ids = await search_fts_whole_words(db, user_id, query)
    assert expected_ids <= whole_ids
    assert not (whole_ids & excluded_ids)

    items, _ = await hybrid_search(db, None, user_id, query, mode="keyword")
    hybrid_ids = {item["id"] for item in items}
    assert expected_ids <= hybrid_ids
    assert not (hybrid_ids & excluded_ids)


async def test_symbol_queries_match_tags_in_search_tag_hits(library):
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    cpp_tags = {r["item_id"] for r in await search_tag_hits(db, user_id, "c++")}
    assert ids["cpp_tag"] in cpp_tags
    assert ids["bare_c"] not in cpp_tags

    csharp_tags = {r["item_id"] for r in await search_tag_hits(db, user_id, "c#")}
    assert ids["csharp_tag"] in csharp_tags
    assert ids["bare_c"] not in csharp_tags


# --- Integration tests for single-ASCII-letter prefix queries ---


@pytest.mark.parametrize(
    ("query", "expected_key", "excluded_key"),
    [
        ("z-wave", "zwave_item", "bare_z"),
        ("e-mail", "email_item", "bare_e"),
        ("x-ray", "xray_item", "bare_x"),
    ],
)
async def test_single_ascii_prefix_queries_exclude_bare_letter_items(
    library, query, expected_key, excluded_key
):
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    items, _ = await hybrid_search(db, None, user_id, query, mode="keyword")
    result_ids = {item["id"] for item in items}
    assert ids[expected_key] in result_ids
    assert ids[excluded_key] not in result_ids


# --- Integration tests for punctuated / separator-split / CJK queries ---


@pytest.mark.parametrize(
    ("query", "expected_key"),
    [
        ("self-hosted", "self_hosted"),
        ("tcp/ip", "tcp_ip"),
        ("node.js", "nodejs"),
    ],
)
async def test_separator_queries_find_matching_items(library, query, expected_key):
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    items, _ = await hybrid_search(db, None, user_id, query, mode="keyword")
    result_ids = {item["id"] for item in items}
    assert ids[expected_key] in result_ids


async def test_all_single_ascii_query_requires_all_letters(library):
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    items, _ = await hybrid_search(db, None, user_id, "r&d", mode="keyword")
    result_ids = {item["id"] for item in items}
    assert ids["rd_both"] in result_ids
    assert ids["r_only"] not in result_ids
    assert ids["d_only"] not in result_ids


async def test_single_cjk_character_query_matches(library):
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    items, _ = await hybrid_search(db, None, user_id, "猫", mode="keyword")
    result_ids = {item["id"] for item in items}
    assert ids["cjk_cat"] in result_ids


async def test_single_compound_token_excludes_partial_subterm_matches(library):
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    items, _ = await hybrid_search(db, None, user_id, "self-hosted", mode="keyword")
    result_ids = {item["id"] for item in items}
    assert ids["self_hosted"] in result_ids
    assert ids["hosted_only"] not in result_ids
