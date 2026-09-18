"""AI-based bubble detection service for leak detection."""

import os
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

from camera import create_camera
from ai_detector import EdgeImpulseDetector, FallbackDetector

# Configuration
HOST = "0.0.0.0"
PORT = 5000
FRAME_SIZE = (640, 480)
JPEG_QUALITY = 85
STREAM_BOUNDARY = "frame"

# Camera setup (override with environment variables)
CAMERA_TYPE = os.getenv("CAMERA_TYPE", "pi")  # "pi" or "video"
VIDEO_PATH = os.getenv("VIDEO_PATH", None)  # Path to video file if using video camera
AI_MODEL_PATH = os.getenv("AI_MODEL_PATH", None)  # Path to Edge Impulse model

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
MODELS_DIR = BASE_DIR / "models"


class BubbleDetectionServer:
    """AI-based bubble detection server."""

    def __init__(self) -> None:
        self.camera = None
        self.detector = None

        self.cycle_count = 0
        self.previous_leak_detected = False
        self.last_status: Optional[str] = None
        self.latest_frame_jpeg: Optional[bytes] = None
        self.last_frame_time = 0.0
        self.camera_error: Optional[str] = None
        self.detector_error: Optional[str] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return

        # Initialize camera
        try:
            self.camera = create_camera(CAMERA_TYPE, VIDEO_PATH, FRAME_SIZE)
            self.camera.start()
            if not self.camera.is_running():
                self.camera_error = f"Failed to start {CAMERA_TYPE} camera"
                print(self.camera_error)
                return
        except Exception as exc:
            self.camera_error = str(exc)
            print(f"Camera initialization failed: {self.camera_error}")
            return

        # Initialize detector
        try:
            model_path = AI_MODEL_PATH or str(MODELS_DIR / "bubble_detection")
            if Path(model_path).exists():
                self.detector = EdgeImpulseDetector()
                if not self.detector.load_model(model_path):
                    print("Failed to load Edge Impulse model, falling back to classical CV")
                    self.detector = FallbackDetector()
            else:
                print(f"Model path not found: {model_path}, using fallback detector")
                self.detector = FallbackDetector()

            if not self.detector.is_ready():
                raise RuntimeError("No detector available")

        except Exception as exc:
            self.detector_error = str(exc)
            print(f"Detector initialization failed: {self.detector_error}")
            self.camera.stop()
            return

        camera_name = "Pi Camera" if CAMERA_TYPE == "pi" else f"Video ({VIDEO_PATH})"
        detector_name = (
            "Edge Impulse AI" if isinstance(self.detector, EdgeImpulseDetector) else "Classical CV"
        )

        print(f"Starting {detector_name} detection with {camera_name}...")
        print(f"Hosting leak detector at http://<ip>:{PORT}")
        time.sleep(1.0)

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.camera is not None:
            self.camera.stop()
            self.camera = None

    def reset_count(self) -> None:
        with self._lock:
            self.cycle_count = 0
            self.previous_leak_detected = False
        print("Count reset requested from web UI.")

    def get_status_snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.last_status or "INITIALIZING",
                "count": self.cycle_count,
                "frame_size": {"width": FRAME_SIZE[0], "height": FRAME_SIZE[1]},
                "last_frame_time": self.last_frame_time,
                "stream_ready": self.latest_frame_jpeg is not None,
                "camera_error": self.camera_error,
                "detector_error": self.detector_error,
                "camera_type": CAMERA_TYPE,
                "detector_type": type(self.detector).__name__ if self.detector else None,
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
        """Main capture and detection loop."""
        while self._running:
            try:
                if not self.camera or not self.camera.is_running():
                    time.sleep(0.1)
                    continue

                frame_data = self.camera.capture_frame()
                if frame_data is None:
                    time.sleep(0.05)
                    continue

                frame_bgr, frame_rgb = frame_data

                # Run AI detection
                detection_result = self.detector.detect(frame_rgb)
                leak_detected = detection_result.get("detected", False)
                confidence = detection_result.get("confidence", 0.0)

                # Draw detection boxes
                for box in detection_result.get("boxes", []):
                    x, y, w, h = box
                    cv2.rectangle(frame_bgr, (x, y), (x + w, y + h), (0, 0, 255), 2)

                # Update count on state transition
                with self._lock:
                    if self.previous_leak_detected and not leak_detected:
                        self.cycle_count += 1
                        print(f"Bubble event detected. Count: {self.cycle_count}")

                # Status text
                status_text = f"DETECTION ({confidence:.1%})" if leak_detected else "CLEAR"
                count_text = f"Count: {self.cycle_count}"

                # Draw status and count on frame
                cv2.putText(
                    frame_bgr,
                    status_text,
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 0, 255) if leak_detected else (0, 255, 0),
                    2,
                )
                text_size = cv2.getTextSize(count_text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)[0]
                text_x = FRAME_SIZE[0] - text_size[0] - 10
                cv2.putText(
                    frame_bgr,
                    count_text,
                    (text_x, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (255, 255, 255),
                    2,
                )

                # Encode and store frame
                success, buffer = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if success:
                    with self._lock:
                        self.latest_frame_jpeg = buffer.tobytes()
                        self.last_frame_time = time.time()
                        self.previous_leak_detected = leak_detected
                        self.last_status = status_text

            except Exception as exc:
                print(f"Error in capture loop: {exc}")
                time.sleep(0.1)


detector = BubbleDetectionServer()


@asynccontextmanager
async def lifespan(_: FastAPI):
    detector.start()
    try:
        yield
    finally:
        detector.stop()


app = FastAPI(title="Bubble Detection", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(TEMPLATES_DIR / "leak_detector.html")


@app.get("/status")
def status() -> JSONResponse:
    return JSONResponse(detector.get_status_snapshot())


@app.post("/reset-count")
def reset_count() -> JSONResponse:
    detector.reset_count()
    return JSONResponse({"ok": True})


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
