(function () {
  const root = document.querySelector(".leak-detector-embed");
  if (!root) {
    return;
  }

  const streamShell = document.getElementById("stream-shell");
  const streamEl = document.getElementById("stream");
  const roiBox = document.getElementById("roi-box");
  const countEl = document.getElementById("count");
  const errorEl = document.getElementById("camera-error");
  const resetBackgroundButton = document.getElementById("reset-background-button");
  const resetCountButton = document.getElementById("reset-count-button");

  const statusUrl = root.dataset.statusUrl || "/status";
  const streamUrl = root.dataset.streamUrl || "/stream.mjpg";
  const resetBackgroundUrl =
    root.dataset.resetBackgroundUrl || "/reset-background";
  const resetCountUrl = root.dataset.resetCountUrl || "/reset-count";

  let actionInFlight = false;
  let refreshInFlight = false;
  let frameWidth = 640;
  let frameHeight = 480;
  let roi = null;

  streamEl.src = streamUrl;

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

  function getImageViewport() {
    const shellWidth = streamShell.clientWidth;
    const shellHeight = streamShell.clientHeight;
    if (!shellWidth || !shellHeight || !frameWidth || !frameHeight) {
      return null;
    }

    const frameAspect = frameWidth / frameHeight;
    const shellAspect = shellWidth / shellHeight;

    let width;
    let height;

    if (shellAspect > frameAspect) {
      height = shellHeight;
      width = height * frameAspect;
    } else {
      width = shellWidth;
      height = width / frameAspect;
    }

    return {
      left: (shellWidth - width) / 2,
      top: (shellHeight - height) / 2,
      width,
      height,
    };
  }

  function renderRoi() {
    const viewport = getImageViewport();
    if (!roi || !viewport) {
      roiBox.hidden = true;
      return;
    }

    roiBox.hidden = false;
    roiBox.style.left =
      `${viewport.left + (roi.top_left.x / frameWidth) * viewport.width}px`;
    roiBox.style.top =
      `${viewport.top + (roi.top_left.y / frameHeight) * viewport.height}px`;
    roiBox.style.width = `${(roi.width / frameWidth) * viewport.width}px`;
    roiBox.style.height = `${(roi.height / frameHeight) * viewport.height}px`;
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
      frameWidth = data.frame_size?.width || frameWidth;
      frameHeight = data.frame_size?.height || frameHeight;
      roi = data.roi || null;
      renderRoi();
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

  window.addEventListener("resize", renderRoi);
  streamEl.addEventListener("load", renderRoi);

  refreshStatus();
  window.setInterval(refreshStatus, 1000);
})();
