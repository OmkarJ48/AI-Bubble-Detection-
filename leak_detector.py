import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import cv2
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


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
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(TEMPLATES_DIR / "leak_detector.html")


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
