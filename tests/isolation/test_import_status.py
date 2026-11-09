# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tenant isolation for the persisted import status (GitHub issue #18).

Enforces:
- An import record belongs to the user who uploaded the file: another
  tenant can neither read it nor see it rendered.
- A chunk reported by another tenant never touches a foreign record.
"""

from __future__ import annotations

import pytest

from keepfor.models.imports import (
    LinkSkipReason,
    apply_chunk_counts,
    create_import,
    get_import,
    get_import_detail,
    list_import_items,
    list_imports,
    record_import_items,
    retry_all_failed_import_items,
    retry_import_item,
    set_import_counts,
)
from tests.isolation.conftest import IsolationHarness, snapshot


async def _seed_import(db, user_id: str, filename: str) -> str:
    import_id = await create_import(db, user_id, source="keep", filename=filename)
    await set_import_counts(
        db,
        user_id,
        import_id,
        source="keep",
        notes_total=3,
        trashed_skipped=0,
        urls_found=1,
        attachments_not_imported=0,
        parse_errors=0,
    )
    return import_id


@pytest.mark.asyncio
async def test_import_status_is_scoped_to_its_owner(harness: IsolationHarness):
    """Tenant B never sees tenant A's import records."""
    a = harness.tenant_a
    b = harness.tenant_b

    import_id = await _seed_import(a.db, a.user["id"], "tenant-a-keep.zip")

    assert [r["id"] for r in await list_imports(a.db, a.user["id"])] == [import_id]
    assert await list_imports(b.db, b.user["id"]) == []
    # Same database, other user: the user_id filter is what isolates them.
    assert await list_imports(a.db, b.user["id"]) == []
    assert await get_import(b.db, b.user["id"], import_id) is None


@pytest.mark.asyncio
async def test_foreign_chunk_never_updates_another_tenants_record(
    harness: IsolationHarness,
):
    """Reporting a chunk with a foreign user_id leaves the record untouched."""
    a = harness.tenant_a
    b = harness.tenant_b

    import_id = await _seed_import(a.db, a.user["id"], "tenant-a-keep.zip")
    before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    await apply_chunk_counts(
        b.db,
        b.user["id"],
        import_id,
        {"notes_imported": 99, "notes_processed": 99, "duplicates_skipped": 99},
    )

    record = await get_import(a.db, a.user["id"], import_id)
    assert record["notes_imported"] == 0
    assert record["notes_processed"] == 0
    assert record["duplicates_skipped"] == 0
    after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert after == before


@pytest.mark.asyncio
async def test_import_status_is_not_rendered_for_another_tenant(
    harness: IsolationHarness,
):
    """The settings page of tenant B shows none of tenant A's imports."""
    a = harness.tenant_a
    b = harness.tenant_b

    await _seed_import(a.db, a.user["id"], "tenant-a-keep.zip")

    # /settings/* refuses PATs outright, so this goes through the session.
    harness.client.cookies.clear()
    page = harness.client.get("/settings", cookies=b.cookies)
    assert page.status_code == 200
    assert "tenant-a-keep.zip" not in page.text

    harness.client.cookies.clear()
    page = harness.client.get("/", headers=b.auth_headers)
    assert page.status_code == 200
    assert "tenant-a-keep.zip" not in page.text

    # The owner does see it.
    harness.client.cookies.clear()
    page = harness.client.get("/settings", cookies=a.cookies)
    assert page.status_code == 200
    assert "tenant-a-keep.zip" in page.text


@pytest.mark.asyncio
async def test_import_items_and_detail_routes_are_scoped_to_owner(
    harness: IsolationHarness,
):
    """Tenant B gets 404 on Tenant A's import status, detail, and retry routes."""
    a = harness.tenant_a
    b = harness.tenant_b

    import_id = await _seed_import(a.db, a.user["id"], "tenant-a-keep.zip")
    row_ids = await record_import_items(
        a.db,
        a.user["id"],
        import_id,
        [
            {
                "kind": "link",
                "state": "skipped",
                "url": "https://example.com/tenant-a-secret-link",
                "reason": LinkSkipReason.QUEUE_FAILED.value,
            }
        ],
    )
    row_id = row_ids[0]
    before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    # Model-level checks for Tenant B
    assert await list_import_items(b.db, b.user["id"], import_id) == []
    assert await get_import_detail(b.db, b.user["id"], import_id) is None
    res_item = await retry_import_item(
        b.db, harness.env, b.user["id"], import_id, row_id, scope=b.scope
    )
    assert res_item["status"] == "not_found"
    res_all = await retry_all_failed_import_items(
        b.db, harness.env, b.user["id"], import_id, scope=b.scope
    )
    assert res_all["status"] == "not_found"

    # HTTP route checks for Tenant B (must 404 and leave Tenant A untouched)
    harness.client.cookies.clear()
    r_status = harness.client.get(f"/imports/{import_id}/status", cookies=b.cookies)
    assert r_status.status_code == 404
    assert "tenant-a" not in r_status.text

    r_detail = harness.client.get(f"/imports/{import_id}", cookies=b.cookies)
    assert r_detail.status_code == 404
    assert "tenant-a-secret-link" not in r_detail.text

    r_retry_one = harness.client.post(
        f"/imports/{import_id}/items/{row_id}/retry",
        cookies=b.cookies,
        follow_redirects=False,
    )
    assert r_retry_one.status_code == 404

    r_retry_all = harness.client.post(
        f"/imports/{import_id}/retry",
        cookies=b.cookies,
        follow_redirects=False,
    )
    assert r_retry_all.status_code == 404

    after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert after == before

    # Owner can view status and detail
    harness.client.cookies.clear()
    owner_status = harness.client.get(f"/imports/{import_id}/status", cookies=a.cookies)
    assert owner_status.status_code == 200
    owner_detail = harness.client.get(f"/imports/{import_id}", cookies=a.cookies)
    assert owner_detail.status_code == 200
    assert "https://example.com/tenant-a-secret-link" in owner_detail.text
