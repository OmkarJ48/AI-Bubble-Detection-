(function () {
  const root = document.querySelector(".leak-detector-embed");
  if (!root) {
    return;
  }

  const streamShell = document.getElementById("stream-shell");
  const streamEl = document.getElementById("stream");
  const roiBox = document.getElementById("roi-box");
  const roiHandle = document.getElementById("roi-handle");
  const resetBackgroundButton = document.getElementById("reset-background-button");
  const resetCountButton = document.getElementById("reset-count-button");

  const statusUrl = root.dataset.statusUrl || "/status";
  const streamUrl = root.dataset.streamUrl || "/stream.mjpg";
  const resetBackgroundUrl =
    root.dataset.resetBackgroundUrl || "/reset-background";
  const resetCountUrl = root.dataset.resetCountUrl || "/reset-count";
  const roiUrl = root.dataset.roiUrl || "/roi";
  const minRoiSize = 20;

  let actionInFlight = false;
  let refreshInFlight = false;
  let frameWidth = 640;
  let frameHeight = 480;
  let roi = null;
  let dragMode = null;
  let pendingStatusRefresh = false;
  let dragOffsetX = 0;
  let dragOffsetY = 0;
  let resizeStart = null;

  streamEl.src = streamUrl;

  function setButtonsDisabled(disabled) {
    resetBackgroundButton.disabled = disabled;
    if (resetCountButton) {
      resetCountButton.disabled = disabled;
    }
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
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

  function scaleX(value) {
    const viewport = getImageViewport();
    return viewport ? (value / frameWidth) * viewport.width : 0;
  }

  function scaleY(value) {
    const viewport = getImageViewport();
    return viewport ? (value / frameHeight) * viewport.height : 0;
  }

  function pxToFrameX(value) {
    const viewport = getImageViewport();
    return viewport ? Math.round((value / viewport.width) * frameWidth) : 0;
  }

  function pxToFrameY(value) {
    const viewport = getImageViewport();
    return viewport ? Math.round((value / viewport.height) * frameHeight) : 0;
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
      frameWidth = data.frame_size?.width || frameWidth;
      frameHeight = data.frame_size?.height || frameHeight;
      if (!dragMode) {
        roi = data.roi || null;
        renderRoi();
      } else {
        pendingStatusRefresh = true;
      }
    } catch (_error) {
      // Keep the UI minimal and silent.
    } finally {
      refreshInFlight = false;
    }
  }

  async function postAction(url, body) {
    if (actionInFlight) {
      return;
    }

    actionInFlight = true;
    setButtonsDisabled(true);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (response.ok) {
        await refreshStatus();
      }
    } catch (_error) {
      // Keep the UI minimal and silent.
    } finally {
      setButtonsDisabled(false);
      actionInFlight = false;
    }
  }

  function pointerPosition(event) {
    const rect = streamShell.getBoundingClientRect();
    return {
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    };
  }

  async function updateRoiOnServer(x, y, width, height) {
    const response = await fetch(roiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y, width, height }),
    });
    if (!response.ok) {
      return;
    }

    const data = await response.json();
    frameWidth = data.frame_size?.width || frameWidth;
    frameHeight = data.frame_size?.height || frameHeight;
    roi = data.roi || roi;
    renderRoi();
    await refreshStatus();
  }

  resetBackgroundButton.addEventListener("click", function () {
    postAction(resetBackgroundUrl);
  });

  if (resetCountButton) {
    resetCountButton.addEventListener("click", function () {
      postAction(resetCountUrl);
    });
  }

  document.addEventListener("keydown", function (event) {
    if (event.code !== "Space") {
      return;
    }

    event.preventDefault();
    postAction(resetBackgroundUrl);
  });

  roiBox.addEventListener("pointerdown", function (event) {
    if (event.target === roiHandle || !roi) {
      return;
    }

    const viewport = getImageViewport();
    if (!viewport) {
      return;
    }

    dragMode = "move";
    pendingStatusRefresh = false;
    roiBox.classList.add("dragging");

    const pos = pointerPosition(event);
    dragOffsetX = pos.x - (viewport.left + scaleX(roi.top_left.x));
    dragOffsetY = pos.y - (viewport.top + scaleY(roi.top_left.y));
    roiBox.setPointerCapture(event.pointerId);
  });

  roiHandle.addEventListener("pointerdown", function (event) {
    if (!roi) {
      return;
    }

    event.stopPropagation();
    dragMode = "resize";
    pendingStatusRefresh = false;
    roiBox.classList.add("resizing");
    resizeStart = {
      pointerX: event.clientX,
      pointerY: event.clientY,
      width: roi.width,
      height: roi.height,
    };
    roiBox.setPointerCapture(event.pointerId);
  });

  roiBox.addEventListener("pointermove", function (event) {
    if (!dragMode || !roi) {
      return;
    }

    const viewport = getImageViewport();
    if (!viewport) {
      return;
    }

    if (dragMode === "move") {
      const pos = pointerPosition(event);
      const leftPx = clamp(
        pos.x - dragOffsetX - viewport.left,
        0,
        viewport.width - scaleX(roi.width),
      );
      const topPx = clamp(
        pos.y - dragOffsetY - viewport.top,
        0,
        viewport.height - scaleY(roi.height),
      );
      const nextX = pxToFrameX(leftPx);
      const nextY = pxToFrameY(topPx);
      roi = {
        ...roi,
        top_left: { x: nextX, y: nextY },
        bottom_right: { x: nextX + roi.width, y: nextY + roi.height },
      };
    }

    if (dragMode === "resize" && resizeStart) {
      const deltaWidth = pxToFrameX(event.clientX - resizeStart.pointerX);
      const deltaHeight = pxToFrameY(event.clientY - resizeStart.pointerY);
      const maxWidth = frameWidth - roi.top_left.x;
      const maxHeight = frameHeight - roi.top_left.y;
      const nextWidth = clamp(resizeStart.width + deltaWidth, minRoiSize, maxWidth);
      const nextHeight = clamp(
        resizeStart.height + deltaHeight,
        minRoiSize,
        maxHeight,
      );
      roi = {
        ...roi,
        width: nextWidth,
        height: nextHeight,
        bottom_right: {
          x: roi.top_left.x + nextWidth,
          y: roi.top_left.y + nextHeight,
        },
      };
    }

    renderRoi();
  });

  async function finishDrag(event) {
    if (!dragMode || !roi) {
      return;
    }

    dragMode = null;
    roiBox.classList.remove("dragging");
    roiBox.classList.remove("resizing");
    resizeStart = null;
    try {
      roiBox.releasePointerCapture(event.pointerId);
    } catch (_error) {
      // no-op
    }

    try {
      await updateRoiOnServer(
        roi.top_left.x,
        roi.top_left.y,
        roi.width,
        roi.height,
      );
      if (pendingStatusRefresh) {
        pendingStatusRefresh = false;
        await refreshStatus();
      }
    } catch (_error) {
      // Keep the UI minimal and silent.
    }
  }

  roiBox.addEventListener("pointerup", finishDrag);
  roiBox.addEventListener("pointercancel", finishDrag);

  window.addEventListener("resize", renderRoi);
  streamEl.addEventListener("load", renderRoi);

  refreshStatus();
  window.setInterval(refreshStatus, 1000);
})();
