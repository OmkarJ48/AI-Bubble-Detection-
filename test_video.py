import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import argparse
import atexit
import json
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string

cv2 = None
np = None
detect_bubbles = None
build_pipe_geometry = None
filter_tracker_detections = None
update_single_bubble_tracker = None

DEFAULT_VIDEO_PATH = "Bubbles.mp4"
VIDEO_PATH = DEFAULT_VIDEO_PATH
LOG_PATH = "bubble_log_Bubbles.jsonl"
PROFILE_PATH = None

TARGET_FPS = 20
LOCK_AFTER_FRAMES = 2
LOST_AFTER_FRAMES = 3
MIN_UPWARD_TRAVEL = 18

# ---- PIPE TUNING ----
# Verified manual fallback
PIPE_CENTER_X_BIAS = -50

# Width / lock zone ratios
PIPE_WIDTH_RATIO = 0.35
PIPE_LOCK_WIDTH_RATIO = 0.18

# Vertical pipe region
PIPE_TOP_RATIO = 0.25
PIPE_BOTTOM_RATIO = 0.75
PIPE_EXIT_RATIO = 0.55

# Count band thickness
COUNT_BAND_HALF = 12

# Detection timing
DETECT_EVERY_SECONDS = 0.10

# Bubble size filter
MIN_RADIUS = 10
MAX_RADIUS = 20

# Match distance
MAX_MATCH_DISTANCE = 60
DOWNWARD_TOLERANCE = 10

# Acquisition band
SPAWN_BAND_HALF = 22

# Auto center calibration
AUTO_CENTER_ENABLED = True
AUTO_CENTER_SMOOTHING = 0.80   # higher = more stable, less reactive
CENTER_SEARCH_WIDTH_RATIO = 0.35
AUTO_CENTER_MAX_OFFSET_PX = 120

# Debug overlay
DEBUG_DRAW = True


app = Flask(__name__)
cap = None

active_bubble = None
bubble_history = []
next_bubble_id = 1
bubble_count_total = 0
last_detect_time = 0.0
dynamic_pipe_center_x = None


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Bubble Video Stream Test</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: Arial, sans-serif;
            text-align: center;
            background: #111;
            color: white;
            margin: 0;
            padding: 20px;
        }
        .toolbar {
            margin-bottom: 16px;
        }
        button {
            padding: 10px 16px;
            margin: 0 6px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
        }
        .btn {
            background: #3498db;
            color: white;
        }
        .btn:hover {
            background: #2980b9;
        }
        .status {
            margin-top: 10px;
            font-size: 14px;
            color: #9f9f9f;
        }
        img {
            max-width: 95%;
            border: 2px solid #444;
            border-radius: 8px;
        }
    </style>
</head>
<body>
    <h1>Bubble Video Stream Test</h1>

    <div class="toolbar">
        <button class="btn" onclick="toggleDebug()">Toggle Debug Overlay</button>
        <button class="btn" onclick="resetCounter()">Reset Counter</button>
    </div>

    <div class="status" id="status">Loading...</div>

    <img id="stream" src="/video_feed" alt="Bubble stream">

    <script>
        function refreshStatus() {
            fetch('/status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('status').textContent =
                        'Video: ' + data.video +
                        ' | Debug: ' + (data.debug ? 'ON' : 'OFF') +
                        ' | Count: ' + data.count +
                        ' | Active bubble: ' + (data.active_bubble ? 'YES' : 'NO') +
                        ' | Auto center: ' + (data.auto_center ? 'ON' : 'OFF');
                })
                .catch(() => {
                    document.getElementById('status').textContent = 'Status unavailable';
                });
        }

        function toggleDebug() {
            fetch('/toggle_debug', { method: 'POST' })
                .then(() => refreshStatus());
        }

        function resetCounter() {
            fetch('/reset_counter', { method: 'POST' })
                .then(() => refreshStatus());
        }

        setInterval(refreshStatus, 1000);
        refreshStatus();
    </script>
