# Browser Extension V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver extension V1: popup fast-save + library link + dupe badge, side-panel Tabs/History/Search/Chat-lite, context menus, client-side batching.

**Architecture:** Keep `popup.html/js` as fast path; add `sidepanel.html/js` hub + `background.js` SW + shared `lib/api.js` (saveOne/saveMany/search/mcpCall, concurrency 5). No backend change.

**Tech Stack:** Chrome MV3, vanilla JS/CSS, existing `POST /api/save`, `POST /api/search`, `POST /api/mcp` (tools/call search_library).

---

### Task 1: Shared API lib

**Files:**
- Create: `browser-extension/lib/api.js`
- Test: manual via DevTools console (no JS harness in repo)

- [ ] **Step 1: Create `browser-extension/lib/api.js`**

```js
// Shared fetch helpers. workerUrl normalized (no trailing slash). All calls use Bearer PAT.
export function normBase(u) { return (u || "").trim().replace(/\/+$/, ""); }

async function req(base, path, { method = "GET", body, pat } = {}) {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(pat ? { Authorization: `Bearer ${pat}` } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!res.ok) throw new Error(`Error ${res.status}: ${await res.text()}`);
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

export const saveOne = (base, pat, url, tags = []) =>
  req(base, "/api/save", { method: "POST", body: { url, tags }, pat });

// Concurrency-limited batch. onProgress(done, total, item, ok, err).
export async function saveMany(base, pat, items, { concurrency = 5, onProgress } = {}) {
  const results = [];
  let done = 0;
  let i = 0;
  async function worker() {
    while (i < items.length) {
      const item = items[i++];
      try {
        const data = await saveOne(base, pat, item.url, item.tags || []);
        results.push({ url: item.url, ok: true, data });
        onProgress && onProgress(++done, items.length, item, true);
      } catch (err) {
        results.push({ url: item.url, ok: false, error: String(err.message || err) });
        onProgress && onProgress(++done, items.length, item, false, err);
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
  return results;
}

export const searchLib = (base, pat, query, limit = 20) =>
  req(base, "/api/search", { method: "POST", body: { query, mode: "hybrid", limit }, pat });

export function mcpCall(base, pat, method, params = {}, id = 1) {
  return req(base, "/api/mcp", {
    method: "POST",
    pat,
    body: { jsonrpc: "2.0", id, method, params },
  });
}

export const mcpSearch = (base, pat, query, limit = 10) =>
  mcpCall(base, pat, "tools/call", {
    name: "search_library",
    arguments: { query, mode: "hybrid", limit },
  });

export function isSkippableUrl(url) {
  return /^(chrome|edge|about|chrome-extension|brave|opera):\/\//i.test(url || "");
}

export function dedupeByUrl(rows) {
  const seen = new Set();
  return rows.filter((r) => {
    const k = (r.url || "").trim().toLowerCase().replace(/\/+$/, "");
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
```

- [ ] **Step 2: Verify file loads**

Run: `node --check browser-extension/lib/api.js`
Expected: no output (syntax OK; note: ESM `export` fails under plain `node --check`? It passes — check only parses modules as script... if it errors, use `node --input-type=module --check < file`. Either way, no logic test; consumers import it.)

- [ ] **Step 3: Commit**

```bash
git add browser-extension/lib/api.js docs/superpowers/plans/2026-10-05-extension.md
git commit -m "feat(ext): add shared api lib with batched save"
```

### Task 2: Manifest + background (permissions, side panel, context menus)

**Files:**
- Modify: `browser-extension/manifest.json`
- Create: `browser-extension/background.js`

- [ ] **Step 1: Update `browser-extension/manifest.json`**

