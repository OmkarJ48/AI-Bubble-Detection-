(function () {
  const root = document.querySelector(".leak-detector-embed");
  if (!root) {
    return;
  }

  const countEl = document.getElementById("count");
  const errorEl = document.getElementById("camera-error");
  const resetBackgroundButton = document.getElementById("reset-background-button");
  const resetCountButton = document.getElementById("reset-count-button");

  const statusUrl = root.dataset.statusUrl || "/status";
  const resetBackgroundUrl =
    root.dataset.resetBackgroundUrl || "/reset-background";
  const resetCountUrl = root.dataset.resetCountUrl || "/reset-count";

  let actionInFlight = false;
  let refreshInFlight = false;

  function setButtonsDisabled(disabled) {
    resetBackgroundButton.disabled = disabled;
    resetCountButton.disabled = disabled;
  }

  function setError(message) {
    if (!message) {
      errorEl.hidden = true;
      errorEl.textContent = "";
      return;
    }

    errorEl.hidden = false;
    errorEl.textContent = message;
  }

  function setCount(count) {
    countEl.textContent = Number.isFinite(count) ? String(count) : "0";
  }

  async function refreshStatus() {
    if (refreshInFlight) {
      return;
    }

    refreshInFlight = true;
    try {
      const response = await fetch(statusUrl, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`Status request failed (${response.status})`);
      }

      const data = await response.json();
      setCount(data.count);
      setError(data.camera_error ? `Camera error: ${data.camera_error}` : "");
    } catch (error) {
      setError(error instanceof Error ? error.message : "Unable to load status.");
    } finally {
      refreshInFlight = false;
    }
  }

  async function postAction(url) {
    if (actionInFlight) {
      return;
    }

    actionInFlight = true;
    setButtonsDisabled(true);
    try {
      const response = await fetch(url, { method: "POST" });
      if (!response.ok) {
        throw new Error(`Action failed (${response.status})`);
      }

      await refreshStatus();
    } catch (error) {
      setError(error instanceof Error ? error.message : "Action failed.");
    } finally {
      setButtonsDisabled(false);
      actionInFlight = false;
    }
  }

  resetBackgroundButton.addEventListener("click", function () {
    postAction(resetBackgroundUrl);
  });

  resetCountButton.addEventListener("click", function () {
    postAction(resetCountUrl);
  });

  refreshStatus();
  window.setInterval(refreshStatus, 1000);
})();
