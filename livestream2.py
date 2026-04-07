#!/usr/bin/env python3
"""
Raspberry Pi 5 Camera Livestream Server
Single-bubble pipe-locked tracker:
- only acquires a bubble at the pipe mouth
- tracks one bubble at a time
- counts once when that bubble disappears after moving upward
"""

import io
import json
import threading
import logging
import time
from pathlib import Path

from datetime import datetime
from flask import Flask, render_template_string, Response, jsonify

output = None
cv2 = None
np = None
Picamera2 = None
PROFILE_PATH = None

# =========================
# GLOBAL STATE / TUNING
# =========================
camera = None
streaming = False
recording = False
recording_output = None

active_bubble = None
candidate_bubble = None
bubble_history = []
next_bubble_id = 1
bubble_count_total = 0
dynamic_pipe_center_x = None

TARGET_FPS = 20

# ROI
ROI_X1 = 250
ROI_Y1 = 140
ROI_X2 = 760
ROI_Y2 = 500

# Pipe geometry - matched to your working test-video logic
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

# Detection logic
DETECT_INTERVAL = 0.08
MIN_RADIUS = 4
MAX_RADIUS = 20

# Acquisition band: only start a new bubble near pipe mouth
SPAWN_BAND_HALF = 22
COUNT_BAND_HALF = 12

# Candidate + robust start gating
STARTUP_IGNORE_FRAMES = 8
CANDIDATE_CONFIRM_FRAMES = 2
CANDIDATE_MATCH_DISTANCE = 50
MIN_START_BELOW_EXIT = 16

# Optional auto-centering to handle multiple pipe positions
AUTO_CENTER_ENABLED = True
AUTO_CENTER_SMOOTHING = 0.82
CENTER_SEARCH_WIDTH_RATIO = 0.35
AUTO_CENTER_MAX_OFFSET_PX = 120

# Debug
DEBUG_DRAW = True

frame_lock = threading.Lock()

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
    "max_match_distance": "MAX_MATCH_DISTANCE",
    "downward_tolerance": "DOWNWARD_TOLERANCE",
    "detect_interval": "DETECT_INTERVAL",
    "min_radius": "MIN_RADIUS",
    "max_radius": "MAX_RADIUS",
    "spawn_band_half": "SPAWN_BAND_HALF",
    "count_band_half": "COUNT_BAND_HALF",
    "startup_ignore_frames": "STARTUP_IGNORE_FRAMES",
    "candidate_confirm_frames": "CANDIDATE_CONFIRM_FRAMES",
    "candidate_match_distance": "CANDIDATE_MATCH_DISTANCE",
    "min_start_below_exit": "MIN_START_BELOW_EXIT",
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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

HTML_TEMPLATE = '''
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
        <h1>🎥 Raspberry Pi 5 Livestream</h1>
        <div class="status">● LIVE</div>

        <div class="video-container">
            <img id="stream" src="/video_feed" alt="Camera Stream">
        </div>

        <div class="controls">
            <button class="btn-capture" onclick="captureImage()">📸 Capture</button>
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
                        showMessage('📸 Image captured: ' + data.filename);
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
'''


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
                b"Content-Type: image/jpeg\r\n\r\n" +
                frame + b"\r\n"
            )

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


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


def distance(p1, p2):
    return float(np.hypot(p1[0] - p2[0], p1[1] - p2[1]))


def validate_profile_path(profile_path):
    resolved_path = Path(profile_path)
    if not resolved_path.is_file():
        raise SystemExit(f"Profile file not found: {profile_path}")
    return str(resolved_path)


def load_profile_data(profile_path):
    with open(profile_path, "r", encoding="utf-8") as profile_file:
        data = json.load(profile_file)

    if not isinstance(data, dict):
        raise SystemExit(f"Profile must be a JSON object: {profile_path}")

    return data


def apply_profile_mapping(profile_section, mapping):
    if not isinstance(profile_section, dict):
        return

    for key, global_name in mapping.items():
        if key in profile_section:
            globals()[global_name] = profile_section[key]


def apply_livestream_profile(profile_path):
    global PROFILE_PATH

    PROFILE_PATH = validate_profile_path(profile_path)
    profile_data = load_profile_data(PROFILE_PATH)

    apply_profile_mapping(profile_data.get("shared", {}), SHARED_PROFILE_FIELDS)
    apply_profile_mapping(profile_data.get("livestream", {}), LIVESTREAM_PROFILE_FIELDS)


def load_runtime_dependencies(require_camera=True):
    global cv2, np, Picamera2

    if np is None:
        import numpy as np_module
        np = np_module

    if cv2 is None:
        import cv2 as cv2_module
        cv2 = cv2_module

    if require_camera and Picamera2 is None:
        try:
            from picamera2 import Picamera2 as picamera2_class
        except ImportError:
            print("Error: picamera2 is not installed")
            raise SystemExit(1)
        Picamera2 = picamera2_class


