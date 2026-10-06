document.addEventListener("DOMContentLoaded", async () => {
  const mainView = document.getElementById("main-view");
  const configView = document.getElementById("config-view");
  const toggleConfig = document.getElementById("toggle-config");
  const closeConfig = document.getElementById("close-config");

  const urlInput = document.getElementById("url-input");
  const titleInput = document.getElementById("title-input");
  const tagsInput = document.getElementById("tags-input");
  const saveBtn = document.getElementById("save-btn");
  const statusDiv = document.getElementById("status");

  const workerUrlInput = document.getElementById("worker-url");
  const patTokenInput = document.getElementById("pat-token");
  const saveConfigBtn = document.getElementById("save-config-btn");
  const configStatus = document.getElementById("config-status");

  // Load active tab info
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs && tabs[0]) {
      urlInput.value = tabs[0].url || "";
      titleInput.value = tabs[0].title || "";
    }
  });

  // Load saved config
  chrome.storage.sync.get(["workerUrl", "patToken"], (res) => {
    if (res.workerUrl) workerUrlInput.value = res.workerUrl;
    if (res.patToken) patTokenInput.value = res.patToken;
    if (!res.workerUrl || !res.patToken) {
      showConfig();
      configStatus.textContent = "Please configure your Worker URL and PAT first.";
      configStatus.className = "status error";
    }
  });

  function showConfig() {
    mainView.classList.add("hidden");
    configView.classList.add("active");
  }

  function showMain() {
    configView.classList.remove("active");
    mainView.classList.remove("hidden");
  }

  toggleConfig.addEventListener("click", (e) => { e.preventDefault(); showConfig(); });
  closeConfig.addEventListener("click", (e) => { e.preventDefault(); showMain(); });

  saveConfigBtn.addEventListener("click", async () => {
    const workerUrl = workerUrlInput.value.trim().replace(/\/+$/, "");
    const patToken = patTokenInput.value.trim();
    if (!workerUrl || !patToken) {
      configStatus.textContent = "Enter both Worker URL and PAT.";
      configStatus.className = "status error";
      return;
    }
    configStatus.textContent = "Checking connection…";
    configStatus.className = "status";
    try {
      const r = await fetch(`${workerUrl}/api/items?limit=1`, {
        headers: { Authorization: `Bearer ${patToken}` },
      });
      if (!r.ok) throw new Error(`Error ${r.status}: ${(await r.text()).slice(0, 200)}`);
    } catch (e) {
      configStatus.textContent = `Connection failed: ${e.message}`;
      configStatus.className = "status error";
      return;
    }
    chrome.storage.sync.set({ workerUrl, patToken }, () => {
      configStatus.textContent = "Settings saved! ✓";
      configStatus.className = "status success";
      setTimeout(showMain, 800);
    });
  });

  saveBtn.addEventListener("click", async () => {
    statusDiv.textContent = "Saving...";
    statusDiv.className = "status";
    saveBtn.disabled = true;

    chrome.storage.sync.get(["workerUrl", "patToken"], async (res) => {
      if (!res.workerUrl || !res.patToken) {
        statusDiv.textContent = "Configure settings first!";
        statusDiv.className = "status error";
        saveBtn.disabled = false;
        return;
      }

      const tags = tagsInput.value.split(",").map(t => t.trim()).filter(Boolean);
      const payload = {
        url: urlInput.value.trim(),
        tags: tags
      };

      try {
        const base = res.workerUrl.trim().replace(/\/+$/, "");
        const response = await fetch(`${base}/api/save`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${res.patToken}`
          },
          body: JSON.stringify(payload)
        });

        if (!response.ok) {
          const errText = await response.text();
          throw new Error(`Error ${response.status}: ${errText}`);
        }

        const data = await response.json();
        statusDiv.textContent = data.is_new ? "Saved & Queued!" : "Updated tags!";
        statusDiv.className = "status success";
        if (!data.is_new) {
          const dupe = document.getElementById("dupe");
          if (dupe) dupe.textContent = "Already in library — open it from the library link above.";
        }
        setTimeout(() => window.close(), 2500);
      } catch (err) {
        statusDiv.textContent = err.message;
        statusDiv.className = "status error";
        saveBtn.disabled = false;
      }
    });
  });

  // Library link + panel launcher.
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

  // Surface context-menu result stored by background.js.
  chrome.storage.local.get(["kfmLast"], (r) => {
    const last = r && r.kfmLast;
    if (last && Date.now() - last.at < 60000) {
      if (last.ok) {
        statusDiv.textContent = last.is_new === false ? "Updated tags!" : "Saved & Queued!";
        statusDiv.className = "status success";
      } else {
        statusDiv.textContent = last.error || "Save failed";
        statusDiv.className = "status error";
      }
    }
  });
});
