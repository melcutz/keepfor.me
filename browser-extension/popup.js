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

  saveConfigBtn.addEventListener("click", () => {
    const workerUrl = workerUrlInput.value.trim().replace(/\/+$/, "");
    const patToken = patTokenInput.value.trim();
    chrome.storage.sync.set({ workerUrl, patToken }, () => {
      configStatus.textContent = "Settings saved!";
      configStatus.className = "status success";
      setTimeout(showMain, 600);
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
        const response = await fetch(`${res.workerUrl}/api/save`, {
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
        setTimeout(() => window.close(), 1200);
      } catch (err) {
        statusDiv.textContent = err.message;
        statusDiv.className = "status error";
        saveBtn.disabled = false;
      }
    });
  });
});