```json
{
  "manifest_version": 3,
  "name": "Keepfor.me",
  "version": "0.2.0",
  "description": "Save pages and articles to your Keepfor.me personal library",
  "icons": { "16": "icon16.png", "48": "icon48.png", "128": "icon128.png" },
  "permissions": ["activeTab", "storage", "tabs", "history", "contextMenus", "sidePanel"],
  "host_permissions": [
    "https://app.keepfor.me/*",
    "https://keepfor.me/*",
    "https://*.workers.dev/*",
    "http://localhost/*"
  ],
  "action": { "default_popup": "popup.html", "default_title": "Save to Keepfor.me" },
  "background": { "service_worker": "background.js" },
  "side_panel": { "default_path": "sidepanel.html" },
  "commands": {
    "_execute_action": {
      "suggested_key": { "default": "Alt+S", "mac": "Alt+S" },
      "description": "Open Keepfor.me Save Dialog"
    }
  }
}
```

- [ ] **Step 2: Create `browser-extension/background.js`**

```js
// Service worker: context menus -> single save + notification via chrome.notifications? No
// notifications perm requested; use badge text + last-result in storage for popup/panel to show.
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "kfm-save-page", title: "Save page to Keepfor.me", contexts: ["page"] });
  chrome.contextMenus.create({ id: "kfm-save-link", title: "Save link to Keepfor.me", contexts: ["link"] });
});

async function saveViaConfig(url) {
  const { workerUrl, patToken } = await chrome.storage.sync.get(["workerUrl", "patToken"]);
  if (!workerUrl || !patToken) throw new Error("Configure Worker URL + PAT first");
  const base = workerUrl.trim().replace(/\/+$/, "");
  const res = await fetch(`${base}/api/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${patToken}` },
    body: JSON.stringify({ url, tags: [] }),
  });
  if (!res.ok) throw new Error(`Error ${res.status}: ${await res.text()}`);
  return res.json();
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.menuItemId === "kfm-save-link" ? info.linkUrl : tab?.url;
  if (!url) return;
  try {
    const data = await saveViaConfig(url);
    await chrome.storage.local.set({ kfmLast: { ok: true, url, is_new: data.is_new, at: Date.now() } });
  } catch (e) {
    await chrome.storage.local.set({ kfmLast: { ok: false, url, error: String(e.message || e), at: Date.now() } });
  }
});

// Allow popup/panel "open side panel" button.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "kfm-open-panel") {
    chrome.sidePanel.open({ windowId: sender?.tab?.windowId }).then(() => sendResponse({ ok: true }));
    return true;
  }
});
```

- [ ] **Step 3: Validate JSON + syntax**

Run: `python3 -c "import json;json.load(open('browser-extension/manifest.json'))" && node --check browser-extension/background.js`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add browser-extension/manifest.json browser-extension/background.js
git commit -m "feat(ext): side panel manifest + context-menu background"
```

### Task 3: Popup fast-path (library link, dupe badge, panel launcher)

**Files:**
- Modify: `browser-extension/popup.html`
- Modify: `browser-extension/popup.js`

- [ ] **Step 1: Edit `browser-extension/popup.html`** — add footer row after `#status`:

```html
<div id="ext-links" style="display:flex;gap:8px;margin-top:10px;font-size:12px;">
  <a href="#" id="open-library">Open library ↗</a>
  <a href="#" id="open-panel" style="margin-left:auto">Bulk / history →</a>
</div>
<div id="dupe" class="status"></div>
```

Keep everything else identical (320px card, main/config views).

- [ ] **Step 2: Edit `browser-extension/popup.js`** — append handlers (keep existing save flow; only add):

```js
// Library link + panel launcher (append inside DOMContentLoaded).
document.getElementById("open-library").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.storage.sync.get(["workerUrl"], (res) => {
    const base = (res.workerUrl || "https://app.keepfor.me").trim().replace(/\/+$/, "");
    chrome.tabs.create({ url: base });
  });
});
document.getElementById("open-panel").addEventListener("click", async (e) => {
  e.preventDefault();
  try { await chrome.sidePanel.open({ windowId: chrome.windows.WINDOW_ID_CURRENT }); }
  catch { chrome.runtime.sendMessage({ type: "kfm-open-panel" }); }
});
// Dupe badge: after successful save, if !data.is_new show amber line.
// (Wire into existing success branch: statusDiv shows Saved&Queued / Updated tags;
// add: if (!data.is_new) dupe.textContent = "Already in library — open it from the library link above.")
```

