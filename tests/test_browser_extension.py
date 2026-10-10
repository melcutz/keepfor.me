# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Reusable Playwright tests for the browser extension (MV3 popup).

Runs against the live app as a normal user — no backdoors, no DB access.
Only the public UI + public API that any user (and the extension) can reach.

Config (env vars, so no secrets are committed):
  KFM_BASE_URL  default https://app.keepfor.me
  KFM_EMAIL     default m@m.com
  KFM_PASSWORD  required for live tests (skipped when unset)

Run:
  KFM_PASSWORD='...' python3 -m pytest tests/test_browser_extension.py -q
  # headed (watch it work):
  KFM_PASSWORD='...' KFM_HEADED=1 python3 -m pytest tests/test_browser_extension.py -q

What is covered:
  static: manifest validity, popup DOM ids, popup.js save contract,
          host_permissions cover the base URL, icons exist.
  live (needs KFM_PASSWORD):
    - login via UI form (session cookie set)
    - PAT creation via settings UI (token format, one-time display)
    - /api/save contract: new vs dupe, tags, 400 on bad URL, 401 without/bad token
    - popup end-to-end: config gate, successful save, trailing-slash strip,
      tags parsing, invalid PAT + invalid URL error surfacing
Cleanup: every created item is DELETEd, every created PAT is revoked via the
settings UI, so the account is left as it was found.
"""

import json
import os
import re
import uuid
from pathlib import Path

import pytest

BASE_URL = os.environ.get("KFM_BASE_URL", "https://app.keepfor.me").rstrip("/")
EMAIL = os.environ.get("KFM_EMAIL", "m@m.com")
PASSWORD = os.environ.get("KFM_PASSWORD")  # never commit this
HEADED = os.environ.get("KFM_HEADED", "0") == "1"

REPO = Path(__file__).resolve().parent.parent
EXT_DIR = REPO / "browser-extension"
MANIFEST = EXT_DIR / "manifest.json"
POPUP_HTML = EXT_DIR / "popup.html"
POPUP_JS = EXT_DIR / "popup.js"

needs_password = pytest.mark.skipif(
    not PASSWORD, reason="KFM_PASSWORD not set — live prod tests skipped"
)


# ---------------------------------------------------------------- static ---


def test_manifest_is_valid_mv3():
    data = json.loads(MANIFEST.read_text())
    assert data["manifest_version"] == 3
    assert data["name"] == "Keepfor.me"
    assert data["action"]["default_popup"] == "popup.html"
    assert "storage" in data["permissions"]
    assert "activeTab" in data["permissions"]
    assert isinstance(data["host_permissions"], list) and data["host_permissions"]
    for icon in data["icons"].values():
        assert (EXT_DIR / icon).exists(), f"missing icon {icon}"


def test_popup_html_has_required_ids():
    html = POPUP_HTML.read_text()
    for el_id in [
        "main-view",
        "config-view",
        "url-input",
        "title-input",
        "tags-input",
        "save-btn",
        "status",
        "worker-url",
        "pat-token",
        "save-config-btn",
        "config-status",
        "toggle-config",
        "close-config",
    ]:
        assert f'id="{el_id}"' in html, f"popup.html missing #{el_id}"


def test_popup_js_save_contract():
    js = POPUP_JS.read_text()
    # save endpoint + bearer auth + JSON body with url/tags
    assert "/api/save" in js
    assert "Bearer" in js
    assert "Content-Type" in js and "application/json" in js
    # trailing-slash normalization on the worker URL
    assert re.search(r"replace\(/\\\/\+\$/, \"\"\)", js), (
        "workerUrl must strip trailing /"
    )
    # tags: comma-split, trim, drop empties
    assert 'split(",")' in js
    # error surfacing: non-OK -> throw with status, try/catch renders into .error
    assert "response.ok" in js
    assert "status error" in js
    # config gate: missing workerUrl/patToken -> prompt to configure
    assert "Configure" in js or "configure" in js
    # success messaging distinguishes new vs dupe via is_new
    assert "is_new" in js


def test_popup_html_settings_placeholder():
    html = POPUP_HTML.read_text()
    assert 'placeholder="https://app.keepfor.me"' in html
    assert 'placeholder="https://keepfor.me"' not in html


def test_popup_js_includes_title_in_payload():
    js = POPUP_JS.read_text()
    # Must read titleInput and include in payload
    assert "titleInput" in js
    assert "title" in js
    assert re.search(r"title:\s*(title|titleInput\.value)", js)


def test_host_permissions_cover_base_url():
    data = json.loads(MANIFEST.read_text())
    host = BASE_URL.split("://", 1)[1].split("/", 1)[0]
    patterns = data["host_permissions"]
    covered = any(
        p.replace("https://", "").replace("http://", "").split("/")[0].replace("*.", "")
        in host
        or host in p
        for p in patterns
    )
    assert covered, f"{BASE_URL} not covered by {patterns}"


# --------------------------------------------------------------- helpers ---

TEST_TAG = "pw-ext-test"


def _unique_name(prefix="pw-ext-pat"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _unique_url():
    return f"https://example.com/pw-ext-{uuid.uuid4().hex[:12]}"


def _browser(pw):
    return pw.chromium.launch(headless=not HEADED)


def login_via_ui(page, email=EMAIL, password=PASSWORD):
    """Log in exactly like a normal user. Returns session cookies."""
    page.goto(f"{BASE_URL}/auth/login", wait_until="domcontentloaded")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(re.escape(BASE_URL) + r"/.*"), timeout=15000)
    assert "/auth/login" not in page.url, "login did not redirect away from /auth/login"
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    assert "kfm_session" in cookies or "rk_session" in cookies, "no session cookie set"
    return cookies


def create_pat_via_ui(page, name):
    """Generate a PAT via the settings form. Returns (raw_token, pat_id)."""
    page.goto(f"{BASE_URL}/settings", wait_until="domcontentloaded")
    page.fill('form[action="/settings/tokens"] input[name="name"]', name)
    page.click('form[action="/settings/tokens"] button[type="submit"]')
    page.wait_for_selector("#new-token-val", timeout=15000)
    token = page.input_value("#new-token-val")
    assert re.match(r"^(kfm_live_|rk_live_)", token), (
        f"unexpected token format: {token[:12]}"
    )
    # the new row for this name carries the revoke form with the pat id
    pat_id = page.evaluate(
        "(name) => {"
        "const rows = [...document.querySelectorAll('tbody tr')];"
        "for (const r of rows) {"
        "if (!r.textContent.includes(name)) continue;"
        "const f = r.querySelector('form[action*=\"/settings/tokens/\"]');"
        "if (!f) continue;"
        "const m = f.getAttribute('action').match(/tokens\\/(.*?)\\/delete/);"
        "if (m) return m[1];}"
        "return null; }",
        name,
    )
    assert pat_id, "could not find pat id in settings table"
    return token, pat_id


def revoke_pat_via_ui(page, pat_id):
    page.goto(f"{BASE_URL}/settings", wait_until="domcontentloaded")
    page.evaluate(
        "(pid) => {"
        'const sel = `form[action="/settings/tokens/${pid}/delete"]`;'
        "const f = document.querySelector(sel);"
        "if (f) f.submit(); }",
        pat_id,
    )
    page.wait_for_timeout(2000)


def api_save(request, token, url, tags=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return request.post(
        f"{BASE_URL}/api/save",
        headers=headers,
        data=json.dumps({"url": url, **({"tags": tags} if tags is not None else {})}),
    )


# ------------------------------------------------- popup harness (mocked chrome) ---

POPUP_CHROME_MOCK = """(cfg) => {
  window.__kfmStorage = { workerUrl: cfg.workerUrl, patToken: cfg.patToken };
  window.__kfmClosed = false;
  window.close = () => { window.__kfmClosed = true; };
  window.chrome = {
    tabs: { query: (q, cb) => cb([{ url: cfg.tabUrl, title: cfg.tabTitle }]) },
    storage: {
      sync: {
        get: (keys, cb) => {
          const s = window.__kfmStorage;
          if (Array.isArray(keys)) {
            const o = {};
            keys.forEach(k => o[k] = s[k]); cb(o);
          }
          else if (typeof keys === 'string') { cb({ [keys]: s[keys] }); }
          else { cb({ ...s }); }
        },
        set: (obj, cb) => { Object.assign(window.__kfmStorage, obj); if (cb) cb(); },
      },
    },
  };
  window.__kfmRequests = [];
  const origFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    window.__kfmRequests.push({ url: String(input), body: init && init.body });
    return origFetch(input, init);
  };
}"""


def load_popup(page, worker_url, pat_token, tab_url, tab_title):
    """Render the real popup.html+popup.js on the app origin (same-origin fetch).

    file:// would trip the app's intentional no-CORS policy; loading on the
    app origin mirrors the extension (host_permissions bypass CORS there).
    """
    html = POPUP_HTML.read_text().replace('<script src="popup.js"></script>', "")
    js = POPUP_JS.read_text()
    page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")
    page.evaluate(
        POPUP_CHROME_MOCK,
        {
            "workerUrl": worker_url,
            "patToken": pat_token,
            "tabUrl": tab_url,
            "tabTitle": tab_title,
        },
    )
    page.evaluate(
        """([html, js]) => {
          document.open(); document.write(html); document.close();
          const s = document.createElement('script');
          s.textContent = js;
          document.body.appendChild(s);
          document.dispatchEvent(new Event('DOMContentLoaded'));
        }""",
        [html, js],
    )
    page.wait_for_selector("#save-btn", timeout=10000, state="attached")


# ------------------------------------------------------------------ live ---


@needs_password
def test_login_via_ui_sets_session():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page()
        login_via_ui(page)
        # logged-in landing page shows library chrome, not the login form
        assert "Log in" not in page.title() or "/auth/login" not in page.url
        browser.close()


@needs_password
def test_pat_lifecycle_via_settings_ui():
    """Create a PAT via UI, prove it works, revoke it via UI."""
    from playwright.sync_api import sync_playwright

    name = _unique_name()
    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        token, pat_id = create_pat_via_ui(page, name)
        # token authenticates against the API
        hdrs = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        resp = ctx.request.post(
            f"{BASE_URL}/api/save",
            headers=hdrs,
            data=json.dumps({"url": _unique_url(), "tags": [TEST_TAG]}),
        )
        assert resp.status in (200, 202), resp.text()
        item = resp.json()
        # cleanup item + token
        ctx.request.delete(
            f"{BASE_URL}/api/items/{item['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        revoke_pat_via_ui(page, pat_id)
        # NOTE: ctx still holds the session cookie, which also authenticates.
        # Prove the *token* is dead with a cookie-free context.
        anon = browser.new_context()
        bad = anon.request.post(
            f"{BASE_URL}/api/save",
            headers=hdrs,
            data=json.dumps({"url": _unique_url()}),
        )
        assert bad.status == 401, "revoked token must stop working"
        anon.close()
        browser.close()


@needs_password
def test_api_save_contract_new_dupe_invalid_unauthorized():
    from playwright.sync_api import sync_playwright

    name = _unique_name("pw-ext-contract")
    created_ids = []
    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        token, pat_id = create_pat_via_ui(page, name)
        try:
            url = _unique_url()
            r1 = api_save(ctx.request, token, url, tags=[TEST_TAG])
            assert r1.status == 202, r1.text()
            assert r1.json()["is_new"] is True
            created_ids.append(r1.json()["id"])

            r2 = api_save(ctx.request, token, url, tags=[TEST_TAG, "second"])
            assert r2.status == 200, r2.text()
            assert r2.json()["is_new"] is False  # dupe badge path

            r3 = api_save(ctx.request, token, "not-a-valid-url")
            assert r3.status in (400, 422), r3.text()

            # NOTE: ctx carries the login session cookie, which also
            # authenticates /api/save by design. Auth-negative cases need
            # a cookie-free context to isolate the PAT.
            anon = browser.new_context()
            try:
                r4 = api_save(anon.request, None, _unique_url())
                assert r4.status == 401

                r5 = api_save(anon.request, "kfm_live_bogus", _unique_url())
                assert r5.status == 401
            finally:
                anon.close()
        finally:
            for iid in created_ids:
                ctx.request.delete(
                    f"{BASE_URL}/api/items/{iid}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            revoke_pat_via_ui(page, pat_id)
            browser.close()


@needs_password
def test_popup_config_gate_when_unconfigured():
    """Fresh install (no workerUrl/PAT): popup must show settings + hint."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page()
        load_popup(page, "", "", "https://example.com/x", "Example")
        page.wait_for_timeout(500)
        assert page.evaluate(
            "document.getElementById('config-view').classList.contains('active')"
        ), "config view should open when unconfigured"
        assert "onfigur" in page.inner_text("#config-status")
        browser.close()


