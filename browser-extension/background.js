// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Claudiu Branzan

// Service worker: context menus -> single save + notification via chrome.notifications? No
// notifications perm requested; use badge text + last-result in storage for popup/panel to show.
const HTTP_PATTERNS = ["http://*/*", "https://*/*"];
let badgeTimer = null;

function isHttpUrl(url) {
  return /^https?:\/\//i.test(url || "");
}

function parseApiError(status, text) {
  let detail = "";
  const raw = (text || "").trim();
  if (raw) {
    try {
      const data = JSON.parse(raw);
      if (typeof data?.detail === "string") {
        detail = data.detail;
      } else if (Array.isArray(data?.detail)) {
        detail = data.detail
          .map((d) => (typeof d?.msg === "string" ? d.msg : JSON.stringify(d)))
          .filter(Boolean)
          .join("; ");
      }
    } catch {
      detail = raw.slice(0, 200);
    }
  }
  return detail ? `Error ${status}: ${detail}` : `Error ${status}`;
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "kfm-save-page",
    title: "Save page to Keepfor.me",
    contexts: ["page"],
    documentUrlPatterns: HTTP_PATTERNS,
  });
  chrome.contextMenus.create({
    id: "kfm-save-link",
    title: "Save link to Keepfor.me",
    contexts: ["link"],
    targetUrlPatterns: HTTP_PATTERNS,
  });
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
  if (!res.ok) throw new Error(parseApiError(res.status, await res.text()));
  return res.json();
}

function setBadge(ok) {
  try {
    if (badgeTimer) clearTimeout(badgeTimer);
    chrome.action.setBadgeText({ text: ok ? "✓" : "!" });
    chrome.action.setBadgeBackgroundColor({ color: ok ? "#2563eb" : "#dc2626" });
    badgeTimer = setTimeout(() => {
      badgeTimer = null;
      chrome.action.setBadgeText({ text: "" });
    }, 4000);
  } catch {}
}
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.menuItemId === "kfm-save-link" ? info.linkUrl : tab?.url;
  if (!url || !isHttpUrl(url)) return;
  try {
    const data = await saveViaConfig(url);
    await chrome.storage.local.set({ kfmLast: { ok: true, url, is_new: data.is_new, at: Date.now() } });
    setBadge(true);
  } catch (e) {
    await chrome.storage.local.set({ kfmLast: { ok: false, url, error: String(e.message || e), at: Date.now() } });
    setBadge(false);
  }
});

// Allow popup/panel "open side panel" button.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "kfm-open-panel") {
    (async () => {
      try {
        const win = sender?.tab?.windowId ?? (await chrome.windows.getCurrent()).id;
        await chrome.sidePanel.open({ windowId: win });
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: String(e.message || e) });
      }
    })();
    return true;
  }
});