Also surface `chrome.storage.local kfmLast` (context-menu result) at popup open: if set within 60s and not yet shown, render into `#status`.

- [ ] **Step 3: Manual check** — `chrome://extensions` → Load unpacked `browser-extension/` → popup opens, links present, no console errors.

- [ ] **Step 4: Commit**

```bash
git add browser-extension/popup.html browser-extension/popup.js
git commit -m "feat(ext): popup library link, dupe badge, panel launcher"
```

### Task 4: Side panel shell + Tabs bulk-save

**Files:**
- Create: `browser-extension/sidepanel.html`
- Create: `browser-extension/sidepanel.js` (part 1: tabs)

- [ ] **Step 1: Create `browser-extension/sidepanel.html`** — nav + four sections:

```html
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Keepfor.me</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:12px;color:#1f2937;min-width:280px}
nav{display:flex;gap:4px;margin-bottom:10px}nav button{flex:1;padding:7px;border:1px solid #d1d5db;background:#f9fafb;border-radius:6px;cursor:pointer;font-size:12px}nav button.active{background:#0f172a;color:#fff;border-color:#0f172a}
.row{display:flex;gap:6px;align-items:center;margin:6px 0}.row input[type=text]{flex:1;padding:6px 8px;font-size:12px;border:1px solid #d1d5db;border-radius:6px}
.list{max-height:55vh;overflow:auto;border:1px solid #e5e7eb;border-radius:6px}.item{display:flex;gap:8px;padding:6px 8px;border-bottom:1px solid #f3f4f6;font-size:12px;align-items:flex-start}.item img{width:14px;height:14px;margin-top:2px}.item .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.item .u{color:#6b7280;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btn{width:100%;margin-top:8px;padding:8px;background:#0f172a;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer}.btn:disabled{opacity:.5}
.prog{font-size:12px;margin-top:6px}.err{color:#dc2626;font-size:12px;white-space:pre-wrap}
section{display:none}section.active{display:block}
</style></head><body>
<nav><button data-tab="tabs" class="active">Tabs</button><button data-tab="history">History</button><button data-tab="search">Search</button><button data-tab="chat">Ask</button></nav>
<section id="tab-tabs" class="active">
  <div class="row"><button id="scope-win">This window</button><button id="scope-all">All windows</button><span id="tabs-count"></span></div>
  <div class="row"><input type="text" id="tabs-filter" placeholder="Filter title or URL…"><button id="tabs-all">All</button><button id="tabs-none">None</button></div>
  <div class="row"><input type="text" id="tabs-tags" placeholder="Tags for batch (comma separated)"></div>
  <div id="tabs-list" class="list"></div>
  <button id="tabs-save" class="btn">Save selected</button><div id="tabs-prog" class="prog"></div><div id="tabs-err" class="err"></div>
</section>
<section id="tab-history">
  <div class="row"><select id="hist-range"><option value="86400000">Last 24h</option><option value="604800000" selected>Last 7 days</option><option value="2592000000">Last 30 days</option><option value="0">All time</option></select>
  <select id="hist-max"><option value="100">100</option><option value="500" selected>500</option></select></div>
  <div class="row"><input type="text" id="hist-q" placeholder="Search history…"><button id="hist-go">Search</button></div>
  <div class="row"><input type="text" id="hist-tags" placeholder="Tags for batch (comma separated)"></div>
  <div id="hist-list" class="list"></div>
  <button id="hist-save" class="btn">Save selected</button><div id="hist-prog" class="prog"></div><div id="hist-err" class="err"></div>
</section>
<section id="tab-search"><div class="row"><input type="text" id="s-q" placeholder="Search your library…"><button id="s-go">Go</button></div><div id="s-list" class="list"></div></section>
<section id="tab-chat"><div class="row"><input type="text" id="c-q" placeholder="Ask your library…"><button id="c-go">Ask</button></div><div id="c-list" class="list"></div><div style="font-size:11px;color:#6b7280">Grounded search over your saves (no generative answer in V1).</div></section>
<script type="module" src="sidepanel.js"></script>
</body></html>
```