def detect_bubbles(small_gray, scale_x, scale_y, x_offset, y_offset):
    """
    Detect circles in the reduced ROI and map center back to full-frame coordinates.
    Radius is kept as RAW Hough radius to avoid over-scaling problems.
    """
    small_gray = cv2.GaussianBlur(small_gray, (5, 5), 0)

    circles = cv2.HoughCircles(
        small_gray,
        cv2.HOUGH_GRADIENT,
        dp=1.6,
        minDist=20,
        param1=70,
        param2=10,
        minRadius=3,
        maxRadius=20,
    )

    results = []
    if circles is None:
        return results

    circles = np.round(circles[0]).astype(int)

    for (x, y, r) in circles:
        if x < 0 or y < 0 or x >= small_gray.shape[1] or y >= small_gray.shape[0]:
            continue

        cx = int(x * scale_x) + x_offset
        cy = int(y * scale_y) + y_offset
        rr = int(r)

        results.append((cx, cy, rr))

    return results


def estimate_pipe_center_x(gray_roi, fallback_center_x, previous_center_x=None):
    roi_h, roi_w = gray_roi.shape[:2]
    search_half_width = int(roi_w * CENTER_SEARCH_WIDTH_RATIO / 2)
    x_left = max(0, fallback_center_x - search_half_width)
    x_right = min(roi_w, fallback_center_x + search_half_width)
    search = gray_roi[:, x_left:x_right]

    if search.size == 0:
        return fallback_center_x

    edges = cv2.Canny(search, 50, 150)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=40,
        minLineLength=max(20, int(roi_h * 0.25)),
        maxLineGap=15,
    )

    candidate_xs = []
    if lines is not None:
        for line in lines[:, 0]:
            x1, y1, x2, y2 = line
            dx = abs(x2 - x1)
            dy = abs(y2 - y1)
            if dy > 20 and dx <= 12:
                length = np.hypot(x2 - x1, y2 - y1)
                center_x = int((x1 + x2) / 2) + x_left
                candidate_xs.append((length, center_x))

    estimated = fallback_center_x
    if candidate_xs:
        candidate_xs.sort(reverse=True, key=lambda item: item[0])
        best = candidate_xs[:2]
        estimated = int(np.mean([x for _, x in best]))
        max_delta = max(0, int(AUTO_CENTER_MAX_OFFSET_PX))
        estimated = max(fallback_center_x - max_delta, min(fallback_center_x + max_delta, estimated))

    if previous_center_x is None:
        return estimated

    return int(
        AUTO_CENTER_SMOOTHING * previous_center_x +
        (1.0 - AUTO_CENTER_SMOOTHING) * estimated
    )


def begin_candidate(cx, cy, r, now):
    return {
        "cx": cx,
        "cy": cy,
        "r": r,
        "first_seen": now,
        "last_seen": now,
        "seen_frames": 1,
    }


def promote_candidate_to_active(candidate, now, bubble_id):
    return {
        "id": bubble_id,
        "cx": candidate["cx"],
        "cy": candidate["cy"],
        "r": candidate["r"],
        "start_x": candidate["cx"],
        "start_y": candidate["cy"],
        "min_y": candidate["cy"],
        "last_seen": now,
        "seen_frames": candidate["seen_frames"],
        "lost_frames": 0,
    }


