// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Claudiu Branzan

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

function setBadge(ok) {
  try {
    chrome.action.setBadgeText({ text: ok ? "✓" : "!" });
    chrome.action.setBadgeBackgroundColor({ color: ok ? "#16a34a" : "#dc2626" });
    setTimeout(() => chrome.action.setBadgeText({ text: "" }), 4000);
  } catch {}
}
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.menuItemId === "kfm-save-link" ? info.linkUrl : tab?.url;
  if (!url) return;
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
