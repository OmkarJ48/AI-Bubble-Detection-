#!/usr/bin/env python3
"""
Raspberry Pi 5 Camera Livestream Server.

Precision-first, single-track behavior:
- one candidate max, one active bubble max
- strict pipe-lock + spawn-band start gating
- direction-aware matching to avoid jitter jumps
- count only after lock + upward travel + count-band hit + disappearance
"""

import io
import logging
import threading
import time
from datetime import datetime

import cv2
from flask import Flask, Response, jsonify, render_template_string

from bubble_tracker import (
    PipeGeometry,
    PrecisionBubbleTracker,
    TrackerConfig,
    apply_profile_mapping,
    detect_bubbles,
    estimate_pipe_center_x,
    load_profile_data,
    resolve_profile_path,
)

# =========================
# GLOBAL STATE / TUNING
# =========================
output = None
camera = None
streaming = False
recording = False
recording_output = None
Picamera2 = None

tracker = PrecisionBubbleTracker(TrackerConfig())
last_detection_snapshot = {
    "raw_detections": [],
    "filtered_detections": [],
    "start_candidates": [],
    "state": "idle",
    "started_id": None,
    "counted": False,
    "ended_event": None,
}

TARGET_FPS = 20

# ROI
ROI_X1 = 250
ROI_Y1 = 140
ROI_X2 = 760
ROI_Y2 = 500

# Pipe geometry
PIPE_CENTER_X_BIAS = -50
PIPE_WIDTH_RATIO = 0.35
PIPE_LOCK_WIDTH_RATIO = 0.18
PIPE_TOP_RATIO = 0.25
PIPE_BOTTOM_RATIO = 0.75
PIPE_EXIT_RATIO = 0.55

# Tracking logic
LOCK_AFTER_FRAMES = 2
LOST_AFTER_FRAMES = 4
MIN_UPWARD_TRAVEL = 30
MAX_MATCH_DISTANCE = 70
DOWNWARD_TOLERANCE = 10
MAX_LATERAL_SHIFT = 35
MAX_STEP_DISTANCE = 80

# Detection logic
DETECT_INTERVAL = 0.08
MIN_RADIUS = 4
MAX_RADIUS = 20

# Acquisition / band logic
SPAWN_BAND_HALF = 22
MIN_START_BELOW_EXIT = 18
CANDIDATE_CONFIRM_FRAMES = 2
CANDIDATE_MATCH_DISTANCE = 50
CANDIDATE_LOST_AFTER_FRAMES = 1
COUNT_BAND_HALF = 12
COUNT_BAND_OFFSET = -30

# Auto-center (optional, profile-driven)
AUTO_CENTER_ENABLED = False
AUTO_CENTER_SMOOTHING = 0.85
CENTER_SEARCH_WIDTH_RATIO = 0.35
AUTO_CENTER_MAX_OFFSET_PX = 120

# Debug
DEBUG_DRAW = True

PROFILE_PATH = None
STREAM_NAME = "livestream"
dynamic_pipe_center_x = None


SHARED_PROFILE_FIELDS = {
    "pipe_center_x_bias": "PIPE_CENTER_X_BIAS",
    "pipe_width_ratio": "PIPE_WIDTH_RATIO",
    "pipe_lock_width_ratio": "PIPE_LOCK_WIDTH_RATIO",
    "pipe_top_ratio": "PIPE_TOP_RATIO",
    "pipe_bottom_ratio": "PIPE_BOTTOM_RATIO",
    "pipe_exit_ratio": "PIPE_EXIT_RATIO",
    "count_band_half": "COUNT_BAND_HALF",
}

