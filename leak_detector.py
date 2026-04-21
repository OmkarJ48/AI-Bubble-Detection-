import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import cv2
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from picamera2 import Picamera2


HOST = "0.0.0.0"
PORT = 5000
MIN_BUBBLE_AREA = 50
THRESHOLD_VALUE = 25
BLUR_SIZE = (21, 21)
FRAME_SIZE = (640, 480)
MIN_ROI_SIZE = 20
ROI_TOP_LEFT = (279, 232)
ROI_BOTTOM_RIGHT = (349, 302)
JPEG_QUALITY = 85
STREAM_BOUNDARY = "frame"


class LeakDetectorServer:
    def __init__(self) -> None:
        self.picam2: Optional[Picamera2] = None

        self.roi_x1, self.roi_y1 = ROI_TOP_LEFT
        self.roi_x2, self.roi_y2 = ROI_BOTTOM_RIGHT

        self.background_frame = None
        self.cycle_count = 0
        self.previous_leak_detected = False
        self.last_status: Optional[str] = None
        self.latest_frame_jpeg: Optional[bytes] = None
        self.last_frame_time = 0.0
        self.camera_error: Optional[str] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return

        try:
            self.picam2 = Picamera2()
            camera_config = self.picam2.create_preview_configuration(
                main={"size": FRAME_SIZE, "format": "RGB888"}
            )
            self.picam2.configure(camera_config)
            self.picam2.start()
        except Exception as exc:
            self.camera_error = str(exc)
            print(f"Camera startup failed: {self.camera_error}")
            return

        print("Warming up Pi camera... Please ensure the water is still.")
        time.sleep(2.0)
        print(f"Hosting leak detector at http://<pi-ip>:{PORT}")
        print("Background will be captured from the ROI on the first valid frame.")

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.picam2 is not None:
            self.picam2.stop()
            self.picam2 = None

    def reset_background(self) -> None:
        with self._lock:
            self.background_frame = None
            self.previous_leak_detected = False
        print("Background reset requested from web UI.")

    def reset_count(self) -> None:
        with self._lock:
            self.cycle_count = 0
            self.previous_leak_detected = False
        print("Count reset requested from web UI.")

    def update_roi(self, x: int, y: int, width: int | None = None, height: int | None = None) -> dict:
        with self._lock:
            current_width = self.roi_x2 - self.roi_x1
            current_height = self.roi_y2 - self.roi_y1

        next_width = current_width if width is None else int(width)
        next_height = current_height if height is None else int(height)
        next_width = max(MIN_ROI_SIZE, min(next_width, FRAME_SIZE[0]))
        next_height = max(MIN_ROI_SIZE, min(next_height, FRAME_SIZE[1]))

        max_x = FRAME_SIZE[0] - next_width
        max_y = FRAME_SIZE[1] - next_height
        clamped_x = max(0, min(int(x), max_x))
        clamped_y = max(0, min(int(y), max_y))

        with self._lock:
            self.roi_x1 = clamped_x
            self.roi_y1 = clamped_y
            self.roi_x2 = clamped_x + next_width
            self.roi_y2 = clamped_y + next_height
            self.background_frame = None
            self.previous_leak_detected = False

        print(
            f"ROI updated to ({self.roi_x1}, {self.roi_y1}, "
            f"{self.roi_x2 - self.roi_x1}x{self.roi_y2 - self.roi_y1}) and background reset."
        )
        return self.get_status_snapshot()

    def get_status_snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.last_status or "WARMING UP",
                "count": self.cycle_count,
                "roi": {
                    "top_left": {"x": self.roi_x1, "y": self.roi_y1},
                    "bottom_right": {"x": self.roi_x2, "y": self.roi_y2},
                    "width": self.roi_x2 - self.roi_x1,
                    "height": self.roi_y2 - self.roi_y1,
                },
                "frame_size": {"width": FRAME_SIZE[0], "height": FRAME_SIZE[1]},
                "last_frame_time": self.last_frame_time,
                "stream_ready": self.latest_frame_jpeg is not None,
                "camera_error": self.camera_error,
            }

    def stream_generator(self):
        while True:
            with self._lock:
                frame = self.latest_frame_jpeg

            if frame is None:
                time.sleep(0.1)
                continue

            yield (
                b"--" + STREAM_BOUNDARY.encode("ascii") + b"\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
            time.sleep(0.05)

    def _capture_loop(self) -> None:
        while self._running:
            if self.picam2 is None:
                time.sleep(0.1)
                continue
            frame_rgb = self.picam2.capture_array()
            if frame_rgb is None:
                print("Failed to grab frame from Pi camera.")
                time.sleep(0.1)
                continue

            with self._lock:
                roi_x1, roi_y1 = self.roi_x1, self.roi_y1
                roi_x2, roi_y2 = self.roi_x2, self.roi_y2
                if self.background_frame is None:
                    background_frame = None
                else:
                    background_frame = self.background_frame.copy()
                previous_leak_detected = self.previous_leak_detected
                cycle_count = self.cycle_count

            frame = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, BLUR_SIZE, 0)
            roi_gray = gray[roi_y1:roi_y2, roi_x1:roi_x2]

            with self._lock:
                if self.background_frame is None:
                    self.background_frame = roi_gray.copy()
                    self.last_status = "STATUS: CLEAR"
                    print("Background captured! Monitoring for leaks.")
                    self._store_encoded_frame(frame)
                    continue

            frame_delta = cv2.absdiff(background_frame, roi_gray)
            thresh = cv2.threshold(frame_delta, THRESHOLD_VALUE, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(
                thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            leak_detected = False
            for contour in contours:
                if cv2.contourArea(contour) < MIN_BUBBLE_AREA:
                    continue

                leak_detected = True
                x, y, w, h = cv2.boundingRect(contour)
                cv2.rectangle(
                    frame,
                    (x + roi_x1, y + roi_y1),
                    (x + roi_x1 + w, y + roi_y1 + h),
                    (0, 0, 255),
                    2,
                )

            status_text = "LEAK DETECTED" if leak_detected else "STATUS: CLEAR"

            if previous_leak_detected and not leak_detected:
                cycle_count += 1
                print(f"COUNT: {cycle_count}")

            count_text = f"COUNT: {cycle_count}"
            text_size, _ = cv2.getTextSize(count_text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
            text_x = FRAME_SIZE[0] - text_size[0] - 10
            cv2.putText(
                frame,
                count_text,
                (text_x, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 255, 255),
                2,
            )

            if status_text != self.last_status:
                print(status_text)

            with self._lock:
                self.cycle_count = cycle_count
                self.previous_leak_detected = leak_detected
                self.last_status = status_text
                self._store_encoded_frame(frame)

    def _store_encoded_frame(self, frame) -> None:
        success, buffer = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
        )
        if success:
            self.latest_frame_jpeg = buffer.tobytes()
            self.last_frame_time = time.time()


detector = LeakDetectorServer()


@asynccontextmanager
async def lifespan(_: FastAPI):
    detector.start()
    try:
        yield
    finally:
        detector.stop()


app = FastAPI(title="Leak Detector", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Leak Detector</title>
  <style>
    :root {{
      --bg: #0b1220;
      --panel: #131c2f;
      --text: #eaf1ff;
      --muted: #9fb0d1;
      --accent: #4dd0e1;
      --danger: #ff6b6b;
      --ok: #4ade80;
    }}
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      background: radial-gradient(circle at top, #182845 0%, var(--bg) 60%);
      color: var(--text);
    }}
    .wrap {{
      max-width: 980px;
      margin: 0 auto;
      padding: 24px;
    }}
    .panel {{
      background: rgba(19, 28, 47, 0.92);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 28px;
    }}
    .meta {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin: 0 0 18px;
    }}
    .chip {{
      background: #1b2740;
      color: var(--muted);
      padding: 10px 12px;
      border-radius: 999px;
      font-size: 14px;
    }}
    .status {{
      color: var(--accent);
      font-weight: 700;
    }}
    .stream-shell {{
      position: relative;
      width: 100%;
      overflow: hidden;
      border-radius: 14px;
      border: 1px solid rgba(255, 255, 255, 0.08);
      background: #000;
      touch-action: none;
      user-select: none;
      line-height: 0;
      aspect-ratio: {FRAME_SIZE[0]} / {FRAME_SIZE[1]};
    }}
    .stream {{
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
    }}
    .roi-box {{
      position: absolute;
      border: 3px solid #000;
      box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.08);
      border-radius: 10px;
      cursor: grab;
      touch-action: none;
      box-sizing: border-box;
    }}
    .roi-box.dragging {{
      cursor: grabbing;
    }}
    .roi-box.resizing {{
      cursor: nwse-resize;
    }}
    .roi-handle {{
      position: absolute;
      right: -8px;
      bottom: -8px;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: #000;
      border: 2px solid #fff;
      cursor: nwse-resize;
      touch-action: none;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      margin-top: 16px;
      flex-wrap: wrap;
    }}
    button {{
      border: 0;
      border-radius: 12px;
      padding: 12px 16px;
      font-size: 16px;
      cursor: pointer;
      background: var(--accent);
      color: #08111d;
      font-weight: 700;
    }}
    button.secondary {{
      background: #d6e2ff;
    }}
    .small {{
      color: var(--muted);
      font-size: 14px;
      margin-top: 14px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <h1>Leak Detector Live View</h1>
      <div class="meta">
        <div class="chip">Status: <span id="status" class="status">Starting...</span></div>
        <div class="chip">Count: <span id="count">0</span></div>
      </div>
      <div id="camera-error" class="small" style="color: var(--danger); display: none;"></div>
      <div id="stream-shell" class="stream-shell">
        <img id="stream" class="stream" src="/stream.mjpg" alt="Leak detector live stream">
        <div id="roi-box" class="roi-box" aria-label="ROI">
          <div id="roi-handle" class="roi-handle" aria-hidden="true"></div>
        </div>
      </div>
      <div class="actions">
        <button id="reset-button" type="button">Reset Background</button>
        <button id="reset-count-button" class="secondary" type="button">Reset Count</button>
      </div>
    </div>
  </div>
  <script>
    const streamShell = document.getElementById('stream-shell');
    const streamImage = document.getElementById('stream');
    const roiBox = document.getElementById('roi-box');
    const roiHandle = document.getElementById('roi-handle');
    const roiLabel = document.getElementById('roi-label');
    let frameWidth = {FRAME_SIZE[0]};
    let frameHeight = {FRAME_SIZE[1]};
    let roi = null;
    let dragMode = null;
    let dragOffsetX = 0;
    let dragOffsetY = 0;
    let resizeStart = null;
    let pendingStatusRefresh = false;

    function getImageViewport() {{
      const shellWidth = streamShell.clientWidth;
      const shellHeight = streamShell.clientHeight;
      if (!shellWidth || !shellHeight) {{
        return null;
      }}

      const frameAspect = frameWidth / frameHeight;
      const shellAspect = shellWidth / shellHeight;
      let width;
      let height;

      if (shellAspect > frameAspect) {{
        height = shellHeight;
        width = height * frameAspect;
      }} else {{
        width = shellWidth;
        height = width / frameAspect;
      }}

      return {{
        left: (shellWidth - width) / 2,
        top: (shellHeight - height) / 2,
        width,
        height
      }};
    }}

    function scaleX(value) {{
      const viewport = getImageViewport();
      return viewport ? (value / frameWidth) * viewport.width : 0;
    }}

    function scaleY(value) {{
      const viewport = getImageViewport();
      return viewport ? (value / frameHeight) * viewport.height : 0;
    }}

    function clamp(value, min, max) {{
      return Math.min(Math.max(value, min), max);
    }}

    function renderRoi() {{
      const viewport = getImageViewport();
      if (!roi || !viewport) {{
        return;
      }}
      roiBox.style.left = `${{viewport.left + scaleX(roi.top_left.x)}}px`;
      roiBox.style.top = `${{viewport.top + scaleY(roi.top_left.y)}}px`;
      roiBox.style.width = `${{scaleX(roi.width)}}px`;
      roiBox.style.height = `${{scaleY(roi.height)}}px`;
      roiLabel.textContent = `${{roi.top_left.x}},${{roi.top_left.y}} (${{roi.width}} x ${{roi.height}})`;
    }}

    async function updateRoiOnServer(x, y, width, height) {{
      const response = await fetch('/roi', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ x, y, width, height }})
      }});
      const data = await response.json();
      roi = data.roi;
      frameWidth = data.frame_size.width;
      frameHeight = data.frame_size.height;
      renderRoi();
      await refreshStatus();
    }}

    async function refreshStatus() {{
      const response = await fetch('/status');
      const data = await response.json();
      document.getElementById('status').textContent = data.status;
      document.getElementById('status').style.color = data.status === 'LEAK DETECTED' ? '{'#ff6b6b'}' : '{'#4ade80'}';
      document.getElementById('count').textContent = data.count;
      frameWidth = data.frame_size.width;
      frameHeight = data.frame_size.height;
      if (!dragMode) {{
        roi = data.roi;
        renderRoi();
      }} else {{
        pendingStatusRefresh = true;
      }}
      const errorEl = document.getElementById('camera-error');
      if (data.camera_error) {{
        errorEl.style.display = 'block';
        errorEl.textContent = 'Camera error: ' + data.camera_error;
      }} else {{
        errorEl.style.display = 'none';
        errorEl.textContent = '';
      }}
    }}

    document.getElementById('reset-button').addEventListener('click', async () => {{
      await fetch('/reset-background', {{ method: 'POST' }});
      refreshStatus();
    }});

    document.getElementById('reset-count-button').addEventListener('click', async () => {{
      await fetch('/reset-count', {{ method: 'POST' }});
      refreshStatus();
    }});

    function pointerPosition(event) {{
      const rect = streamShell.getBoundingClientRect();
      return {{
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
        rect
      }};
    }}

    function pxToFrameX(value) {{
      const viewport = getImageViewport();
      return viewport ? Math.round((value / viewport.width) * frameWidth) : 0;
    }}

    function pxToFrameY(value) {{
      const viewport = getImageViewport();
      return viewport ? Math.round((value / viewport.height) * frameHeight) : 0;
    }}

    roiBox.addEventListener('pointerdown', (event) => {{
      if (event.target === roiHandle) return;
      if (!roi) return;
      dragMode = 'move';
      pendingStatusRefresh = false;
      roiBox.classList.add('dragging');
      const pos = pointerPosition(event);
      const viewport = getImageViewport();
      if (!viewport) return;
      dragOffsetX = pos.x - (viewport.left + scaleX(roi.top_left.x));
      dragOffsetY = pos.y - (viewport.top + scaleY(roi.top_left.y));
      roiBox.setPointerCapture(event.pointerId);
    }});

    roiHandle.addEventListener('pointerdown', (event) => {{
      event.stopPropagation();
      if (!roi) return;
      dragMode = 'resize';
      pendingStatusRefresh = false;
      roiBox.classList.add('resizing');
      resizeStart = {{
        pointerX: event.clientX,
        pointerY: event.clientY,
        width: roi.width,
        height: roi.height
      }};
      roiBox.setPointerCapture(event.pointerId);
    }});

    roiBox.addEventListener('pointermove', (event) => {{
      if (!dragMode || !roi) return;
      const pos = pointerPosition(event);
      const viewport = getImageViewport();
      if (!viewport) return;
      if (dragMode === 'move') {{
        const leftPx = clamp(
          pos.x - dragOffsetX - viewport.left,
          0,
          viewport.width - scaleX(roi.width)
        );
        const topPx = clamp(
          pos.y - dragOffsetY - viewport.top,
          0,
          viewport.height - scaleY(roi.height)
        );
        const nextX = pxToFrameX(leftPx);
        const nextY = pxToFrameY(topPx);
        roi = {{
          ...roi,
          top_left: {{ x: nextX, y: nextY }},
          bottom_right: {{ x: nextX + roi.width, y: nextY + roi.height }}
        }};
      }} else if (dragMode === 'resize' && resizeStart) {{
        const deltaWidth = pxToFrameX(event.clientX - resizeStart.pointerX);
        const deltaHeight = pxToFrameY(event.clientY - resizeStart.pointerY);
        const maxWidth = frameWidth - roi.top_left.x;
        const maxHeight = frameHeight - roi.top_left.y;
        const nextWidth = clamp(resizeStart.width + deltaWidth, {MIN_ROI_SIZE}, maxWidth);
        const nextHeight = clamp(resizeStart.height + deltaHeight, {MIN_ROI_SIZE}, maxHeight);
        roi = {{
          ...roi,
          width: nextWidth,
          height: nextHeight,
          bottom_right: {{ x: roi.top_left.x + nextWidth, y: roi.top_left.y + nextHeight }}
        }};
      }}
      renderRoi();
    }});

    async function finishDrag(event) {{
      if (!dragMode || !roi) return;
      const mode = dragMode;
      dragMode = null;
      roiBox.classList.remove('dragging');
      roiBox.classList.remove('resizing');
      resizeStart = null;
      try {{
        roiBox.releasePointerCapture(event.pointerId);
      }} catch (error) {{}}
      if (mode === 'move' || mode === 'resize') {{
        await updateRoiOnServer(roi.top_left.x, roi.top_left.y, roi.width, roi.height);
      }}
      if (pendingStatusRefresh) {{
        pendingStatusRefresh = false;
        await refreshStatus();
      }}
    }}

    roiBox.addEventListener('pointerup', finishDrag);
    roiBox.addEventListener('pointercancel', finishDrag);
    document.addEventListener('keydown', async (event) => {{
      if (event.code !== 'Space') return;
      if (event.target && ['INPUT', 'TEXTAREA'].includes(event.target.tagName)) return;
      event.preventDefault();
      await fetch('/reset-background', {{ method: 'POST' }});
      refreshStatus();
    }});
    window.addEventListener('resize', renderRoi);
    streamImage.addEventListener('load', renderRoi);

    refreshStatus();
    setInterval(refreshStatus, 1000);
  </script>
</body>
</html>
"""


@app.get("/status")
def status() -> JSONResponse:
    return JSONResponse(detector.get_status_snapshot())


@app.post("/reset-background")
def reset_background() -> JSONResponse:
    detector.reset_background()
    return JSONResponse({"ok": True})


@app.post("/reset-count")
def reset_count() -> JSONResponse:
    detector.reset_count()
    return JSONResponse({"ok": True})


@app.post("/roi")
def update_roi(payload: dict) -> JSONResponse:
    x = payload.get("x", ROI_TOP_LEFT[0])
    y = payload.get("y", ROI_TOP_LEFT[1])
    width = payload.get("width")
    height = payload.get("height")
    return JSONResponse(detector.update_roi(x, y, width, height))


@app.get("/stream.mjpg")
def stream() -> StreamingResponse:
    return StreamingResponse(
        detector.stream_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={STREAM_BOUNDARY}",
    )


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
