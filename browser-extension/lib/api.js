// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Claudiu Branzan

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

export const searchLib = (base, pat, query, limit = 20) => {
  if (!base || !pat) throw new Error("Configure Worker URL + PAT first");
  return req(base, "/api/search", {
    method: "POST",
    body: { query, mode: "hybrid", limit },
    pat,
  });
};

export function isSkippableUrl(url) {
  return /^(chrome|edge|about|chrome-extension|brave|opera|file|view-source|data|javascript|blob):/i.test(
    url || ""
  );
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