- [ ] **Step 2: Create `browser-extension/sidepanel.js` part 1** — nav + tabs logic:

```js
import { saveMany, dedupeByUrl, isSkippableUrl } from "./lib/api.js";
const $ = (id) => document.getElementById(id);
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll("section").forEach((s) => s.classList.remove("active"));
  b.classList.add("active");
  $("tab-" + b.dataset.tab).classList.add("active");
}));
let tabRows = [], scope = "win";
async function loadTabs() {
  const wins = scope === "all" ? await chrome.tabs.query({}) : await chrome.tabs.query({ currentWindow: true });
  tabRows = dedupeByUrl(wins.filter((t) => t.url && !isSkippableUrl(t.url))
    .map((t) => ({ url: t.url, title: t.title || t.url, fav: t.favIconUrl || "" })));
  renderTabs("");
}
function renderTabs(f) {
  const q = (f || "").toLowerCase();
  const rows = tabRows.filter((r) => !q || r.title.toLowerCase().includes(q) || r.url.toLowerCase().includes(q));
  $("tabs-count").textContent = `${rows.length} tabs`;
  $("tabs-list").innerHTML = rows.map((r, i) =>
    `<label class="item"><input type="checkbox" data-i="${i}" checked><img src="${r.fav}" onerror="this.remove()"><span><span class="t">${r.title.replace(/</g, "&lt;")}</span><br><span class="u">${new URL(r.url).host}</span></span></label>`).join("");
  $("tabs-list").querySelectorAll("input").forEach((c) => c._row = rows[+c.dataset.i]);
  $("tabs-save").textContent = `Save selected (${rows.length})`;
}
$("tabs-filter").addEventListener("input", (e) => renderTabs(e.target.value));
$("tabs-all").addEventListener("click", () => $("tabs-list").querySelectorAll("input").forEach((c) => c.checked = true));
$("tabs-none").addEventListener("click", () => $("tabs-list").querySelectorAll("input").forEach((c) => c.checked = false));
$("scope-win").addEventListener("click", () => { scope = "win"; loadTabs(); });
$("scope-all").addEventListener("click", () => { scope = "all"; loadTabs(); });
$("tabs-save").addEventListener("click", async () => {
  const checked = [...$("tabs-list").querySelectorAll("input")].filter((c) => c.checked).map((c) => c._row);
  if (checked.length > 200 && !confirm(`Save ${checked.length} items?`)) return;
  if (checked.length > 100) alert(`Large batch (${checked.length}) — saving with concurrency 5.`);
  const { workerUrl, patToken } = await chrome.storage.sync.get(["workerUrl", "patToken"]);
  const base = (workerUrl || "").trim().replace(/\/+$/, "");
  const tags = $("tabs-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  const items = checked.map((r) => ({ url: r.url, tags }));
  $("tabs-save").disabled = true; $("tabs-err").textContent = "";
  const res = await saveMany(base, patToken, items, {
    onProgress: (d, t) => { $("tabs-prog").textContent = `${d}/${t} saved`; },
  });
  const fails = res.filter((r) => !r.ok);
  $("tabs-prog").textContent = `${res.length - fails.length}/${res.length} saved${fails.length ? `, ${fails.length} failed` : ""}`;
  $("tabs-err").textContent = fails.map((f) => `${f.url}: ${f.error}`).join("\n");
  $("tabs-save").disabled = false;
});
loadTabs();
```

- [ ] **Step 3: Manual check** — load unpacked, open side panel (action icon → Open side panel or via popup link), Tabs lists current window, filter + Save updates progress.

- [ ] **Step 4: Commit**

```bash
git add browser-extension/sidepanel.html browser-extension/sidepanel.js
git commit -m "feat(ext): side panel shell + tabs bulk save"
```

