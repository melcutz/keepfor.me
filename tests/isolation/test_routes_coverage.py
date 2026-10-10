# SPDX-License-Identifier: AGPL-3.0-or-later
"""Route-table coverage test for tenant isolation.

Enforces:
- Every non-public route in the application must either be exercised by the
  isolation test suite (EXERCISED_ROUTES) or explicitly classified in NOT_TENANT_DATA
  with a documented reason.
- Newly added routes fail this test until audited and classified.
"""

from __future__ import annotations

from typing import Any

from fastapi.routing import APIRoute

from keepfor.app import create_app

# Routes that are open to the public without authentication or static assets / docs
PUBLIC_ROUTES: set[tuple[str, str]] = {
    ("GET", "/openapi.json"),
    ("HEAD", "/openapi.json"),
    ("GET", "/docs"),
    ("HEAD", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("HEAD", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
    ("HEAD", "/redoc"),
    ("GET", "/favicon.ico"),
    ("GET", "/icon-192.png"),
    ("GET", "/icon-512.png"),
    ("GET", "/icon-maskable.png"),
    ("GET", "/manifest.webmanifest"),
    ("GET", "/sw.js"),
    ("GET", "/auth/login"),
    ("POST", "/auth/login"),
    ("GET", "/auth/logout"),
    ("POST", "/auth/logout"),
    ("GET", "/auth/register"),
    ("POST", "/auth/register"),
}

# Routes that are explicitly exercised across tenants in tests/isolation/
EXERCISED_ROUTES: set[tuple[str, str]] = {
    ("GET", "/"),
    ("GET", "/stats"),
    ("GET", "/tags"),
    ("GET", "/api/items"),
    ("GET", "/api/export"),
    ("POST", "/search"),
    ("POST", "/api/search"),
    ("GET", "/api/mcp"),
    ("POST", "/api/mcp"),
    ("DELETE", "/api/mcp"),
    ("GET", "/items/{item_id}"),
    ("GET", "/items/{item_id}/card"),
    ("GET", "/items/{item_id}/markdown"),
    ("POST", "/items/{item_id}/pin"),
    ("POST", "/items/{item_id}/notes"),
    ("POST", "/items/{item_id}/archive"),
    ("POST", "/items/{item_id}/unarchive"),
    ("POST", "/items/{item_id}/tags"),
    ("POST", "/items/{item_id}/suggestions/{sugg_id}/accept"),
    ("POST", "/items/{item_id}/suggestions/{sugg_id}/dismiss"),
    ("POST", "/items/bulk-tags"),
    ("GET", "/api/items/{item_id}"),
    ("DELETE", "/api/items/{item_id}"),
    ("GET", "/api/items/{item_id}/content"),
    ("POST", "/tags/rename"),
    ("POST", "/tags/merge"),
    ("POST", "/tags/delete"),
    ("POST", "/tags/prune"),
    ("POST", "/tags/rules/delete"),
    ("POST", "/settings/tokens/{pat_id}/delete"),
}

# Routes that do not carry cross-tenant data risks, with documented reasons
NOT_TENANT_DATA: dict[tuple[str, str], str] = {
    (
        "GET",
        "/settings",
    ): "Renders personal settings and account overview scoped to authenticated caller",
    (
        "POST",
        "/settings/tokens",
    ): "Generates a new Personal Access Token scoped strictly to authenticated caller",
    (
        "POST",
        "/settings/cleanup-failed",
    ): "Deletes failed items scoped strictly to calling user_id",
    (
        "POST",
        "/admin/requeue",
    ): "Re-enqueues queued items scoped strictly to calling user_id",
    (
        "GET",
        "/save-popup",
    ): "Renders bookmarklet/extension save popup UI for authenticated caller",
    ("POST", "/save-popup"): "Saves a URL to caller's library and enqueues extraction",
    ("GET", "/share"): "Renders mobile share target UI for authenticated caller",
    ("GET", "/share/saved"): "Renders share confirmation UI for authenticated caller",
    ("POST", "/save"): "UI form endpoint to save a URL to caller's personal library",
    ("POST", "/api/save"): "API endpoint to save a URL to caller's personal library",
    ("POST", "/api/items"): "API endpoint to save an item to caller's personal library",
    (
        "POST",
        "/notes",
    ): "Creates a new note/markdown snippet in caller's personal library",
    (
        "POST",
        "/import",
    ): "Uploads and imports bookmarks strictly into caller's personal library",
    ("POST", "/tags/create"): "Creates a new tag name strictly for calling user_id",
    (
        "GET",
        "/tags/suggest",
    ): "Autocompletes tag names based on caller's own existing tags",
    (
        "POST",
        "/tags/rules/create",
    ): "Creates an auto-tagging rule strictly for calling user_id",
    (
        "POST",
        "/tags/rules/suggestions/dismiss",
    ): "Dismisses a tag rule suggestion for calling user_id",
}


def _extract_all_routes(router: Any) -> list[tuple[str, str]]:
    """Recursively extracts (method, path) tuples from a FastAPI app or router."""
    routes: list[tuple[str, str]] = []
    for r in getattr(router, "routes", []):
        if isinstance(r, APIRoute):
            for m in r.methods:
                routes.append((m, r.path))
        elif hasattr(r, "original_router"):
            routes.extend(_extract_all_routes(r.original_router))
        elif hasattr(r, "routes"):
            routes.extend(_extract_all_routes(r))
        elif hasattr(r, "path") and hasattr(r, "methods"):
            for m in r.methods:
                routes.append((m, r.path))
    return routes


def test_routes_coverage():
    """Verify that every route in the app is either exercised

    or explicitly classified in NOT_TENANT_DATA.
    """
    app = create_app()
    all_routes = set(_extract_all_routes(app))

    uncovered: list[tuple[str, str]] = []
    for method, path in sorted(all_routes):
        entry = (method, path)
        if entry in PUBLIC_ROUTES:
            continue
        if entry in EXERCISED_ROUTES:
            continue
        if entry in NOT_TENANT_DATA:
            # Must have non-empty documented reason
            assert NOT_TENANT_DATA[entry].strip(), (
                f"Reason for {entry} must not be empty"
            )
            continue
        uncovered.append(entry)

    assert not uncovered, (
        f"Found {len(uncovered)} unclassified route(s) in application! "
        f"All non-public routes must be in EXERCISED_ROUTES or NOT_TENANT_DATA:\n"
        + "\n".join(f"  {m} {p}" for m, p in uncovered)
    )