@needs_password
def test_popup_save_end_to_end_and_cleanup():
    """Full extension flow: prefilled tab -> Save -> success -> item in library."""
    from playwright.sync_api import sync_playwright

    name = _unique_name("pw-ext-e2e")
    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        token, pat_id = create_pat_via_ui(page, name)
        try:
            tab_url = _unique_url()
            load_popup(page, BASE_URL, token, tab_url, "Playwright E2E Page")
            assert page.input_value("#url-input") == tab_url
            assert page.input_value("#title-input") == "Playwright E2E Page"
            page.fill("#tags-input", f"{TEST_TAG}, e2e")
            page.click("#save-btn")
            page.wait_for_selector("#status.success", timeout=20000)
            assert "aved" in page.inner_text("#status") or "pdated" in page.inner_text(
                "#status"
            )
            # verify server-side via the same PAT (what Open library would show)
            items = ctx.request.get(
                f"{BASE_URL}/api/items?limit=50",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
            match = [i for i in items if i["url"] == tab_url]
            assert match, "saved URL not found in library"
            # dupe re-save through the popup shows the 'Updated tags!' path
            load_popup(page, BASE_URL, token, tab_url, "Playwright E2E Page")
            page.fill("#tags-input", TEST_TAG)
            page.click("#save-btn")
            page.wait_for_selector("#status.success", timeout=20000)
            assert "pdated" in page.inner_text("#status")
            ctx.request.delete(
                f"{BASE_URL}/api/items/{match[0]['id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            revoke_pat_via_ui(page, pat_id)
            browser.close()


@needs_password
def test_popup_trailing_slash_and_tags_parsing():
    """workerUrl 'https://app.keepfor.me///' must not produce '//api/save';
    'a, b,, c' must send ['a','b','c']."""
    from playwright.sync_api import sync_playwright

    name = _unique_name("pw-ext-parse")
    with sync_playwright() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        token, pat_id = create_pat_via_ui(page, name)
        try:
            tab_url = _unique_url()
            load_popup(page, BASE_URL + "///", token, tab_url, "Parse check")
            page.fill("#tags-input", " alpha ,, beta ,  gamma ")
            page.click("#save-btn")
            page.wait_for_selector("#status.success", timeout=20000)
            reqs = page.evaluate("window.__kfmRequests")
            assert reqs, "popup made no fetch call"
            assert "//api/save" not in reqs[-1]["url"], reqs[-1]["url"]
            assert reqs[-1]["url"].endswith("/api/save"), reqs[-1]["url"]
            body = json.loads(reqs[-1]["body"])
            assert body["tags"] == ["alpha", "beta", "gamma"], body
            # cleanup the saved item
            items = ctx.request.get(
                f"{BASE_URL}/api/items?limit=50",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
            for i in items:
                if i["url"] == tab_url:
                    ctx.request.delete(
                        f"{BASE_URL}/api/items/{i['id']}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
        finally:
            revoke_pat_via_ui(page, pat_id)
            browser.close()


@needs_password
def test_popup_surfaces_auth_and_validation_errors():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page()
        # bad PAT -> 401 surfaces in #status.error
        load_popup(page, BASE_URL, "kfm_live_bogus", _unique_url(), "Bad PAT")
        page.click("#save-btn")
        page.wait_for_selector("#status.error", timeout=20000)
        assert "401" in page.inner_text("#status")

        # garbage URL -> 400 surfaces in #status.error (needs a real PAT first,
        # so reuse a throwaway login just for the failure path)
        browser.close()

    from playwright.sync_api import sync_playwright as sp2

    name = _unique_name("pw-ext-err")
    with sp2() as pw:
        browser = _browser(pw)
        ctx = browser.new_context()
        page = ctx.new_page()
        login_via_ui(page)
        token, pat_id = create_pat_via_ui(page, name)
        try:
            load_popup(page, BASE_URL, token, "not-a-valid-url", "Bad URL")
            # url-input is readonly but settable for the test
            page.evaluate(
                "document.getElementById('url-input').value = 'not-a-valid-url'"
            )
            page.click("#save-btn")
            page.wait_for_selector("#status.error", timeout=20000)
            assert "400" in page.inner_text("#status") or "422" in page.inner_text(
                "#status"
            )
        finally:
            revoke_pat_via_ui(page, pat_id)
            browser.close()