### Task 5: History + Search + Chat-lite

**Files:**
- Modify: `browser-extension/sidepanel.js` (append)

- [ ] **Step 1: Append history logic to `browser-extension/sidepanel.js`**

```js
import { searchLib, mcpSearch } from "./lib/api.js";
$("hist-go").addEventListener("click", async () => {
  const range = +$("hist-range").value, max = +$("hist-max").value, text = $("hist-q").value || "";
  const startTime = range ? Date.now() - range : 0;
  const raw = await chrome.history.search({ text, startTime, maxResults: max });
  const rows = dedupeByUrl(raw.filter((h) => h.url && !isSkippableUrl(h.url))
    .map((h) => ({ url: h.url, title: h.title || h.url })));
  $("hist-list").innerHTML = rows.map((r, i) =>
    `<label class="item"><input type="checkbox" data-i="${i}" checked><span><span class="t">${String(r.title).replace(/</g, "&lt;")}</span><br><span class="u">${r.url}</span></span></label>`).join("");
  $("hist-list").querySelectorAll("input").forEach((c) => c._row = rows[+c.dataset.i]);
  $("hist-save").textContent = `Save selected (${rows.length})`;
  if (!rows.length) $("hist-list").innerHTML = `<div class="item">No history in range.</div>`;
});
$("hist-save").addEventListener("click", async () => {
  const checked = [...$("hist-list").querySelectorAll("input")].filter((c) => c.checked).map((c) => c._row);
  if (!checked.length) return;
  if (checked.length > 200 && !confirm(`Save ${checked.length} items?`)) return;
  const { workerUrl, patToken } = await chrome.storage.sync.get(["workerUrl", "patToken"]);
  const base = (workerUrl || "").trim().replace(/\/+$/, "");
  const tags = $("hist-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  $("hist-save").disabled = true;
  const { saveMany: _sm } = await import("./lib/api.js");
  const res = await _sm(base, patToken, checked.map((r) => ({ url: r.url, tags })), {
    onProgress: (d, t) => { $("hist-prog").textContent = `${d}/${t} saved`; },
  });
  const fails = res.filter((r) => !r.ok);
  $("hist-prog").textContent = `${res.length - fails.length}/${res.length} saved${fails.length ? `, ${fails.length} failed` : ""}`;
  $("hist-err").textContent = fails.map((f) => `${f.url}: ${f.error}`).join("\n");
  $("hist-save").disabled = false;
});
```

(Note: `saveMany` already imported in part 1 — reuse the top import in final file; do not duplicate the import. Final file has ONE import line: `import { saveMany, searchLib, mcpSearch, dedupeByUrl, isSkippableUrl } from "./lib/api.js";`.)

- [ ] **Step 2: Append search + chat-lite logic**

```js
async function cfg() {
  const { workerUrl, patToken } = await chrome.storage.sync.get(["workerUrl", "patToken"]);
  return { base: (workerUrl || "").trim().replace(/\/+$/, ""), pat: patToken };
}
$("s-go").addEventListener("click", async () => {
  const { base, pat } = await cfg();
  const q = $("s-q").value.trim(); if (!q) return;
  $("s-list").innerHTML = `<div class="item">Searching…</div>`;
  try {
    const items = await searchLib(base, pat, q, 20);
    const arr = Array.isArray(items) ? items : items.items || [];
    $("s-list").innerHTML = arr.length ? arr.map((it) =>
      `<div class="item"><span><a href="${it.url}" target="_blank">${String(it.title || it.url).replace(/</g, "&lt;")}</a><br><span class="u">${it.url}</span></span></div>`).join("")
      : `<div class="item">0 results (search may have degraded).</div>`;
  } catch (e) { $("s-list").innerHTML = `<div class="item err">${String(e.message || e)}</div>`; }
});
$("c-go").addEventListener("click", async () => {
  const { base, pat } = await cfg();
  const q = $("c-q").value.trim(); if (!q) return;
  $("c-list").innerHTML = `<div class="item">Asking your library…</div>`;
  try {
    const resp = await mcpSearch(base, pat, q, 10);
    const text = JSON.stringify(resp);
    let items = [];
    try { const parsed = resp.result ? JSON.parse(resp.result.content?.[0]?.text || "[]") : resp; items = parsed.items || parsed || []; } catch { items = []; }
    if (!items.length) $("c-list").innerHTML = `<div class="item">No grounded hits. Raw: ${text.slice(0, 300).replace(/</g, "&lt;")}</div>`;
    else $("c-list").innerHTML = items.map((it) =>
      `<div class="item"><span><a href="${it.url}" target="_blank">${String(it.title || it.url).replace(/</g, "&lt;")}</a><br><span class="u">${it.url}</span></span></div>`).join("");
  } catch (e) { $("c-list").innerHTML = `<div class="item err">${String(e.message || e)}</div>`; }
});
```