</body>
</html>
"""

SHARED_PROFILE_FIELDS = {
    "pipe_center_x_bias": "PIPE_CENTER_X_BIAS",
    "pipe_width_ratio": "PIPE_WIDTH_RATIO",
    "pipe_lock_width_ratio": "PIPE_LOCK_WIDTH_RATIO",
    "pipe_top_ratio": "PIPE_TOP_RATIO",
    "pipe_bottom_ratio": "PIPE_BOTTOM_RATIO",
    "pipe_exit_ratio": "PIPE_EXIT_RATIO",
    "count_band_half": "COUNT_BAND_HALF",
}

TEST_VIDEO_PROFILE_FIELDS = {
    "target_fps": "TARGET_FPS",
    "lock_after_frames": "LOCK_AFTER_FRAMES",
    "lost_after_frames": "LOST_AFTER_FRAMES",
    "min_travel_y": "MIN_UPWARD_TRAVEL",
    "min_upward_travel": "MIN_UPWARD_TRAVEL",
    "detect_every_seconds": "DETECT_EVERY_SECONDS",
    "min_radius": "MIN_RADIUS",
    "max_radius": "MAX_RADIUS",
    "max_match_distance": "MAX_MATCH_DISTANCE",
    "downward_tolerance": "DOWNWARD_TOLERANCE",
    "spawn_band_half": "SPAWN_BAND_HALF",
    "auto_center_enabled": "AUTO_CENTER_ENABLED",
    "auto_center_smoothing": "AUTO_CENTER_SMOOTHING",
    "center_search_width_ratio": "CENTER_SEARCH_WIDTH_RATIO",
    "auto_center_max_offset_px": "AUTO_CENTER_MAX_OFFSET_PX",
    "debug_draw": "DEBUG_DRAW",
}


def build_log_path(video_path):
    return f"bubble_log_{Path(video_path).stem}.jsonl"


def build_default_profile_path(video_path):
    return Path("profiles") / f"{Path(video_path).stem}.json"


def validate_video_path(video_path):
    resolved_path = Path(video_path)
    if not resolved_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")
    return str(resolved_path)


def validate_profile_path(profile_path):
    resolved_path = Path(profile_path)
    if not resolved_path.is_file():
        raise SystemExit(f"Profile file not found: {profile_path}")
    return str(resolved_path)


def resolve_profile_path(video_path, explicit_profile_path=None):
    if explicit_profile_path is not None:
        return validate_profile_path(explicit_profile_path)

    default_profile = build_default_profile_path(video_path)
    if default_profile.is_file():
        return str(default_profile)

    fallback_profile = Path("profiles") / "Bubbles.json"
    if fallback_profile.is_file():
        return str(fallback_profile)

    raise SystemExit(
        f"No profile found for {video_path}. Expected {default_profile} "
        "or fallback profiles/Bubbles.json."
    )


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


def apply_test_video_profile(profile_path):
    global PROFILE_PATH

    PROFILE_PATH = validate_profile_path(profile_path)
    profile_data = load_profile_data(PROFILE_PATH)

    apply_profile_mapping(profile_data.get("shared", {}), SHARED_PROFILE_FIELDS)
    apply_profile_mapping(profile_data.get("test_video", {}), TEST_VIDEO_PROFILE_FIELDS)


def load_runtime_dependencies():
    global cv2, np, detect_bubbles
    global build_pipe_geometry, filter_tracker_detections, update_single_bubble_tracker

    import cv2 as cv2_module
    import numpy as np_module
    from livestream import (
        build_pipe_geometry as build_pipe_geometry_func,
        detect_bubbles as detect_bubbles_func,
        filter_tracker_detections as filter_tracker_detections_func,
        load_runtime_dependencies as load_livestream_runtime_dependencies,
        update_single_bubble_tracker as update_single_bubble_tracker_func,
    )

    load_livestream_runtime_dependencies(require_camera=False)

    cv2 = cv2_module
    np = np_module
    build_pipe_geometry = build_pipe_geometry_func
    detect_bubbles = detect_bubbles_func
    filter_tracker_detections = filter_tracker_detections_func
    update_single_bubble_tracker = update_single_bubble_tracker_func


def configure_video_source(video_path):
    global VIDEO_PATH, LOG_PATH, cap

    VIDEO_PATH = validate_video_path(video_path)
    LOG_PATH = build_log_path(VIDEO_PATH)
    reset_tracker_runtime_state(reset_counts=True)

    if cap is not None:
        cap.release()

    cap = cv2.VideoCapture(VIDEO_PATH)
    if not cap.isOpened():
        raise SystemExit(f"Could not open video file: {VIDEO_PATH}")


def cleanup_video_source():
    global cap

    if cap is not None:
        cap.release()
        cap = None


def current_video_name():
    return Path(VIDEO_PATH).name


def distance(p1, p2):
    return float(np.hypot(p1[0] - p2[0], p1[1] - p2[1]))


def log_bubble_event(entry):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print("Failed to write log:", e)


def reset_tracker_runtime_state(reset_counts=False):
    global active_bubble
    global bubble_history
    global next_bubble_id
    global bubble_count_total
    global last_detect_time
    global dynamic_pipe_center_x

    active_bubble = None
    last_detect_time = 0.0
    dynamic_pipe_center_x = None

    if reset_counts:
        bubble_history = []
        next_bubble_id = 1
        bubble_count_total = 0


def generate_frames():
    global active_bubble
    global bubble_history
    global next_bubble_id
    global bubble_count_total
    global last_detect_time
    global dynamic_pipe_center_x
    global DEBUG_DRAW

    frame_count = 0
    frame_interval = 1.0 / TARGET_FPS

    while True:
        start_time = time.time()

        ret, frame = cap.read()
        if not ret:
            reset_tracker_runtime_state(reset_counts=False)
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue

        frame_count += 1
        h, w = frame.shape[:2]

        # Full-frame ROI for testing
        x1, y1, x2, y2 = 0, 0, w, h
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

        # ---- DRAW GUIDES ----
        if DEBUG_DRAW:
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
                frame, "SPAWN BAND", (x1 + 10, geometry["spawn_y1"] - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1
            )

            cv2.rectangle(
                frame,
                (x1, geometry["count_y1"]),
                (x2, geometry["count_y2"]),
                (0, 100, 255),
                1,
            )

            cv2.putText(
                frame, "UPWARD CHECK", (x1 + 10, geometry["count_y1"] - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1
            )

        detections = []
        raw_detections = []

        # ---- DETECT ----
        if time.time() - last_detect_time > DETECT_EVERY_SECONDS:
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

        if DEBUG_DRAW:
            for (cx, cy, r) in detections:
                cv2.circle(frame, (cx, cy), int(r), (0, 255, 255), 2)

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
            bubble_history.append(ended_entry)
            log_bubble_event(ended_entry)

            if count_increment:
                bubble_count_total += count_increment
                print(f"COUNTED bubble {ended_entry['id']} total={bubble_count_total}")
            else:
                print(
                    f"DROPPED bubble {ended_entry['id']} "
                    f"seen_frames={ended_entry['seen_frames']} "
                    f"traveled_up={ended_entry['traveled_up']}"
                )

            print(f"ENDED bubble {ended_entry['id']} counted={ended_entry['counted']}")

        # ---- DRAW ACTIVE BUBBLE ----
        if active_bubble is not None:
            cx = active_bubble["cx"]
            cy = active_bubble["cy"]
            r = active_bubble["r"]
            bid = active_bubble["id"]

            color = (0, 255, 0)

            cv2.circle(frame, (cx, cy), int(r), color, 2)
            cv2.circle(frame, (cx, cy), 3, color, -1)

            label = f"ID {bid} TRACKING"

            cv2.putText(
                frame, label, (cx + 10, cy),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1
            )

        # ---- OVERLAY ----
        fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))

        overlay = frame.copy()
        box_width = 330
        box_height = 160
        ox1 = frame.shape[1] - box_width - 10
        oy1 = 10
        ox2 = frame.shape[1] - 10
        oy2 = oy1 + box_height

        cv2.rectangle(overlay, (ox1, oy1), (ox2, oy2), (0, 0, 0), -1)
        frame = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0)

        y = oy1 + 22
        line_height = 20
        texts = [
            f"Video: {current_video_name()}",
            f"FPS: {fps:.1f}",
            f"Frame: {frame_count}",
            f"Bubble: {1 if active_bubble is not None else 0}",
            f"Count: {bubble_count_total}",
            f"Fallback bias: {PIPE_CENTER_X_BIAS}",
            f"Auto center: {'ON' if AUTO_CENTER_ENABLED else 'OFF'}",
        ]

        for text in texts:
            cv2.putText(
                frame, text, (ox1 + 10, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2
            )
            y += line_height

        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
        if not ok:
            continue

        frame_bytes = buffer.tobytes()
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" +
            frame_bytes + b"\r\n"
        )

        elapsed = time.time() - start_time
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)


@app.route("/video_feed")
def video_feed():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/toggle_debug", methods=["POST"])
def toggle_debug():
    global DEBUG_DRAW
    DEBUG_DRAW = not DEBUG_DRAW
    return jsonify({"success": True, "debug": DEBUG_DRAW})


@app.route("/reset_counter", methods=["POST"])
def reset_counter():
    reset_tracker_runtime_state(reset_counts=True)
    return jsonify({"success": True})


@app.route("/status")
def status():
    return jsonify({
        "video": current_video_name(),
        "debug": DEBUG_DRAW,
        "count": bubble_count_total,
        "active_bubble": active_bubble is not None,
        "auto_center": AUTO_CENTER_ENABLED,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bubble video stream test harness")
    parser.add_argument(
        "--video",
        default=DEFAULT_VIDEO_PATH,
        help="Video file to play through the test harness (default: %(default)s)",
    )
    parser.add_argument(
        "--profile",
        help=(
            "Profile file to load. Defaults to profiles/<video_stem>.json "
            "or profiles/Bubbles.json."
        ),
    )
    args = parser.parse_args()

    atexit.register(cleanup_video_source)

    validated_video_path = validate_video_path(args.video)
    resolved_profile_path = resolve_profile_path(
        validated_video_path,
        explicit_profile_path=args.profile,
    )
    apply_test_video_profile(resolved_profile_path)
    load_runtime_dependencies()
    configure_video_source(validated_video_path)

    app.run(host="0.0.0.0", port=5001, threaded=True)
