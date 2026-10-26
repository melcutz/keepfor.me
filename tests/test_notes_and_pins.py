# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for notes, pins, user notes, archiving, and cover metadata (0006)."""

import pytest

from keepfor.auth.service import register_user
from keepfor.consumer.extractor import extract_article, extract_image_url
from keepfor.models.items import (
    archive_item,
    get_item,
    save_item,
    save_note,
    toggle_pin_item,
    unarchive_item,
    update_user_notes,
)
from keepfor.search.engine import get_recent_items, hybrid_search


class FakeQueue:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


@pytest.fixture
async def user_with_env(db):
    user = await register_user(db, "notes@keepfor.me", "password123")
    return user, MockEnv(), db


@pytest.mark.asyncio
async def test_migration_0006_columns(db):
    rows = await db.query_all("PRAGMA table_info(items);")
    names = {r["name"] for r in rows}
    for col in (
        "item_type",
        "is_pinned",
        "user_notes",
        "image_url",
        "summary",
        "read_state",
    ):
        assert col in names


@pytest.mark.asyncio
async def test_toggle_pin_item(user_with_env):
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/pin1", [])
    assert await toggle_pin_item(db, user["id"], item["id"]) is True
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["is_pinned"] == 1
    assert await toggle_pin_item(db, user["id"], item["id"]) is False
    assert await toggle_pin_item(db, user["id"], "missing") is None


@pytest.mark.asyncio
async def test_save_note(user_with_env):
    user, env, db = user_with_env
    note = await save_note(
        db, env, user["id"], "My idea", "# Hello\nSome body text", ["ideas"]
    )
    assert note["item_type"] == "note"
    fetched = await get_item(db, user["id"], note["id"])
    assert fetched["item_type"] == "note"
    assert fetched["status"] == "saved"
    # FTS5 presence
    rows = await db.query_all(
        "SELECT item_id FROM items_fts WHERE item_id = ?;", (note["id"],)
    )
    assert rows
    # Unified search finds the note
    items, _ = await hybrid_search(db, env, user["id"], query="Hello")
    assert any(i["id"] == note["id"] for i in items)


@pytest.mark.asyncio
async def test_update_user_notes(user_with_env):
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/n", [])
    assert await update_user_notes(db, user["id"], item["id"], "Use for D1 migration")
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["user_notes"] == "Use for D1 migration"
    assert await update_user_notes(db, user["id"], "missing", "x") is False


@pytest.mark.asyncio
async def test_archive_item(user_with_env):
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/r", [])
    assert await archive_item(db, user["id"], item["id"]) is True
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["read_state"] == "archived"
    assert await unarchive_item(db, user["id"], item["id"]) is True
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["read_state"] == "unread"


@pytest.mark.asyncio
async def test_category_tabs(user_with_env):
    user, env, db = user_with_env
    url_item, _ = await save_item(db, env, user["id"], "https://example.com/post", [])
    note = await save_note(db, env, user["id"], "N", "body text", [])
    await archive_item(db, user["id"], url_item["id"])
    notes = await get_recent_items(db, user["id"], category="notes")
    assert {i["id"] for i in notes} == {note["id"]}
    archived = await get_recent_items(db, user["id"], category="archive")
    assert {i["id"] for i in archived} == {url_item["id"]}


def test_extractor_cover_image():
    html = (
        "<html><head>"
        '<meta property="og:image" content="https://example.com/cover.jpg">'
        "<title>T</title></head><body><p>text</p></body></html>"
    )
    assert extract_image_url(html) == "https://example.com/cover.jpg"
    extracted = extract_article(html, "https://example.com/t")
    assert extracted["image_url"] == "https://example.com/cover.jpg"


@pytest.mark.asyncio
async def test_copy_markdown_endpoint(user_with_env):
    user, env, db = user_with_env
    from keepfor.app import build_ai_markdown

    item, _ = await save_item(db, env, user["id"], "https://example.com/m", ["ai"])
    await update_user_notes(db, user["id"], item["id"], "Because reasons")
    fetched = await get_item(db, user["id"], item["id"])
    md = build_ai_markdown(fetched)
    assert md.startswith("# ")
    assert "Source:" in md
    assert "#ai" in md
    assert "Because reasons" in md


@pytest.mark.asyncio
async def test_mcp_save_note_and_pin(user_with_env):
    user, env, db = user_with_env
    from keepfor.mcp.server import handle_tool_call

    res = await handle_tool_call(
        "save_note",
        {"title": "T", "content": "hello world"},
        db,
        env,
        {"id": user["id"]},
    )
    assert res["item_type"] == "note"
    pin = await handle_tool_call(
        "pin_item",
        {"item_id": res["id"], "pinned": True},
        db,
        env,
        {"id": user["id"]},
    )
    assert pin["is_pinned"] is True
    got = await handle_tool_call(
        "get_item", {"item_id": res["id"]}, db, env, {"id": user["id"]}
    )
    assert got["item_type"] == "note"
    assert got["is_pinned"] is True