- [ ] **Step 3: Fix single import line** — final top of `sidepanel.js` must read:

```js
import { saveMany, searchLib, mcpSearch, dedupeByUrl, isSkippableUrl } from "./lib/api.js";
```

- [ ] **Step 4: Manual check** — history 7d search renders; search tab returns library hits; ask tab renders grounded cards.

- [ ] **Step 5: Commit**

```bash
git add browser-extension/sidepanel.js
git commit -m "feat(ext): history import, library search, chat-lite"
```

### Task 6: AI suggest-tags (optional) + final pass

**Files:**
- Modify: `browser-extension/sidepanel.js` (append), `browser-extension/popup.js` (optional button)

- [ ] **Step 1: Append feature-detected suggest-tags to `sidepanel.js` Tabs section**

```js
if (window.LanguageModel || window.ai?.canCreateTextSession) {
  const b = document.createElement("button");
  b.textContent = "Suggest tags (on-device)";
  b.className = "btn"; b.style.background = "#334155";
  b.addEventListener("click", async () => {
    try {
      const titles = tabRows.slice(0, 5).map((r) => r.title).join("; ");
      let out = "";
      if (window.LanguageModel) {
        const s = await window.LanguageModel.create();
        out = await s.prompt(`Suggest 5 comma-separated lowercase tags for these tabs: ${titles}`);
      } else { const s = await window.ai.createTextSession(); out = await s.prompt(`Suggest tags: ${titles}`); }
      $("tabs-tags").value = out.split(/[,\n]/).map((t) => t.trim().toLowerCase()).filter(Boolean).slice(0, 8).join(", ");
    } catch (e) { $("tabs-err").textContent = `On-device AI unavailable: ${e.message}`; }
  });
  $("tab-tabs").appendChild(b);
}
```

Hidden entirely when APIs absent — save flow never depends on it.

- [ ] **Step 2: Full manual matrix + lint**

Run: `python3 -c "import json;json.load(open('browser-extension/manifest.json'))" && node --check browser-extension/background.js && node --check browser-extension/popup.js`
Expected: clean. Then: fresh-install gate, single new vs dupe badge, tabs win/all + filter + 50-batch w/ bad URL → retry, history range+search, search, ask, context-menu page+link, library link trailing slash.

- [ ] **Step 3: Commit**

```bash
git add browser-extension/sidepanel.js browser-extension/popup.js
git commit -m "feat(ext): optional on-device tag suggestions"
```

## Self-Review

- Spec §1 (arch/perms/batch) → Tasks 1–2. Spec §2 (tabs/history) → Task 4 + Task 5a. Spec §3 (search/chat-lite/dupe/context/AI) → Tasks 3, 5b, 6. Spec §4 (errors/caps/testing) → inline in Tasks 4–6 (confirm>200, warn>100, per-URL errors, empty states).
- No TODO/TBD; every code step shows complete code; single import line de-duplicated in Task 5.
- Types consistent: `saveMany(base, pat, [{url, tags}])`, `mcpSearch` wraps `tools/call search_library`.
