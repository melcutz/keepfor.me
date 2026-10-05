import { saveMany, searchLib, mcpSearch, dedupeByUrl, isSkippableUrl } from "./lib/api.js";
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
  const res = await saveMany(base, patToken, checked.map((r) => ({ url: r.url, tags })), {
    onProgress: (d, t) => { $("hist-prog").textContent = `${d}/${t} saved`; },
  });
  const fails = res.filter((r) => !r.ok);
  $("hist-prog").textContent = `${res.length - fails.length}/${res.length} saved${fails.length ? `, ${fails.length} failed` : ""}`;
  $("hist-err").textContent = fails.map((f) => `${f.url}: ${f.error}`).join("\n");
  $("hist-save").disabled = false;
});
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