@pytest.mark.asyncio
async def test_save_note_enforces_entitlements_and_embeds_all_chunks(
    user_with_env, monkeypatch
):
    from keepfor.spi import Decision, EntitlementDenied, Providers, TenantScope

    user, env, db = user_with_env

    upserted: list[dict] = []
    embedded_batches: list[list[str]] = []
    checked: list[tuple[str, str, int]] = []
    recorded: list[tuple[str, str, int]] = []

    class FakeVectorIndex:
        async def upsert(self, vectors):
            upserted.extend(vectors)

    class FakeAI:
        async def embed(self, env_arg, texts):
            embedded_batches.append(list(texts))
            return [[0.1, 0.2] for _ in texts]

    allow_save = True

    class FakeEntitlements:
        async def check(self, tenant_id: str, action: str, qty: int = 1) -> Decision:
            checked.append((tenant_id, action, qty))
            if action == "save_item" and not allow_save:
                return Decision(allowed=False, reason="Note quota exceeded")
            return Decision(allowed=True)

        async def record(self, tenant_id: str, action: str, qty: int = 1) -> None:
            recorded.append((tenant_id, action, qty))

    from keepfor.runtime import get_providers

    base = get_providers()
    monkeypatch.setattr(
        "keepfor.runtime.get_providers",
        lambda: Providers(
            scope=base.scope,
            entitlements=FakeEntitlements(),  # type: ignore[arg-type]
            events=base.events,
            ai=FakeAI(),  # type: ignore[arg-type]
        ),
    )

    env.VECTORIZE = FakeVectorIndex()
    scope = TenantScope(
        tenant_id="tenant_notes",
        user_id=user["id"],
        db=db,
        blob_prefix="t/",
        vector_namespace="ns_notes",
    )

    # Create a note long enough for multiple chunks (> 400 tokens)
    long_body = "\n\n".join(
        f"Paragraph {i}: " + ("knowledge base word " * 60) for i in range(6)
    )
    note = await save_note(
        db, env, user["id"], "Long Note", long_body, ["deep"], scope=scope
    )

    chunk_rows = await db.query_all(
        "SELECT chunk_index FROM chunks WHERE item_id = ? ORDER BY chunk_index;",
        (note["id"],),
    )
    assert len(chunk_rows) >= 2
    assert len(upserted) == len(chunk_rows)
    assert ("tenant_notes", "save_item", 1) in checked
    assert ("tenant_notes", "save_item", 1) in recorded
    assert ("tenant_notes", "embed", len(chunk_rows)) in checked
    assert ("tenant_notes", "embed", len(chunk_rows)) in recorded

    # Denied save_item raises EntitlementDenied
    allow_save = False
    with pytest.raises(EntitlementDenied):
        await save_note(db, env, user["id"], "Denied", "body", scope=scope)


@pytest.mark.asyncio
async def test_workers_ai_provider_generate_unwraps_jsproxy():
    from keepfor.defaults import WorkersAIProvider

    class FakeJsProxy:
        def to_py(self):
            return {"response": "  hello from pyodide  "}

    class FakeAIBinding:
        async def run(self, model, payload):
            return FakeJsProxy()

    class EnvWithAI:
        AI = FakeAIBinding()

    provider = WorkersAIProvider()
    out = await provider.generate(EnvWithAI(), "sys", "usr")
    assert out == "hello from pyodide"


@pytest.mark.asyncio
async def test_update_user_notes_enforces_cap_and_reindexes_vectors(
    user_with_env, monkeypatch
):
    from keepfor.models.items import MAX_ITEM_NOTES_CHARS
    from keepfor.spi import Providers, TenantScope

    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/notes-vec", [])

    upserted = []

    class FakeVectorIndex:
        async def upsert(self, vectors):
            upserted.extend(vectors)

    class FakeAI:
        async def embed(self, env_arg, texts):
            return [[0.3, 0.7] for _ in texts]

    from keepfor.runtime import get_providers

    base = get_providers()
    monkeypatch.setattr(
        "keepfor.runtime.get_providers",
        lambda: Providers(
            scope=base.scope,
            entitlements=base.entitlements,
            events=base.events,
            ai=FakeAI(),  # type: ignore[arg-type]
        ),
    )

    env.VECTORIZE = FakeVectorIndex()
    scope = TenantScope(
        tenant_id="t1",
        user_id=user["id"],
        db=db,
        blob_prefix="t/",
        vector_namespace="ns_notes",
    )

    ok = await update_user_notes(
        db,
        user["id"],
        item["id"],
        "Updated note for vector index",
        env=env,
        scope=scope,
    )
    assert ok is True
    assert len(upserted) == 1

    with pytest.raises(ValueError, match="limited to"):
        await update_user_notes(
            db, user["id"], item["id"], "x" * (MAX_ITEM_NOTES_CHARS + 1)
        )