LIVESTREAM_PROFILE_FIELDS = {
    "target_fps": "TARGET_FPS",
    "roi_x1": "ROI_X1",
    "roi_y1": "ROI_Y1",
    "roi_x2": "ROI_X2",
    "roi_y2": "ROI_Y2",
    "lock_after_frames": "LOCK_AFTER_FRAMES",
    "lost_after_frames": "LOST_AFTER_FRAMES",
    "min_upward_travel": "MIN_UPWARD_TRAVEL",
    "detect_interval": "DETECT_INTERVAL",
    "min_radius": "MIN_RADIUS",
    "max_radius": "MAX_RADIUS",
    "max_match_distance": "MAX_MATCH_DISTANCE",
    "downward_tolerance": "DOWNWARD_TOLERANCE",
    "max_lateral_shift": "MAX_LATERAL_SHIFT",
    "max_step_distance": "MAX_STEP_DISTANCE",
    "spawn_band_half": "SPAWN_BAND_HALF",
    "min_start_below_exit": "MIN_START_BELOW_EXIT",
    "candidate_confirm_frames": "CANDIDATE_CONFIRM_FRAMES",
    "candidate_match_distance": "CANDIDATE_MATCH_DISTANCE",
    "candidate_lost_after_frames": "CANDIDATE_LOST_AFTER_FRAMES",
    "count_band_half": "COUNT_BAND_HALF",
    "count_band_offset": "COUNT_BAND_OFFSET",
    "auto_center_enabled": "AUTO_CENTER_ENABLED",
    "auto_center_smoothing": "AUTO_CENTER_SMOOTHING",
    "center_search_width_ratio": "CENTER_SEARCH_WIDTH_RATIO",
    "auto_center_max_offset_px": "AUTO_CENTER_MAX_OFFSET_PX",
    "debug_draw": "DEBUG_DRAW",
}


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Raspberry Pi 5 Camera Livestream</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }

        .container {
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            max-width: 1200px;
            width: 100%;
            padding: 30px;
        }

        h1 {
            text-align: center;
            color: #333;
            margin-bottom: 10px;
            font-size: 2em;
        }

        .status {
            text-align: center;
            color: #27ae60;
            font-weight: bold;
            margin-bottom: 20px;
        }

        .video-container {
            position: relative;
            width: 100%;
            background: #000;
            border-radius: 8px;
            overflow: hidden;
            margin-bottom: 20px;
            aspect-ratio: 16 / 9;
        }

        #stream {
            width: 100%;
            height: 100%;
            display: block;
            background: #000;
        }

        .controls {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 10px;
            margin-bottom: 20px;
        }

        button {
            padding: 12px 20px;
            font-size: 1em;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.3s ease;
            text-transform: uppercase;
        }

        button:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.2);
        }

        button:disabled {
            opacity: 0.6;
            cursor: not-allowed;
        }

        .btn-capture {
            background: #3498db;
            color: white;
        }

        .btn-capture:hover:not(:disabled) {
            background: #2980b9;
        }

        .info {
            background: #e8f4f8;
            border-left: 4px solid #3498db;
            padding: 12px;
            border-radius: 4px;
            margin-top: 10px;
            font-size: 0.9em;
            color: #555;
            display: none;
        }

        .error {
            background: #fadbd8;
            border-left: 4px solid #e74c3c;
            padding: 12px;
            border-radius: 4px;
            margin-top: 10px;
            font-size: 0.9em;
            color: #c0392b;
            display: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Raspberry Pi 5 Livestream</h1>
        <div class="status">LIVE</div>

        <div class="video-container">
            <img id="stream" src="/video_feed" alt="Camera Stream">
        </div>

        <div class="controls">
            <button class="btn-capture" onclick="captureImage()">Capture</button>
        </div>

        <div id="message" class="info"></div>
        <div id="error" class="error"></div>
    </div>

    <script>
        function captureImage() {
            const button = event.target;
            button.disabled = true;

            fetch('/capture')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        showMessage('Image captured: ' + data.filename);
                    } else {
                        showError('Failed: ' + data.error);
                    }
                })
                .catch(error => {
                    showError('Capture error: ' + error);
                })
                .finally(() => {
                    button.disabled = false;
                });
        }

        function showMessage(msg) {
            const msgDiv = document.getElementById('message');
            msgDiv.textContent = msg;
            msgDiv.style.display = 'block';
            setTimeout(() => {
                msgDiv.style.display = 'none';
            }, 3000);
        }

        function showError(msg) {
            const errDiv = document.getElementById('error');
            errDiv.textContent = msg;
            errDiv.style.display = 'block';
        }

        window.addEventListener('load', () => {
            const img = document.getElementById('stream');
            img.src = '/video_feed';

            img.onerror = () => {
                showError("Stream disconnected");
                setTimeout(() => {
                    img.src = '/video_feed?' + new Date().getTime();
                }, 1000);
            };
        });
    </script>
