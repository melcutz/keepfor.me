# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Reusable Playwright tests for the drag-and-drop bookmarklet.

Runs against the live app as a normal user — no backdoors, no DB access.
Only the public settings page, the public /save-popup flow, and the
bookmarklet JS any user can copy.

Config (env vars, so no secrets are committed):
  KFM_BASE_URL  default https://app.keepfor.me
  KFM_EMAIL     default m@m.com
  KFM_PASSWORD  required for live tests (skipped when unset)

Run:
  KFM_PASSWORD='...' python3 -m pytest tests/test_bookmarklet.py -q

The bookmarklet under test is the #bookmarklet anchor in settings.html:
  javascript:(function(){ ... window.open('<base>/save-popup?url='+u...
Cleanup: every saved item is DELETEd, no PATs are created (session-cookie
auth only), so the account is left as it was found.
"""

import os
import re
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

# Every test here drives a real browser. CI installs only ruff/pytest/httpx,
# so skip the whole module there instead of erroring on the import.
pytest.importorskip("playwright.sync_api")

BASE_URL = os.environ.get("KFM_BASE_URL", "https://app.keepfor.me").rstrip("/")
EMAIL = os.environ.get("KFM_EMAIL", "m@m.com")
PASSWORD = os.environ.get("KFM_PASSWORD")  # never commit this
HEADED = os.environ.get("KFM_HEADED", "0") == "1"

needs_password = pytest.mark.skipif(
    not PASSWORD, reason="KFM_PASSWORD not set — live prod tests skipped"
)

TEST_TAG = "pw-bkm-test"


def _unique_path():
    return f"/bkm-{uuid.uuid4().hex[:12]}"


def _browser(pw):
    return pw.chromium.launch(headless=not HEADED)


def login_via_ui(page, email=EMAIL, password=PASSWORD):
    page.goto(f"{BASE_URL}/auth/login", wait_until="domcontentloaded")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(re.escape(BASE_URL) + r"/.*"), timeout=15000)
    assert "/auth/login" not in page.url


def get_bookmarklet_href(page):
    """Copy the bookmarklet code exactly as a user would (Copy code button)."""
    page.goto(f"{BASE_URL}/settings", wait_until="domcontentloaded")
    href = page.get_attribute("#bookmarklet", "href")
    assert href, "settings page has no #bookmarklet anchor"
    return href


def run_bookmarklet(page, js_body):
    """Execute the bookmarklet on the current page, return the popup page."""
    with page.expect_popup() as pop:
        page.evaluate(js_body)
    popup = pop.value
    popup.wait_for_load_state("domcontentloaded")
    return popup


@needs_password
def test_bookmarklet_anchor_wellformed():
    """The anchor a user drags must be a self-contained popup opener."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page()
        login_via_ui(page)
        href = get_bookmarklet_href(page)
        assert href.startswith("javascript:")
        body = href[len("javascript:") :]
        assert "\n" not in body, "newline would break the bookmarklet"
        assert "encodeURIComponent(window.location.href)" in body
        assert "encodeURIComponent(document.title)" in body
        assert "/save-popup?url=" in body
        assert "window.open" in body
        assert "width='+w+',height='+h" in body
        assert "popup=yes" in body
        # points at this instance, not a hardcoded other host
        assert BASE_URL + "/save-popup" in body
        browser.close()


@needs_password
def test_bookmarklet_saves_page_end_to_end():
    """Drag-bookmarklet flow: external page -> popup prefilled -> save."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        body = get_bookmarklet_href(page)[len("javascript:") :]

        page.goto("https://example.com/", wait_until="domcontentloaded")
        page.evaluate(f"history.replaceState(null, '', '{_unique_path()}')")
        page.evaluate("document.title = 'Example Domain'")
        popup = run_bookmarklet(page, body)

        assert popup.url.startswith(f"{BASE_URL}/save-popup?url=")
        assert popup.input_value('input[name="url"]') == page.url
        assert popup.input_value('input[name="title"]') == "Example Domain"
        # brand + favicon render in the popup itself
        assert popup.evaluate("!!document.querySelector('svg')")
        assert popup.evaluate("!!document.querySelector('link[rel=\"icon\"]')")

        popup.fill('input[name="tags"]', TEST_TAG)
        popup.click("#share-save-submit")
        popup.wait_for_load_state("domcontentloaded")
        assert "Saved & Queued!" in popup.inner_text("body")

        items = ctx.request.get(f"{BASE_URL}/api/items?limit=50").json()
        match = [i for i in items if i["url"] == page.url]
        assert match, "bookmarklet-saved URL not found in library"
        assert TEST_TAG in match[0].get("tags", [])
        ctx.request.delete(f"{BASE_URL}/api/items/{match[0]['id']}")
        browser.close()


@needs_password
def test_bookmarklet_preserves_special_chars_in_title():
    """Titles with & \" < > must survive encode -> prefill round-trip."""
    from playwright.sync_api import sync_playwright

    tricky = "A & B \"quoted\" <tag> 'apos'"
    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        body = get_bookmarklet_href(page)[len("javascript:") :]

        page.goto("https://example.com/", wait_until="domcontentloaded")
        page.evaluate("(t) => { document.title = t; }", tricky)
        popup = run_bookmarklet(page, body)
        qs = parse_qs(urlparse(popup.url).query)
        assert qs["title"][0] == tricky  # transport intact
        assert popup.input_value('input[name="title"]') == tricky  # render intact
        browser.close()


@needs_password
def test_bookmarklet_unauthenticated_redirects_and_returns():
    """Logged-out click -> login with ?next= -> back to prefilled popup."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()  # no cookies: a logged-out user
        page = ctx.new_page()
        page.goto(
            f"{BASE_URL}/save-popup?url=https://example.com/abc&title=Hi",
            wait_until="domcontentloaded",
        )
        assert "/auth/login" in page.url
        assert "next=" in page.url
        # NOTE: submit the form in place — navigating to plain /auth/login
        # would drop the ?next= return address and land on "/" instead.
        page.fill('input[name="email"]', EMAIL)
        page.fill('input[name="password"]', PASSWORD or "")
        page.click('button[type="submit"]')
        page.wait_for_url(re.compile(re.escape(BASE_URL) + r"/.*"), timeout=15000)
        assert page.url.startswith(f"{BASE_URL}/save-popup?")
        assert popup_prefill(page) == "https://example.com/abc"
        browser.close()


def popup_prefill(page):
    return page.input_value('input[name="url"]')


@needs_password
def test_bookmarklet_cancel_closes_popup():
    """Cancel on a bookmarklet popup (source='') calls window.close()."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        body = get_bookmarklet_href(page)[len("javascript:") :]

        page.goto("https://example.com/", wait_until="domcontentloaded")
        popup = run_bookmarklet(page, body)
        with popup.expect_event("close", timeout=10000):
            popup.click("text=Cancel")
        browser.close()


@needs_password
def test_bookmarklet_rejects_invalid_url():
    """Odd input (e.g. pasted text instead of URL) surfaces an error."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        page.goto(
            f"{BASE_URL}/save-popup?url=&title=junk",
            wait_until="domcontentloaded",
        )
        page.evaluate("document.querySelector('input[name=\"url\"]').value = 'x'")
        page.click("#share-save-submit")
        page.wait_for_load_state("domcontentloaded")
        assert "Invalid URL" in page.content()
        browser.close()


def test_favicon_served_for_bookmark_workaround_prereq():
    """The site icon itself must exist and be linked, otherwise even the
    manual bookmark-then-edit trick has no icon to keep."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page()
        resp = page.request.get(f"{BASE_URL}/favicon.ico")
        assert resp.status == 200
        assert "svg" in resp.headers.get("content-type", "")
        assert "max-age" in resp.headers.get("cache-control", "")
        assert len(resp.body()) > 100

        page.goto(f"{BASE_URL}/auth/login", wait_until="domcontentloaded")
        assert page.evaluate("!!document.querySelector('link[rel=\"icon\"]')")
        browser.close()


@needs_password
def test_save_popup_carries_favicon_and_brand():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        page.goto(
            f"{BASE_URL}/save-popup?url=https://example.com/x&title=T",
            wait_until="domcontentloaded",
        )
        assert page.evaluate("!!document.querySelector('link[rel=\"icon\"]')")
        assert "Keepfor" in page.inner_text("body")
        browser.close()
