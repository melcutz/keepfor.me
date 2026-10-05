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