def frame_capture_thread():
    """
    One-bubble-at-a-time logic with robust candidate gating and optional
    auto-centering so one configuration works across pipe placements.
    """
    global active_bubble, candidate_bubble, next_bubble_id
    global bubble_count_total, bubble_history, dynamic_pipe_center_x

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
                _lores = request.make_array("lores")
            finally:
                if request is not None:
                    request.release()

            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            h, w = frame.shape[:2]

            if frame_count <= STARTUP_IGNORE_FRAMES:
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
            x_offset, y_offset = x1, y1

            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (7, 7), 0)
            small = cv2.resize(blur, (64, 48), interpolation=cv2.INTER_AREA)
            small = cv2.equalizeHist(small)

            fallback_pipe_center_x = (roi_w // 2) + PIPE_CENTER_X_BIAS
            if AUTO_CENTER_ENABLED:
                dynamic_pipe_center_x = estimate_pipe_center_x(
                    gray_roi=gray,
                    fallback_center_x=fallback_pipe_center_x,
                    previous_center_x=dynamic_pipe_center_x,
                )
                pipe_center_x = x_offset + dynamic_pipe_center_x
            else:
                pipe_center_x = x_offset + fallback_pipe_center_x

            pipe_width = int(roi_w * PIPE_WIDTH_RATIO)
            pipe_lock_width = int(roi_w * PIPE_LOCK_WIDTH_RATIO)
            pipe_top = y_offset + int(roi_h * PIPE_TOP_RATIO)
            pipe_bottom = y_offset + int(roi_h * PIPE_BOTTOM_RATIO)
            pipe_exit_y = y_offset + int(roi_h * PIPE_EXIT_RATIO)

            spawn_y1 = pipe_exit_y - SPAWN_BAND_HALF
            spawn_y2 = pipe_exit_y + SPAWN_BAND_HALF
            count_y1 = pipe_exit_y - COUNT_BAND_HALF
            count_y2 = pipe_exit_y + COUNT_BAND_HALF

            if DEBUG_DRAW:
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
                cv2.line(frame, (pipe_center_x, y1), (pipe_center_x, y2), (255, 255, 0), 2)
                cv2.rectangle(frame, (pipe_center_x - pipe_width, pipe_top), (pipe_center_x + pipe_width, pipe_bottom), (255, 0, 255), 2)
                cv2.rectangle(frame, (pipe_center_x - pipe_lock_width, pipe_top), (pipe_center_x + pipe_lock_width, pipe_bottom), (0, 255, 255), 1)
                cv2.rectangle(frame, (x1, spawn_y1), (x2, spawn_y2), (0, 165, 255), 1)
                cv2.rectangle(frame, (x1, count_y1), (x2, count_y2), (0, 0, 255), 1)

            detections = []
            if time.time() - last_detect_time > DETECT_INTERVAL:
                raw_detections = detect_bubbles(
                    small_gray=small,
                    scale_x=roi_w / 64,
                    scale_y=roi_h / 48,
                    x_offset=x_offset,
                    y_offset=y_offset,
                )
                last_detect_time = time.time()

                detections = [
                    (cx, cy, r)
                    for (cx, cy, r) in raw_detections
                    if MIN_RADIUS <= r <= MAX_RADIUS
                ]
                detections = [
                    (cx, cy, r)
                    for (cx, cy, r) in detections
                    if abs(cx - pipe_center_x) <= pipe_lock_width and pipe_top <= cy <= pipe_bottom
                ]

                if active_bubble is None:
                    detections = [
                        (cx, cy, r)
                        for (cx, cy, r) in detections
                        if spawn_y1 <= cy <= spawn_y2 and cy >= (pipe_exit_y + MIN_START_BELOW_EXIT)
                    ]
                    detections = sorted(detections, key=lambda d: (abs(d[0] - pipe_center_x), -d[2]))
                else:
                    detections = sorted(
                        detections,
                        key=lambda d: distance((d[0], d[1]), (active_bubble["cx"], active_bubble["cy"])),
                    )

            if DEBUG_DRAW:
                for (cx, cy, r) in detections:
                    cv2.circle(frame, (cx, cy), max(8, int(r * 2.0)), (0, 255, 255), 2)

            now = time.time()

            if active_bubble is None:
                if detections:
                    cx, cy, r = detections[0]
                    if candidate_bubble is None:
                        candidate_bubble = begin_candidate(cx, cy, r, now)
                    else:
                        d = distance((cx, cy), (candidate_bubble["cx"], candidate_bubble["cy"]))
                        if d < CANDIDATE_MATCH_DISTANCE:
                            candidate_bubble["cx"] = int(0.7 * candidate_bubble["cx"] + 0.3 * cx)
                            candidate_bubble["cy"] = int(0.7 * candidate_bubble["cy"] + 0.3 * cy)
                            candidate_bubble["r"] = r
                            candidate_bubble["last_seen"] = now
                            candidate_bubble["seen_frames"] += 1
                        else:
                            candidate_bubble = begin_candidate(cx, cy, r, now)

                    if candidate_bubble["seen_frames"] >= CANDIDATE_CONFIRM_FRAMES:
                        active_bubble = promote_candidate_to_active(candidate_bubble, now, next_bubble_id)
                        next_bubble_id += 1
                        candidate_bubble = None
                else:
                    candidate_bubble = None
            else:
                best_det = None
                best_dist = 999999.0
                for det in detections:
                    cx, cy, _ = det
                    if cy > active_bubble["cy"] + DOWNWARD_TOLERANCE:
                        continue
                    d = distance((cx, cy), (active_bubble["cx"], active_bubble["cy"]))
                    if d < best_dist:
                        best_dist = d
                        best_det = det

                if best_det is not None and best_dist < MAX_MATCH_DISTANCE:
                    cx, cy, r = best_det
                    active_bubble["cx"] = int(0.7 * active_bubble["cx"] + 0.3 * cx)
                    active_bubble["cy"] = int(0.7 * active_bubble["cy"] + 0.3 * cy)
                    active_bubble["r"] = r
                    active_bubble["last_seen"] = now
                    active_bubble["seen_frames"] += 1
                    active_bubble["lost_frames"] = 0
                    active_bubble["min_y"] = min(active_bubble["min_y"], active_bubble["cy"])
                else:
                    active_bubble["lost_frames"] += 1

            if active_bubble is not None and active_bubble["lost_frames"] >= LOST_AFTER_FRAMES:
                traveled_up = active_bubble["start_y"] - active_bubble["min_y"]
                should_count = (
                    active_bubble["seen_frames"] >= LOCK_AFTER_FRAMES and
                    traveled_up >= MIN_UPWARD_TRAVEL
                )
                if should_count:
                    bubble_count_total += 1

                bubble_history.append({
                    "id": active_bubble["id"],
                    "counted": should_count,
                    "start_x": active_bubble["start_x"],
                    "start_y": active_bubble["start_y"],
                    "end_x": active_bubble["cx"],
                    "end_y": active_bubble["cy"],
                    "min_y": active_bubble["min_y"],
                    "seen_frames": active_bubble["seen_frames"],
                    "traveled_up": traveled_up,
                    "ended_at": now,
                })
                active_bubble = None

            if active_bubble is not None:
                cx = active_bubble["cx"]
                cy = active_bubble["cy"]
                r = max(10, int(active_bubble["r"] * 2.5))
                cv2.circle(frame, (cx, cy), r, (0, 255, 0), 2)
                cv2.circle(frame, (cx, cy), 4, (0, 255, 0), -1)
                cv2.putText(frame, f"ID {active_bubble['id']} TRACKING", (cx + 10, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

            if candidate_bubble is not None and DEBUG_DRAW:
                cv2.circle(frame, (candidate_bubble["cx"], candidate_bubble["cy"]), max(8, int(candidate_bubble["r"] * 2.0)), (255, 165, 0), 1)
                cv2.putText(frame, f"CAND {candidate_bubble['seen_frames']}", (candidate_bubble["cx"] + 10, candidate_bubble["cy"] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 165, 0), 1)

            fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))
            overlay = frame.copy()
            box_width = 290
            box_height = 155
            ox1 = frame.shape[1] - box_width - 10
            oy1 = 10
            ox2 = frame.shape[1] - 10
            oy2 = oy1 + box_height
            cv2.rectangle(overlay, (ox1, oy1), (ox2, oy2), (0, 0, 0), -1)
            frame = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0)

            texts = [
                f"FPS: {fps:.1f}",
                f"Frame: {frame_count}",
                f"Bubble: {1 if active_bubble is not None else 0}",
                f"Candidate: {1 if candidate_bubble is not None else 0}",
                f"Count: {bubble_count_total}",
                f"Bias: {PIPE_CENTER_X_BIAS}",
                f"Auto center: {'ON' if AUTO_CENTER_ENABLED else 'OFF'}",
            ]
            y = oy1 + 22
            for text in texts:
                cv2.putText(frame, text, (ox1 + 10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
                y += 20

            ret, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 45])
            if ret:
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
    global camera, streaming, output

    try:
        load_runtime_dependencies(require_camera=True)
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

        capture_thread = threading.Thread(
            target=frame_capture_thread,
            daemon=True
        )
        capture_thread.start()

        logger.info("Camera initialized: %s @ %sfps", resolution, fps)
        return True

    except Exception as e:
        logger.exception("Error initializing camera: %s", e)
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

        except Exception as e:
            logger.exception("Error cleaning up camera: %s", e)

    output = None


if __name__ == "__main__":
    import argparse
    import atexit

    parser = argparse.ArgumentParser(description="Raspberry Pi 5 Camera Livestream Server")
    parser.add_argument("-p", "--port", type=int, default=5000)
    parser.add_argument("-H", "--host", type=str, default="127.0.0.1")
    parser.add_argument("-r", "--resolution", type=int, nargs=2, default=[960, 540])
    parser.add_argument("-f", "--fps", type=int, default=30)
    parser.add_argument(
        "--profile",
        help="Profile file to load for live camera tuning overrides",
    )
    args = parser.parse_args()

    atexit.register(cleanup_camera)

    try:
        if args.profile:
            apply_livestream_profile(args.profile)

        if initialize_camera(tuple(args.resolution), args.fps):
            logger.info("Starting livestream server on http://%s:%s", args.host, args.port)
            app.run(
                host=args.host,
                port=args.port,
                debug=False,
                threaded=True,
                use_reloader=False
            )

    except KeyboardInterrupt:
        logger.info("Shutting down...")

    except Exception as e:
        logger.exception("Fatal error: %s", e)

    finally:
        cleanup_camera()