import { saveMany, searchLib, dedupeByUrl, isSkippableUrl } from "./lib/api.js";
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
function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function hostOf(url) {
  try { return new URL(url).host || url; } catch { return url; }
}
function checkedCount(listId) {
  return [...$(listId).querySelectorAll("input")].filter((c) => c.checked).length;
}
function updateSaveLabels() {
  const tTotal = $("tabs-list").querySelectorAll("input").length;
  const tChecked = checkedCount("tabs-list");
  $("tabs-save").textContent = `Save selected (${tChecked}/${tTotal})`;
  $("tabs-save").disabled = tChecked === 0;
  const hTotal = $("hist-list").querySelectorAll("input").length;
  const hChecked = checkedCount("hist-list");
  $("hist-save").textContent = `Save selected (${hChecked}/${hTotal})`;
  $("hist-save").disabled = hChecked === 0;
}
function renderTabs(f) {
  const q = (f || "").toLowerCase();
  const rows = tabRows.filter((r) => !q || r.title.toLowerCase().includes(q) || r.url.toLowerCase().includes(q));
  $("tabs-count").textContent = `${rows.length} tabs`;
  $("tabs-list").innerHTML = rows.map((r, i) =>
    `<label class="item"><input type="checkbox" data-i="${i}" checked><img src="${esc(r.fav)}" onerror="this.remove()"><span><span class="t">${esc(r.title)}</span><br><span class="u">${esc(hostOf(r.url))}</span></span></label>`).join("");
  $("tabs-list").querySelectorAll("input").forEach((c) => c._row = rows[+c.dataset.i]);
  updateSaveLabels();
}
$("tabs-filter").addEventListener("input", (e) => renderTabs(e.target.value));
$("tabs-all").addEventListener("click", () => { $("tabs-list").querySelectorAll("input").forEach((c) => c.checked = true); updateSaveLabels(); });
$("tabs-none").addEventListener("click", () => { $("tabs-list").querySelectorAll("input").forEach((c) => c.checked = false); updateSaveLabels(); });
$("tabs-list").addEventListener("change", updateSaveLabels);
$("hist-list").addEventListener("change", updateSaveLabels);
$("scope-win").addEventListener("click", () => { scope = "win"; loadTabs(); });
$("scope-all").addEventListener("click", () => { scope = "all"; loadTabs(); });
function markSaved(listId, okUrls) {
  const ok = new Set(okUrls);
  [...$(listId).querySelectorAll("input")].forEach((c) => {
    if (c._row && ok.has(c._row.url)) {
      const label = c.closest("label");
      if (label && !label.querySelector(".saved-tick")) {
        const s = document.createElement("span");
        s.className = "saved-tick";
        s.textContent = " ✓";
        s.style.color = "#16a34a";
        label.querySelector(".t")?.appendChild(s);
      }
    }
  });
}
$("tabs-save").addEventListener("click", async () => {
  const boxes = [...$("tabs-list").querySelectorAll("input")];
  const checked = boxes.filter((c) => c.checked).map((c) => c._row);
  if (!checked.length) { $("tabs-prog").textContent = "Select at least one tab first."; return; }
  if (checked.length > 200 && !confirm(`Save ${checked.length} items?`)) return;
  const { workerUrl, patToken } = await chrome.storage.sync.get(["workerUrl", "patToken"]);
  const base = (workerUrl || "").trim().replace(/\/+$/, "");
  if (!base || !patToken) { $("tabs-prog").textContent = "Configure Worker URL + PAT first (extension popup → Settings)."; return; }
  const tags = $("tabs-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  const items = checked.map((r) => ({ url: r.url, tags }));
  $("tabs-save").disabled = true; $("tabs-err").textContent = "";
  $("tabs-prog").textContent = `Saving 0/${items.length}…`;
  const res = await saveMany(base, patToken, items, {
    onProgress: (d, t) => { $("tabs-prog").textContent = `Saving ${d}/${t}…`; },
  });
  const fails = res.filter((r) => !r.ok);
  const done = res.length - fails.length;
  $("tabs-prog").textContent = done === res.length ? `Saved ${done}/${res.length} ✓` : `Saved ${done}/${res.length}, ${fails.length} failed`;
  $("tabs-prog").style.color = fails.length ? "" : "#16a34a";
  $("tabs-err").textContent = fails.map((f) => `${f.url}: ${f.error}`).join("\n");
  markSaved("tabs-list", res.filter((r) => r.ok).map((r) => r.url));
  updateSaveLabels();
});
loadTabs().catch((e) => {
  $("tabs-list").innerHTML = `<div class="item err">${esc(e.message || e)}</div>`;
  updateSaveLabels();
});
updateSaveLabels();
$("hist-go").addEventListener("click", async () => {
  const range = +$("hist-range").value, max = +$("hist-max").value, text = $("hist-q").value || "";
  const startTime = range ? Date.now() - range : 0;
  const raw = await chrome.history.search({ text, startTime, maxResults: max });
  const rows = dedupeByUrl(raw.filter((h) => h.url && !isSkippableUrl(h.url))
    .map((h) => ({ url: h.url, title: h.title || h.url })));
  $("hist-list").innerHTML = rows.map((r, i) =>
    `<label class="item"><input type="checkbox" data-i="${i}" checked><span><span class="t">${esc(r.title)}</span><br><span class="u">${esc(r.url)}</span></span></label>`).join("");
  $("hist-list").querySelectorAll("input").forEach((c) => c._row = rows[+c.dataset.i]);
  updateSaveLabels();
  if (!rows.length) $("hist-list").innerHTML = `<div class="item">No history in range.</div>`;
});
$("hist-save").addEventListener("click", async () => {
  const checked = [...$("hist-list").querySelectorAll("input")].filter((c) => c.checked).map((c) => c._row);
  if (!checked.length) { $("hist-prog").textContent = "Select at least one item first."; return; }
  if (checked.length > 200 && !confirm(`Save ${checked.length} items?`)) return;
  const { workerUrl, patToken } = await chrome.storage.sync.get(["workerUrl", "patToken"]);
  const base = (workerUrl || "").trim().replace(/\/+$/, "");
  if (!base || !patToken) { $("hist-prog").textContent = "Configure Worker URL + PAT first (extension popup → Settings)."; return; }
  const tags = $("hist-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  $("hist-save").disabled = true; $("hist-err").textContent = "";
  $("hist-prog").textContent = `Saving 0/${checked.length}…`;
  const res = await saveMany(base, patToken, checked.map((r) => ({ url: r.url, tags })), {
    onProgress: (d, t) => { $("hist-prog").textContent = `Saving ${d}/${t}…`; },
  });
  const fails = res.filter((r) => !r.ok);
  const done = res.length - fails.length;
  $("hist-prog").textContent = done === res.length ? `Saved ${done}/${res.length} ✓` : `Saved ${done}/${res.length}, ${fails.length} failed`;
  $("hist-prog").style.color = fails.length ? "" : "#16a34a";
  $("hist-err").textContent = fails.map((f) => `${f.url}: ${f.error}`).join("\n");
  markSaved("hist-list", res.filter((r) => r.ok).map((r) => r.url));
  updateSaveLabels();
});
async function cfg() {
  const { workerUrl, patToken } = await chrome.storage.sync.get(["workerUrl", "patToken"]);
  return { base: (workerUrl || "").trim().replace(/\/+$/, ""), pat: patToken };
}
async function runSearch() {
  const { base, pat } = await cfg();
  const q = $("s-q").value.trim();
  if (!q) { $("s-list").innerHTML = `<div class="item">Type a query first, then press Go or Enter.</div>`; return; }
  if (!base || !pat) { $("s-list").innerHTML = `<div class="item err">Configure Worker URL + PAT first (extension popup → Settings).</div>`; return; }
  $("s-list").innerHTML = `<div class="item">Searching…</div>`;
  try {
    const items = await searchLib(base, pat, q, 20);
    const arr = Array.isArray(items) ? items : items.items || [];
    $("s-list").innerHTML = arr.length ? arr.map((it) =>
      `<div class="item"><span><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title || it.url)}</a><br><span class="u">${esc(it.url)}</span></span></div>`).join("")
      : `<div class="item">0 results (search may have degraded).</div>`;
  } catch (e) { $("s-list").innerHTML = `<div class="item err">${esc(e.message || e)}</div>`; }
}
$("s-go").addEventListener("click", runSearch);
$("s-q").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
$("hist-q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("hist-go").click(); });
$("tabs-filter").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
if (window.LanguageModel || window.ai?.canCreateTextSession) {
  const b = document.createElement("button");
  b.textContent = "Suggest tags (on-device)";
  b.className = "btn"; b.style.background = "#334155";
  b.addEventListener("click", async () => {
    try {
      const titles = tabRows.slice(0, 5).map((r) => r.title).join("; ");
      let out = "";
      if (window.LanguageModel) {
        const s = await window.LanguageModel.create({
          expectedInputs: [{ type: "text", languages: ["en"] }],
          expectedOutputs: [{ type: "text", languages: ["en"] }],
        });
        out = await s.prompt(`Suggest 5 comma-separated lowercase tags for these tabs: ${titles}`);
      } else { const s = await window.ai.createTextSession(); out = await s.prompt(`Suggest tags: ${titles}`); }
      $("tabs-tags").value = out.split(/[,\n]/).map((t) => t.trim().toLowerCase()).filter(Boolean).slice(0, 8).join(", ");
    } catch (e) { $("tabs-err").textContent = `On-device AI unavailable: ${e.message}`; }
  });
  $("tab-tabs").appendChild(b);
}