</body>
</html>
"""


def build_tracker() -> PrecisionBubbleTracker:
    return PrecisionBubbleTracker(
        TrackerConfig(
            lock_after_frames=int(LOCK_AFTER_FRAMES),
            lost_after_frames=int(LOST_AFTER_FRAMES),
            min_upward_travel=int(MIN_UPWARD_TRAVEL),
            max_match_distance=int(MAX_MATCH_DISTANCE),
            downward_tolerance=int(DOWNWARD_TOLERANCE),
            max_lateral_shift=int(MAX_LATERAL_SHIFT),
            max_step_distance=int(MAX_STEP_DISTANCE),
            min_radius=int(MIN_RADIUS),
            max_radius=int(MAX_RADIUS),
            candidate_confirm_frames=int(CANDIDATE_CONFIRM_FRAMES),
            candidate_match_distance=int(CANDIDATE_MATCH_DISTANCE),
            candidate_lost_after_frames=int(CANDIDATE_LOST_AFTER_FRAMES),
            min_start_below_exit=int(MIN_START_BELOW_EXIT),
        )
    )


def apply_livestream_profile(profile_path):
    global PROFILE_PATH
    PROFILE_PATH = profile_path
    profile_data = load_profile_data(PROFILE_PATH)
    apply_profile_mapping(profile_data.get("shared", {}), SHARED_PROFILE_FIELDS, globals())
    apply_profile_mapping(profile_data.get("livestream", {}), LIVESTREAM_PROFILE_FIELDS, globals())


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/video_feed")
def video_feed():
    def generate():
        global output, streaming
        if output is None:
            yield b"Camera not ready"
            return

        while streaming:
            with output.condition:
                output.condition.wait()
                frame = output.frame

            if frame is None:
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/capture")
def capture():
    request_obj = None
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"image_{timestamp}.jpg"
        request_obj = camera.capture_request()
        request_obj.save("main", filename)
        logger.info("Image captured: %s", filename)
        return jsonify({"success": True, "filename": filename})
    except Exception as e:
        logger.exception("Error capturing image: %s", e)
        return jsonify({"success": False, "error": str(e)})
    finally:
        if request_obj is not None:
            try:
                request_obj.release()
            except Exception:
                pass


def frame_capture_thread():
    global dynamic_pipe_center_x, last_detection_snapshot, tracker

    frame_count = 0
    frame_interval = 1.0 / TARGET_FPS
    last_detect_time = 0.0

    logger.info("Frame capture thread started")

    while streaming:
        start_time = time.time()
        try:
            frame_count += 1
            request = None
            try:
                request = camera.capture_request()
                frame = request.make_array("main")
            finally:
                if request is not None:
                    request.release()

            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            h, w = frame.shape[:2]

            if frame_count < 10:
                continue

            x1, y1, x2, y2 = ROI_X1, ROI_Y1, ROI_X2, ROI_Y2
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(w, x2)
            y2 = min(h, y2)
            if x2 <= x1 or y2 <= y1:
                continue

            roi = frame[y1:y2, x1:x2]
            if roi.size == 0:
                continue

            roi_h, roi_w = roi.shape[:2]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (7, 7), 0)
            small = cv2.resize(blur, (64, 48), interpolation=cv2.INTER_AREA)
            small = cv2.equalizeHist(small)

            fallback_local_center = (roi_w // 2) + int(PIPE_CENTER_X_BIAS)
            if AUTO_CENTER_ENABLED:
                dynamic_pipe_center_x = estimate_pipe_center_x(
                    gray_roi=gray,
                    fallback_center_x=fallback_local_center,
                    previous_center_x=dynamic_pipe_center_x,
                    search_width_ratio=float(CENTER_SEARCH_WIDTH_RATIO),
                    smoothing=float(AUTO_CENTER_SMOOTHING),
                    max_offset_px=AUTO_CENTER_MAX_OFFSET_PX,
                )
                local_pipe_center_x = dynamic_pipe_center_x
            else:
                local_pipe_center_x = fallback_local_center

            pipe_center_x = x1 + local_pipe_center_x
            pipe_width = int(roi_w * PIPE_WIDTH_RATIO)
            pipe_lock_width = int(roi_w * PIPE_LOCK_WIDTH_RATIO)
            pipe_top = y1 + int(roi_h * PIPE_TOP_RATIO)
            pipe_bottom = y1 + int(roi_h * PIPE_BOTTOM_RATIO)
            pipe_exit_y = y1 + int(roi_h * PIPE_EXIT_RATIO)
            spawn_y1 = pipe_exit_y - int(SPAWN_BAND_HALF)
            spawn_y2 = pipe_exit_y + int(SPAWN_BAND_HALF)
            count_y1 = pipe_exit_y - int(COUNT_BAND_HALF) + int(COUNT_BAND_OFFSET)
            count_y2 = pipe_exit_y + int(COUNT_BAND_HALF) + int(COUNT_BAND_OFFSET)

            geometry = PipeGeometry(
                pipe_center_x=pipe_center_x,
                pipe_lock_width=pipe_lock_width,
                pipe_top=pipe_top,
                pipe_bottom=pipe_bottom,
                pipe_exit_y=pipe_exit_y,
                spawn_y1=spawn_y1,
                spawn_y2=spawn_y2,
                count_y1=count_y1,
                count_y2=count_y2,
            )

            now = time.time()
            if now - last_detect_time > DETECT_INTERVAL:
                raw_detections = detect_bubbles(
                    small_gray=small,
                    scale_x=roi_w / 64,
                    scale_y=roi_h / 48,
                    x_offset=x1,
                    y_offset=y1,
                )
                last_detect_time = now
                last_detection_snapshot = tracker.step(raw_detections, now, geometry)
                if last_detection_snapshot["started_id"] is not None:
                    logger.info("START bubble %s", last_detection_snapshot["started_id"])
                if last_detection_snapshot["counted"]:
                    logger.info("COUNT total=%s", tracker.bubble_count_total)
                if last_detection_snapshot["ended_event"] is not None:
                    logger.info(
                        "ENDED bubble %s counted=%s",
                        last_detection_snapshot["ended_event"]["id"],
                        last_detection_snapshot["ended_event"]["counted"],
                    )

            if DEBUG_DRAW:
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
                cv2.putText(frame, "ROI", (x1, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
                cv2.line(frame, (pipe_center_x, y1), (pipe_center_x, y2), (255, 255, 0), 2)
                cv2.rectangle(
                    frame,
                    (pipe_center_x - pipe_width, pipe_top),
                    (pipe_center_x + pipe_width, pipe_bottom),
                    (255, 0, 255),
                    2,
                )
                cv2.rectangle(
                    frame,
                    (pipe_center_x - pipe_lock_width, pipe_top),
                    (pipe_center_x + pipe_lock_width, pipe_bottom),
                    (0, 255, 255),
                    1,
                )
                cv2.rectangle(frame, (x1, spawn_y1), (x2, spawn_y2), (0, 165, 255), 1)
                cv2.putText(frame, "SPAWN BAND", (x1, spawn_y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)
                cv2.rectangle(frame, (x1, count_y1), (x2, count_y2), (0, 0, 255), 1)
                cv2.putText(frame, "COUNT BAND", (x1, count_y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

                for (cx, cy, r) in last_detection_snapshot["filtered_detections"]:
                    cv2.circle(frame, (cx, cy), max(8, int(r * 2.0)), (0, 255, 255), 2)

            if tracker.active_bubble is not None:
                bubble = tracker.active_bubble
                cx = bubble["cx"]
                cy = bubble["cy"]
                r = max(10, int(bubble["r"] * 2.5))
                cv2.circle(frame, (cx, cy), r, (0, 255, 0), 2)
                cv2.circle(frame, (cx, cy), 4, (0, 255, 0), -1)
                cv2.putText(
                    frame,
                    f"ID {bubble['id']} TRACKING",
                    (cx + 10, cy),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    1,
                )
            elif tracker.candidate_bubble is not None and DEBUG_DRAW:
                cand = tracker.candidate_bubble
                cv2.circle(frame, (cand["cx"], cand["cy"]), max(8, int(cand["r"] * 2.0)), (255, 165, 0), 1)
                cv2.putText(
                    frame,
                    f"CAND {cand['seen_frames']}/{CANDIDATE_CONFIRM_FRAMES}",
                    (cand["cx"] + 10, cand["cy"] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (255, 165, 0),
                    1,
                )

            fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))
            overlay = frame.copy()
            ox1 = frame.shape[1] - 320
            oy1 = 10
            ox2 = frame.shape[1] - 10
            oy2 = oy1 + 165
            cv2.rectangle(overlay, (ox1, oy1), (ox2, oy2), (0, 0, 0), -1)
            frame = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0)

            y = oy1 + 22
            for text in [
                f"FPS: {fps:.1f}",
                f"Frame: {frame_count}",
                f"State: {tracker.state}",
                f"Tracking: {1 if tracker.active_bubble is not None else 0}",
                f"Candidate: {1 if tracker.candidate_bubble is not None else 0}",
                f"Count: {tracker.bubble_count_total}",
                f"Auto center: {'ON' if AUTO_CENTER_ENABLED else 'OFF'}",
            ]:
                cv2.putText(frame, text, (ox1 + 10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
                y += 20

            ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 45])
            if ok:
                with output.condition:
                    output.frame = jpeg.tobytes()
                    output.condition.notify_all()

            elapsed = time.time() - start_time
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)

        except Exception:
            logger.exception("Frame capture thread error")
            time.sleep(0.1)


def initialize_camera(resolution=(960, 540), fps=30):
    global camera, streaming, output, Picamera2, tracker, last_detection_snapshot, dynamic_pipe_center_x
    try:
        if Picamera2 is None:
            try:
                from picamera2 import Picamera2 as picamera2_class
            except ImportError:
                logger.error("Error: picamera2 is not installed")
                logger.error("Install with: sudo apt install python3-picamera2")
                return False
            Picamera2 = picamera2_class

        tracker = build_tracker()
        last_detection_snapshot = {
            "raw_detections": [],
            "filtered_detections": [],
            "start_candidates": [],
            "state": "idle",
            "started_id": None,
            "counted": False,
            "ended_event": None,
        }
        dynamic_pipe_center_x = None

        logger.info("Initializing camera...")
        camera = Picamera2()
        config = camera.create_video_configuration(
            main={"size": resolution},
            lores={"size": (320, 240), "format": "RGB888"},
            controls={"FrameRate": fps},
            buffer_count=2,
        )
        camera.configure(config)
        camera.start()
        output = StreamingOutput()
        streaming = True
        time.sleep(0.5)
        capture_thread = threading.Thread(target=frame_capture_thread, daemon=True)
        capture_thread.start()
        logger.info("Camera initialized: %s @ %sfps", resolution, fps)
        return True
    except Exception:
        logger.exception("Error initializing camera")
        streaming = False
        output = None
        if camera is not None:
            try:
                camera.close()
            except Exception:
                pass
            camera = None
        return False


def cleanup_camera():
    global camera, streaming, recording, output
    streaming = False
    time.sleep(0.2)
    if camera is not None:
        try:
            if recording:
                try:
                    camera.stop_recording()
                except Exception:
                    pass
                recording = False
            try:
                camera.stop()
            except Exception:
                pass
            try:
                camera.close()
            except Exception:
                pass
            camera = None
            logger.info("Camera cleaned up")
        except Exception:
            logger.exception("Error cleaning up camera")
    output = None


if __name__ == "__main__":
    import argparse
    import atexit

    parser = argparse.ArgumentParser(description="Raspberry Pi 5 Camera Livestream Server")
    parser.add_argument("-p", "--port", type=int, default=5000)
    parser.add_argument("-H", "--host", type=str, default="127.0.0.1")
    parser.add_argument("-r", "--resolution", type=int, nargs=2, default=[960, 540])
    parser.add_argument("-f", "--fps", type=int, default=30)
    parser.add_argument("--profile", type=str, default=None)
    parser.add_argument("--stream-name", type=str, default="livestream")
    args = parser.parse_args()

    STREAM_NAME = args.stream_name
    resolved_profile_path = resolve_profile_path(STREAM_NAME, args.profile)
    if resolved_profile_path is not None:
        apply_livestream_profile(resolved_profile_path)
        logger.info("Using profile: %s", resolved_profile_path)
    else:
        logger.info("Using profile: none")

    atexit.register(cleanup_camera)
    try:
        if initialize_camera(tuple(args.resolution), args.fps):
            logger.info("Starting livestream server on http://%s:%s", args.host, args.port)
            app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception:
        logger.exception("Fatal error")
    finally:
        cleanup_camera()
