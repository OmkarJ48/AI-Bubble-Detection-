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

# Auto-centering
AUTO_CENTER_ENABLED = True
AUTO_CENTER_SMOOTHING = 0.85
CENTER_SEARCH_WIDTH_RATIO = 0.35
AUTO_CENTER_MAX_OFFSET_PX = 120

# Acquisition band: only start a new bubble near pipe mouth
SPAWN_BAND_HALF = 22
COUNT_BAND_HALF = 12

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
    "auto_center_enabled": "AUTO_CENTER_ENABLED",
    "auto_center_smoothing": "AUTO_CENTER_SMOOTHING",
    "center_search_width_ratio": "CENTER_SEARCH_WIDTH_RATIO",
    "auto_center_max_offset_px": "AUTO_CENTER_MAX_OFFSET_PX",
    "spawn_band_half": "SPAWN_BAND_HALF",
    "count_band_half": "COUNT_BAND_HALF",
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


def estimate_pipe_center_x(
    gray_roi,
    fallback_center_x,
    previous_center_x=None,
    *,
    search_width_ratio,
    auto_center_smoothing,
    auto_center_max_offset_px,
):
    """
    Estimate the pipe center from near-vertical edges around the fallback center.
    The result is clamped to a small local window so reflections do not drag the
    lock zone away from the pipe.
    """
    roi_h, roi_w = gray_roi.shape[:2]

    search_half_width = int(roi_w * search_width_ratio / 2)
    fallback_local_x = fallback_center_x
    x_left = max(0, fallback_local_x - search_half_width)
    x_right = min(roi_w, fallback_local_x + search_half_width)

    search = gray_roi[:, x_left:x_right]
    if search.size == 0:
        return fallback_center_x

    upper_search = search[:max(1, int(search.shape[0] * 0.52)), :]
    dark_source = cv2.GaussianBlur(upper_search, (9, 9), 0)
    dark_columns = dark_source.mean(axis=0)
    dark_band_width = max(24, min(120, int(gray_roi.shape[1] * 0.08)))
    if dark_columns.shape[0] >= dark_band_width:
        kernel = np.ones(dark_band_width) / dark_band_width
        smoothed_dark = np.convolve(dark_columns, kernel, mode="valid")
        dark_band_center = int(np.argmin(smoothed_dark)) + x_left + (dark_band_width // 2)
    else:
        dark_band_center = int(np.argmin(dark_columns)) + x_left

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

    if candidate_xs:
        candidate_xs.sort(reverse=True, key=lambda item: item[0])
        best = candidate_xs[:2]
        line_center = int(np.mean([x for _, x in best]))
        if abs(line_center - dark_band_center) <= max(20, dark_band_width // 3):
            estimated = int(round((line_center + dark_band_center) / 2))
        else:
            estimated = dark_band_center
    else:
        estimated = dark_band_center

    min_center_x = fallback_local_x - auto_center_max_offset_px
    max_center_x = fallback_local_x + auto_center_max_offset_px
    estimated = max(min_center_x, min(max_center_x, estimated))

    if previous_center_x is None:
        return estimated

    smoothed = int(
        auto_center_smoothing * previous_center_x +
        (1.0 - auto_center_smoothing) * estimated
    )
    return smoothed


def build_pipe_geometry(
    gray_roi,
    *,
    x_offset,
    y_offset,
    auto_center_enabled,
    previous_center_x,
    pipe_center_x_bias,
    pipe_width_ratio,
    pipe_lock_width_ratio,
    pipe_top_ratio,
    pipe_bottom_ratio,
    pipe_exit_ratio,
    center_search_width_ratio,
    auto_center_smoothing,
    auto_center_max_offset_px,
    spawn_band_half,
    count_band_half,
    count_band_y_bias=0,
):
    """
    Compute the shared pipe geometry used by both the live stream and the
    offline video harness.
    """
    roi_h, roi_w = gray_roi.shape[:2]
    fallback_pipe_center_x = (roi_w // 2) + pipe_center_x_bias

    if auto_center_enabled:
        pipe_center_local_x = estimate_pipe_center_x(
            gray_roi=gray_roi,
            fallback_center_x=fallback_pipe_center_x,
            previous_center_x=previous_center_x,
            search_width_ratio=center_search_width_ratio,
            auto_center_smoothing=auto_center_smoothing,
            auto_center_max_offset_px=auto_center_max_offset_px,
        )
    else:
        pipe_center_local_x = fallback_pipe_center_x

    pipe_center_x = x_offset + pipe_center_local_x
    pipe_width = int(roi_w * pipe_width_ratio)
    pipe_lock_width = int(roi_w * pipe_lock_width_ratio)
    pipe_top = y_offset + int(roi_h * pipe_top_ratio)
    pipe_bottom = y_offset + int(roi_h * pipe_bottom_ratio)
    pipe_exit_y = y_offset + int(roi_h * pipe_exit_ratio)

    return {
        "pipe_center_local_x": pipe_center_local_x,
        "pipe_center_x": pipe_center_x,
        "pipe_width": pipe_width,
        "pipe_lock_width": pipe_lock_width,
        "pipe_top": pipe_top,
        "pipe_bottom": pipe_bottom,
        "pipe_exit_y": pipe_exit_y,
        "spawn_y1": pipe_exit_y - spawn_band_half,
        "spawn_y2": pipe_exit_y + spawn_band_half,
        "count_y1": pipe_exit_y - count_band_half + count_band_y_bias,
        "count_y2": pipe_exit_y + count_band_half + count_band_y_bias,
    }


def build_detection_roi_bounds(gray_roi, geometry, *, x_offset, y_offset):
    """
    Build a tighter detection ROI around the pipe corridor so circle detection is
    not distracted by the rest of the frame.
    """
    roi_h, roi_w = gray_roi.shape[:2]
    local_x1 = max(0, geometry["pipe_center_local_x"] - geometry["pipe_width"])
    local_x2 = min(roi_w, geometry["pipe_center_local_x"] + geometry["pipe_width"])
    local_y1 = max(0, geometry["pipe_top"] - y_offset)
    local_y2 = min(roi_h, geometry["pipe_bottom"] - y_offset)

    return {
        "local_x1": local_x1,
        "local_x2": local_x2,
        "local_y1": local_y1,
        "local_y2": local_y2,
        "x_offset": x_offset + local_x1,
        "y_offset": y_offset + local_y1,
    }


def filter_tracker_detections(
    raw_detections,
    geometry,
    active_bubble,
    *,
    min_radius,
    max_radius,
    max_match_distance,
    downward_tolerance,
):
    """
    Keep only detections that are consistent with the pipe corridor and the
    one-bubble tracker state.
    """
    filtered = [
        (cx, cy, r)
        for (cx, cy, r) in raw_detections
        if min_radius <= r <= max_radius
    ]

    filtered = [
        (cx, cy, r)
        for (cx, cy, r) in filtered
        if (
            abs(cx - geometry["pipe_center_x"]) <= geometry["pipe_lock_width"]
            and geometry["pipe_top"] <= cy <= geometry["pipe_bottom"]
        )
    ]

    if active_bubble is None:
        filtered = [
            (cx, cy, r)
            for (cx, cy, r) in filtered
            if geometry["spawn_y1"] <= cy <= geometry["spawn_y2"]
        ]
        filtered = sorted(
            filtered,
            key=lambda d: (abs(d[0] - geometry["pipe_center_x"]), -d[2]),
        )
        return filtered

    filtered = [
        (cx, cy, r)
        for (cx, cy, r) in filtered
        if (
            abs(cx - active_bubble["cx"]) <= max_match_distance
            and abs(cy - active_bubble["cy"]) <= (max_match_distance + downward_tolerance)
        )
    ]
    filtered = sorted(
        filtered,
        key=lambda d: distance((d[0], d[1]), (active_bubble["cx"], active_bubble["cy"])),
    )
    return filtered


def update_single_bubble_tracker(
    active_bubble,
    detections,
    now,
    *,
    next_bubble_id,
    lock_after_frames,
    lost_after_frames,
    min_upward_travel,
    max_match_distance,
    downward_tolerance,
):
    """
    Shared one-bubble state machine for acquisition, tracking, count-on-loss,
    and reset for the next bubble.
    """
    count_increment = 0
    ended_entry = None

    if active_bubble is None:
        if detections:
            cx, cy, r = detections[0]
            active_bubble = {
                "id": next_bubble_id,
                "cx": cx,
                "cy": cy,
                "r": r,
                "start_x": cx,
                "start_y": cy,
                "min_y": cy,
                "last_seen": now,
                "seen_frames": 1,
                "lost_frames": 0,
            }
            next_bubble_id += 1
        return active_bubble, next_bubble_id, ended_entry, count_increment

    best_det = None
    best_dist = 999999.0

    for det in detections:
        cx, cy, r = det
        if cy > active_bubble["cy"] + downward_tolerance:
            continue

        dist = distance((cx, cy), (active_bubble["cx"], active_bubble["cy"]))
        if dist < best_dist:
            best_dist = dist
            best_det = det

    if best_det is not None and best_dist < max_match_distance:
        cx, cy, r = best_det
        active_bubble["cx"] = int(0.7 * active_bubble["cx"] + 0.3 * cx)
        active_bubble["cy"] = int(0.7 * active_bubble["cy"] + 0.3 * cy)
        active_bubble["r"] = r
        active_bubble["last_seen"] = now
        active_bubble["seen_frames"] += 1
        active_bubble["lost_frames"] = 0
        active_bubble["min_y"] = min(active_bubble["min_y"], active_bubble["cy"])
        return active_bubble, next_bubble_id, ended_entry, count_increment

    active_bubble["lost_frames"] += 1
    if active_bubble["lost_frames"] < lost_after_frames:
        return active_bubble, next_bubble_id, ended_entry, count_increment

    traveled_up = active_bubble["start_y"] - active_bubble["min_y"]
    should_count = (
        active_bubble["seen_frames"] >= lock_after_frames
        and traveled_up >= min_upward_travel
    )

    ended_entry = {
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
    }
    if should_count:
        count_increment = 1

    return None, next_bubble_id, ended_entry, count_increment


def reset_live_tracker_state(reset_counts=False):
    global active_bubble, bubble_history, next_bubble_id, bubble_count_total
    global dynamic_pipe_center_x

    active_bubble = None
    dynamic_pipe_center_x = None

    if reset_counts:
        bubble_history = []
        next_bubble_id = 1
        bubble_count_total = 0


def frame_capture_thread():
    """
    One-bubble-at-a-time logic:
    1. Only acquire a new bubble near the pipe mouth.
    2. Track only that bubble.
    3. Ignore all other detections.
    4. Count only when it disappears after moving upward enough.
    """
    global active_bubble, next_bubble_id, bubble_count_total, bubble_history
    global dynamic_pipe_center_x

    frame_count = 0
    frame_interval = 1.0 / TARGET_FPS
    last_detect_time = 0.0

    logger.info("Frame capture thread started")
    print("THREAD STARTED")

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

            if frame_count < 10:
                continue

            # ROI
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

            geometry = build_pipe_geometry(
                gray_roi=gray,
                x_offset=x_offset,
                y_offset=y_offset,
                auto_center_enabled=AUTO_CENTER_ENABLED,
                previous_center_x=dynamic_pipe_center_x,
                pipe_center_x_bias=PIPE_CENTER_X_BIAS,
                pipe_width_ratio=PIPE_WIDTH_RATIO,
                pipe_lock_width_ratio=PIPE_LOCK_WIDTH_RATIO,
                pipe_top_ratio=PIPE_TOP_RATIO,
                pipe_bottom_ratio=PIPE_BOTTOM_RATIO,
                pipe_exit_ratio=PIPE_EXIT_RATIO,
                center_search_width_ratio=CENTER_SEARCH_WIDTH_RATIO,
                auto_center_smoothing=AUTO_CENTER_SMOOTHING,
                auto_center_max_offset_px=AUTO_CENTER_MAX_OFFSET_PX,
                spawn_band_half=SPAWN_BAND_HALF,
                count_band_half=COUNT_BAND_HALF,
                count_band_y_bias=-30,
            )
            dynamic_pipe_center_x = geometry["pipe_center_local_x"]

            if DEBUG_DRAW:
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
                cv2.putText(
                    frame, "ROI", (x1, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1
                )

                cv2.line(
                    frame,
                    (geometry["pipe_center_x"], y1),
                    (geometry["pipe_center_x"], y2),
                    (255, 255, 0),
                    2,
                )
                cv2.rectangle(
                    frame,
                    (geometry["pipe_center_x"] - geometry["pipe_width"], geometry["pipe_top"]),
                    (geometry["pipe_center_x"] + geometry["pipe_width"], geometry["pipe_bottom"]),
                    (255, 0, 255), 2
                )
                cv2.rectangle(
                    frame,
                    (geometry["pipe_center_x"] - geometry["pipe_lock_width"], geometry["pipe_top"]),
                    (geometry["pipe_center_x"] + geometry["pipe_lock_width"], geometry["pipe_bottom"]),
                    (0, 255, 255), 1
                )
                cv2.rectangle(
                    frame,
                    (x1, geometry["spawn_y1"]),
                    (x2, geometry["spawn_y2"]),
                    (0, 165, 255),
                    1,
                )
                cv2.putText(
                    frame, "SPAWN BAND", (x1, geometry["spawn_y1"] - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1
                )
                cv2.rectangle(
                    frame,
                    (x1, geometry["count_y1"]),
                    (x2, geometry["count_y2"]),
                    (0, 0, 255),
                    1,
                )
                cv2.putText(
                    frame, "UPWARD CHECK", (x1, geometry["count_y1"] - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1
                )

            detections = []

            if time.time() - last_detect_time > DETECT_INTERVAL:
                raw_detections = detect_bubbles(
                    small_gray=small,
                    scale_x=roi_w / 64,
                    scale_y=roi_h / 48,
                    x_offset=x_offset,
                    y_offset=y_offset
                )
                last_detect_time = time.time()

                detections = filter_tracker_detections(
                    raw_detections=raw_detections,
                    geometry=geometry,
                    active_bubble=active_bubble,
                    min_radius=MIN_RADIUS,
                    max_radius=MAX_RADIUS,
                    max_match_distance=MAX_MATCH_DISTANCE,
                    downward_tolerance=DOWNWARD_TOLERANCE,
                )

                print(f"RAW detections: {len(raw_detections)} -> {raw_detections}")
                print(f"FILTERED detections: {len(detections)} -> {detections}")

            if DEBUG_DRAW:
                for (cx, cy, r) in detections:
                    draw_r = max(8, int(r * 2.0))
                    cv2.circle(frame, (cx, cy), draw_r, (0, 255, 255), 2)

            now = time.time()

            previous_active_id = active_bubble["id"] if active_bubble is not None else None
            active_bubble, next_bubble_id, ended_entry, count_increment = update_single_bubble_tracker(
                active_bubble=active_bubble,
                detections=detections,
                now=now,
                next_bubble_id=next_bubble_id,
                lock_after_frames=LOCK_AFTER_FRAMES,
                lost_after_frames=LOST_AFTER_FRAMES,
                min_upward_travel=MIN_UPWARD_TRAVEL,
                max_match_distance=MAX_MATCH_DISTANCE,
                downward_tolerance=DOWNWARD_TOLERANCE,
            )

            if previous_active_id is None and active_bubble is not None:
                print(f"START bubble {active_bubble['id']} at ({active_bubble['cx']}, {active_bubble['cy']})")

            if ended_entry is not None:
                if count_increment:
                    bubble_count_total += count_increment
                    print(f"COUNTED bubble {ended_entry['id']} total={bubble_count_total}")
                else:
                    print(
                        f"DROPPED bubble {ended_entry['id']} "
                        f"seen_frames={ended_entry['seen_frames']} "
                        f"traveled_up={ended_entry['traveled_up']}"
                    )

                bubble_history.append(ended_entry)
                print(f"ENDED bubble {ended_entry['id']} counted={ended_entry['counted']}")

            # =========================
            # DRAW ACTIVE BUBBLE
            # =========================
            if active_bubble is not None:
                cx = active_bubble["cx"]
                cy = active_bubble["cy"]
                r = max(10, int(active_bubble["r"] * 2.5))
                bid = active_bubble["id"]

                cv2.circle(frame, (cx, cy), r, (0, 255, 0), 2)
                cv2.circle(frame, (cx, cy), 4, (0, 255, 0), -1)
                cv2.putText(
                    frame, f"ID {bid} TRACKING", (cx + 10, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1
                )

            # Overlay
            fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))

            overlay = frame.copy()
            box_width = 280
            box_height = 135
            ox1 = frame.shape[1] - box_width - 10
            oy1 = 10
            ox2 = frame.shape[1] - 10
            oy2 = oy1 + box_height

            cv2.rectangle(overlay, (ox1, oy1), (ox2, oy2), (0, 0, 0), -1)
            frame = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0)

            y = oy1 + 22
            line_height = 20
            texts = [
                f"FPS: {fps:.1f}",
                f"Frame: {frame_count}",
                f"Tracking: {1 if active_bubble is not None else 0}",
                f"Count: {bubble_count_total}",
                f"Pipe bias: {PIPE_CENTER_X_BIAS}",
                f"Auto center: {'ON' if AUTO_CENTER_ENABLED else 'OFF'}",
            ]

            for text in texts:
                cv2.putText(
                    frame, text, (ox1 + 10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2
                )
                y += line_height

            ret, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 45])
            if ret:
                with output.condition:
                    output.frame = jpeg.tobytes()
                    output.condition.notify_all()

            elapsed = time.time() - start_time
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)

        except Exception as e:
            print("Thread error:", e)
            logger.exception("Frame capture thread error")
            time.sleep(0.1)


def initialize_camera(resolution=(960, 540), fps=30):
    global camera, streaming, output

    try:
        load_runtime_dependencies(require_camera=True)
        reset_live_tracker_state(reset_counts=True)
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
    reset_live_tracker_state(reset_counts=False)

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
