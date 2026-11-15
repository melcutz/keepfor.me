# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Reusable Playwright tests for the browser extension (MV3 popup).

Runs against the live app as a normal user - no backdoors, no DB access.
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
import subprocess
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
    not PASSWORD, reason="KFM_PASSWORD not set - live prod tests skipped"
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


# ------------------------------------------- offline Node + static checks ---


def _run_node(script: str, *, module: bool = False) -> subprocess.CompletedProcess:
    cmd = ["node"]
    if module:
        cmd.append("--input-type=module")
    cmd.extend(["-e", script])
    return subprocess.run(
        cmd,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_extension_js_syntax_slice1():
    for rel in [
        "browser-extension/lib/api.js",
        "browser-extension/popup.js",
        "browser-extension/background.js",
    ]:
        res = subprocess.run(
            ["node", "--check", str(REPO / rel)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res.returncode == 0, f"{rel} syntax error: {res.stderr}"


def test_manifest_custom_host_and_optional_permissions():
    data = json.loads(MANIFEST.read_text())
    assert "https://*.keepfor.me/*" in data["host_permissions"]
    assert data.get("optional_host_permissions") == ["https://*/*", "http://*/*"]


def test_popup_html_pat_is_masked():
    html = POPUP_HTML.read_text()
    assert re.search(
        r'<input[^>]*id="pat-token"[^>]*type="password"|<input[^>]*type="password"[^>]*id="pat-token"',
        html,
    ), 'popup.html #pat-token must use type="password"'


def test_background_js_restricts_context_menus_and_parses_errors():
    bg_js = (EXT_DIR / "background.js").read_text()
    assert "documentUrlPatterns" in bg_js
    assert "targetUrlPatterns" in bg_js
    assert "http://*/*" in bg_js and "https://*/*" in bg_js
    assert "parseApiError" in bg_js
    assert 'ok ? "✓" : "!"' in bg_js


def test_api_js_parse_api_error_and_skippable_url_unit():
    api_url = (EXT_DIR / "lib/api.js").as_uri()
    script = f"""
import assert from "node:assert/strict";
import {{ parseApiError, isSkippableUrl }} from {json.dumps(api_url)};

assert.equal(typeof parseApiError, "function");
assert.equal(
  parseApiError(403, JSON.stringify({{ detail: "This token is read-only." }})),
  "Error 403: This token is read-only."
);
assert.equal(
  parseApiError(422, JSON.stringify({{ detail: [{{ msg: "Invalid URL" }}] }})),
  "Error 422: Invalid URL"
);
assert.equal(
  parseApiError(500, "Internal Server Error"),
  "Error 500: Internal Server Error"
);
assert.equal(parseApiError(502, ""), "Error 502");

assert.equal(isSkippableUrl("chrome://settings"), true);
assert.equal(isSkippableUrl("edge://settings"), true);
assert.equal(isSkippableUrl("about:blank"), true);
assert.equal(isSkippableUrl("moz-extension://uuid/popup.html"), true);
assert.equal(isSkippableUrl("file:///tmp/a.html"), true);
assert.equal(isSkippableUrl("https://example.com"), false);
assert.equal(isSkippableUrl("http://localhost:8787"), false);
assert.equal(isSkippableUrl("not-a-valid-url"), false);
"""
    res = _run_node(script, module=True)
    assert res.returncode == 0, f"Node unit test failed:\n{res.stderr}\n{res.stdout}"


def test_popup_js_permissions_readonly_guard_and_finally_mocked():
    popup_js_path = json.dumps(str(EXT_DIR / "popup.js"))
    script = f"""
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const popupCode = fs.readFileSync({popup_js_path}, "utf8");

function makeHarness({{
  tabUrl = "https://example.com/article",
  tabTitle = "Article",
  storage = {{}},
  fetchImpl,
}}) {{
  const elements = new Map();
  const docListeners = {{}};
  function makeEl(id) {{
    const listeners = {{}};
    const classes = new Set();
    const el = {{
      id,
      value: "",
      textContent: "",
      disabled: false,
      className: "",
      type: id === "pat-token" ? "password" : "text",
      classList: {{
        add: (...c) => c.forEach((x) => classes.add(x)),
        remove: (...c) => c.forEach((x) => classes.delete(x)),
        contains: (c) => classes.has(c),
      }},
      addEventListener: (ev, fn) => {{
        (listeners[ev] ||= []).push(fn);
      }},
      dispatch: async (ev) => {{
        for (const fn of listeners[ev] || []) {{
          await fn({{ preventDefault: () => {{}} }});
        }}
      }},
    }};
    elements.set(id, el);
    return el;
  }}

  for (const id of [
    "main-view", "config-view", "toggle-config", "close-config",
    "url-input", "title-input", "tags-input", "save-btn", "status",
    "worker-url", "pat-token", "save-config-btn", "config-status",
    "open-library", "open-panel", "dupe",
  ]) {{
    makeEl(id);
  }}

  const permCalls = [];
  const fetchCalls = [];
  const store = {{ ...storage }};
  let closed = false;

  const sandbox = {{
    console,
    URL,
    Date,
    setTimeout: () => 0,
    clearTimeout: () => {{}},
    window: {{ close: () => {{ closed = true; }} }},
    document: {{
      getElementById: (id) => elements.get(id) || null,
      addEventListener: (ev, fn) => {{ (docListeners[ev] ||= []).push(fn); }},
    }},
    fetch: async (url, init) => {{
      fetchCalls.push({{
        url: String(url),
        method: init?.method || "GET",
        body: init?.body,
      }});
      return fetchImpl(String(url), init);
    }},
    chrome: {{
      tabs: {{
        query: (_q, cb) => cb([{{ url: tabUrl, title: tabTitle }}]),
        create: () => {{}},
      }},
      permissions: {{
        request: async (req) => {{
          permCalls.push(req);
          return true;
        }},
      }},
      storage: {{
        sync: {{
          get: (keys, cb) => {{
            const out = {{}};
            for (const k of keys) out[k] = store[k];
            cb(out);
          }},
          set: (obj, cb) => {{ Object.assign(store, obj); if (cb) cb(); }},
        }},
        local: {{
          get: (_keys, cb) => cb({{}}),
        }},
      }},
      windows: {{ WINDOW_ID_CURRENT: -2 }},
      sidePanel: {{ open: async () => {{}} }},
      runtime: {{ sendMessage: () => {{}} }},
    }},
  }};

  vm.runInNewContext(popupCode, sandbox);
  return {{
    elements,
    permCalls,
    fetchCalls,
    store,
    wasClosed: () => closed,
    ready: async () => {{
      for (const fn of docListeners["DOMContentLoaded"] || []) await fn();
    }},
  }};
}}

// 1. Save Settings requests host permission and warns on 403 read-only PAT
{{
  const h = makeHarness({{
    fetchImpl: async (url) => {{
      if (url.endsWith("/api/items?limit=1")) {{
        return {{ ok: true, status: 200, text: async () => "[]" }};
      }}
      if (url.endsWith("/api/save")) {{
        return {{
          ok: false,
          status: 403,
          text: async () => JSON.stringify({{ detail: "This token is read-only." }}),
        }};
      }}
      throw new Error("Unexpected fetch: " + url);
    }},
  }});
  await h.ready();
  h.elements.get("worker-url").value = "https://custom.example.com/";
  h.elements.get("pat-token").value = "kfm_live_ro123";
  await h.elements.get("save-config-btn").dispatch("click");

  assert.equal(
    JSON.stringify(h.permCalls),
    JSON.stringify([{{ origins: ["https://custom.example.com/*"] }}])
  );
  assert.equal(h.fetchCalls[0].url, "https://custom.example.com/api/items?limit=1");
  assert.equal(h.fetchCalls[1].url, "https://custom.example.com/api/save");
  assert.equal(h.fetchCalls[1].method, "POST");
  assert.equal(h.store.workerUrl, "https://custom.example.com");
  assert.equal(h.store.patToken, "kfm_live_ro123");
  assert.match(h.elements.get("config-status").textContent, /read-only/i);
  assert.equal(h.elements.get("config-view").classList.contains("active"), true);
}}

// 2. Non-HTTP internal tab disables #save-btn with "Cannot save this browser page"
{{
  const h = makeHarness({{
    tabUrl: "chrome://settings",
    tabTitle: "Settings",
    storage: {{ workerUrl: "https://app.keepfor.me", patToken: "kfm_live_abc" }},
    fetchImpl: async () => {{ throw new Error("should not fetch"); }},
  }});
  await h.ready();
  assert.equal(h.elements.get("save-btn").disabled, true);
  assert.equal(h.elements.get("status").textContent, "Cannot save this browser page");
  assert.equal(h.elements.get("status").className, "status error");
}}

// 3. Save error parses JSON detail and re-enables #save-btn in finally
{{
  const h = makeHarness({{
    tabUrl: "https://example.com/post",
    tabTitle: "Post",
    storage: {{ workerUrl: "https://app.keepfor.me", patToken: "kfm_live_ro" }},
    fetchImpl: async () => ({{
      ok: false,
      status: 403,
      text: async () => JSON.stringify({{ detail: "This token is read-only." }}),
    }}),
  }});
  await h.ready();
  await h.elements.get("save-btn").dispatch("click");
  await new Promise((r) => setImmediate(r));
  assert.equal(
    h.elements.get("status").textContent,
    "Error 403: This token is read-only."
  );
  assert.equal(h.elements.get("status").className, "status error");
  assert.equal(h.elements.get("save-btn").disabled, false);
}}

// 4. Successful save also re-enables #save-btn in finally
{{
  const h = makeHarness({{
    tabUrl: "https://example.com/post",
    tabTitle: "Post",
    storage: {{ workerUrl: "https://app.keepfor.me", patToken: "kfm_live_rw" }},
    fetchImpl: async () => ({{
      ok: true,
      status: 200,
      json: async () => ({{ is_new: true }}),
    }}),
  }});
  await h.ready();
  await h.elements.get("save-btn").dispatch("click");
  await new Promise((r) => setImmediate(r));
  assert.equal(h.elements.get("status").textContent, "Saved & Queued!");
  assert.equal(h.elements.get("save-btn").disabled, false);
}}
"""
    res = _run_node(script, module=True)
    assert res.returncode == 0, f"Popup Node test failed:\n{res.stderr}\n{res.stdout}"


def test_background_js_context_menu_and_error_badge_mocked():
    bg_js_path = json.dumps(str(EXT_DIR / "background.js"))
    script = f"""
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const bgCode = fs.readFileSync({bg_js_path}, "utf8");
const createdMenus = [];
const badgeTexts = [];
const badgeColors = [];
const localWrites = [];
let onClickedFn = null;
let onInstalledFn = null;

const sandbox = {{
  console,
  Date,
  setTimeout: () => 1,
  clearTimeout: () => {{}},
  fetch: async () => ({{
    ok: false,
    status: 403,
    text: async () => JSON.stringify({{ detail: "This token is read-only." }}),
  }}),
  chrome: {{
    runtime: {{
      onInstalled: {{ addListener: (fn) => {{ onInstalledFn = fn; }} }},
      onMessage: {{ addListener: () => {{}} }},
    }},
    contextMenus: {{
      create: (opts) => createdMenus.push(opts),
      onClicked: {{ addListener: (fn) => {{ onClickedFn = fn; }} }},
    }},
    action: {{
      setBadgeText: (v) => badgeTexts.push(v.text),
      setBadgeBackgroundColor: (v) => badgeColors.push(v.color),
    }},
    storage: {{
      sync: {{
        get: async () => ({{
          workerUrl: "https://app.keepfor.me",
          patToken: "kfm_live_ro",
        }}),
      }},
      local: {{
        set: async (obj) => localWrites.push(obj),
      }},
    }},
  }},
}};

vm.runInNewContext(bgCode, sandbox);
onInstalledFn();

const pageMenu = createdMenus.find((m) => m.id === "kfm-save-page");
const linkMenu = createdMenus.find((m) => m.id === "kfm-save-link");
assert.equal(
  JSON.stringify(pageMenu.documentUrlPatterns),
  JSON.stringify(["http://*/*", "https://*/*"])
);
assert.equal(
  JSON.stringify(linkMenu.targetUrlPatterns),
  JSON.stringify(["http://*/*", "https://*/*"])
);

// Non-http(s) URL is ignored without calling fetch or setting badge
await onClickedFn({{ menuItemId: "kfm-save-page" }}, {{ url: "chrome://settings" }});
assert.equal(badgeTexts.length, 0);

// HTTP URL failure sets "!" badge and parsed detail error
await onClickedFn({{ menuItemId: "kfm-save-page" }}, {{ url: "https://example.com" }});
assert.equal(badgeTexts.at(-1), "!");
assert.equal(badgeColors.at(-1), "#dc2626");
assert.equal(localWrites.at(-1).kfmLast.ok, false);
assert.equal(localWrites.at(-1).kfmLast.error, "Error 403: This token is read-only.");
"""
    res = _run_node(script, module=True)
    assert res.returncode == 0, (
        f"Background Node test failed:\n{res.stderr}\n{res.stdout}"
    )


def test_extension_js_syntax_slice2():
    res = subprocess.run(
        ["node", "--check", str(EXT_DIR / "sidepanel.js")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, f"sidepanel.js syntax error: {res.stderr}"


def test_sidepanel_html_no_dead_ask_tab():
    html = (EXT_DIR / "sidepanel.html").read_text()
    assert 'data-tab="chat"' not in html
    assert 'id="tab-chat"' not in html
    assert "Ask is not yet implemented" not in html
    for tab in ("tabs", "history", "search"):
        assert f'data-tab="{tab}"' in html
        assert f'id="tab-{tab}"' in html


def test_sidepanel_js_notes_and_tab_refresh_mocked():
    sidepanel_url = (EXT_DIR / "sidepanel.js").as_uri()
    script = f"""
import assert from "node:assert/strict";

const elements = new Map();
function makeEl(id) {{
  const listeners = {{}};
  const classes = new Set();
  const el = {{
    id,
    value: "",
    textContent: "",
    innerHTML: "",
    disabled: false,
    style: {{}},
    dataset: {{}},
    classList: {{
      add: (...c) => c.forEach((x) => classes.add(x)),
      remove: (...c) => c.forEach((x) => classes.delete(x)),
      contains: (c) => classes.has(c),
    }},
    addEventListener: (ev, fn) => {{
      (listeners[ev] ||= []).push(fn);
    }},
    dispatch: async (ev, extra = {{}}) => {{
      for (const fn of listeners[ev] || []) {{
        await fn({{ preventDefault: () => {{}}, ...extra }});
      }}
    }},
    querySelectorAll: () => [],
    querySelector: () => null,
    appendChild: () => {{}},
  }};
  elements.set(id, el);
  return el;
}}

for (const id of [
  "tabs-list", "tabs-count", "tabs-filter", "tabs-all", "tabs-none",
  "tabs-save", "tabs-prog", "tabs-err", "tabs-tags",
  "scope-win", "scope-all",
  "hist-list", "hist-save", "hist-prog", "hist-err", "hist-tags",
  "hist-range", "hist-max", "hist-q", "hist-go",
  "s-q", "s-go", "s-list",
  "tab-tabs", "tab-history", "tab-search",
]) {{
  makeEl(id);
}}

const tabListeners = {{ onCreated: [], onRemoved: [], onUpdated: [] }};
let tabsList = [
  {{ url: "https://example.com/one", title: "One Tab", favIconUrl: "" }},
  {{ url: "https://example.com/two", title: "Two Tab", favIconUrl: "" }},
];
let queryCount = 0;

globalThis.window = {{}};
globalThis.document = {{
  getElementById: (id) => elements.get(id) || null,
  querySelectorAll: () => [],
  createElement: () => ({{ style: {{}} }}),
}};

globalThis.chrome = {{
  tabs: {{
    query: async () => {{
      queryCount += 1;
      return tabsList;
    }},
    onCreated: {{ addListener: (fn) => tabListeners.onCreated.push(fn) }},
    onRemoved: {{ addListener: (fn) => tabListeners.onRemoved.push(fn) }},
    onUpdated: {{ addListener: (fn) => tabListeners.onUpdated.push(fn) }},
  }},
  storage: {{
    sync: {{
      get: async () => ({{
        workerUrl: "https://app.keepfor.me/",
        patToken: "kfm_live_test",
      }}),
    }},
  }},
  history: {{ search: async () => [] }},
}};

const searchResults = [
  {{ id: "note 1", item_type: "note", url: "urn:note:note 1", title: "My Note" }},
  {{ id: "note-2", url: "urn:note:note-2", title: "" }},
  {{
    id: "link-1",
    item_type: "link",
    url: "https://example.com/post",
    title: "Web Post",
  }},
];

globalThis.fetch = async () => ({{
  ok: true,
  status: 200,
  headers: {{ get: () => "application/json" }},
  json: async () => searchResults,
}});

await import({json.dumps(sidepanel_url)});
await new Promise((r) => setTimeout(r, 20));

// 1. Note search results link to /items/<encoded-id> with "Personal note" subtitle
elements.get("s-q").value = "note";
await elements.get("s-go").dispatch("click");
const sHtml = elements.get("s-list").innerHTML;
assert.match(sHtml, /href="https:\\/\\/app\\.keepfor\\.me\\/items\\/note%201"/);
assert.match(sHtml, />My Note<\\/a>/);
assert.match(sHtml, /href="https:\\/\\/app\\.keepfor\\.me\\/items\\/note-2"/);
assert.match(sHtml, />Untitled note<\\/a>/);
assert.match(sHtml, />Personal note<\\/span>/);
assert.match(sHtml, /href="https:\\/\\/example\\.com\\/post"/);

// 2. chrome.tabs listeners registered, debounced, and preserve #tabs-filter
assert.equal(tabListeners.onCreated.length, 1);
assert.equal(tabListeners.onRemoved.length, 1);
assert.equal(tabListeners.onUpdated.length, 1);

const beforeQueries = queryCount;
elements.get("tabs-filter").value = "Two";
tabsList = [
  {{ url: "https://example.com/one", title: "One Tab", favIconUrl: "" }},
  {{ url: "https://example.com/two", title: "Two Tab", favIconUrl: "" }},
  {{ url: "https://example.com/two-b", title: "Two B Tab", favIconUrl: "" }},
];

// Burst of events should debounce into a single refresh
tabListeners.onCreated[0]();
tabListeners.onUpdated[0]();
tabListeners.onRemoved[0]();
await new Promise((r) => setTimeout(r, 220));

assert.equal(queryCount, beforeQueries + 1);
assert.equal(elements.get("tabs-filter").value, "Two");
assert.equal(elements.get("tabs-count").textContent, "2 tabs");

// 3. Missing chrome.tabs event emitters do not throw
globalThis.chrome.tabs = {{
  query: async () => [],
}};
await import({json.dumps(sidepanel_url)} + "?no_events=1");
"""
    res = _run_node(script, module=True)
    assert res.returncode == 0, (
        f"Sidepanel Node test failed:\n{res.stderr}\n{res.stdout}"
    )
