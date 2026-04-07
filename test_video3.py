import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import argparse
import atexit
import json
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string

cv2 = None
np = None
detect_bubbles = None

DEFAULT_VIDEO_PATH = "Bubbles2.mp4"
VIDEO_PATH = DEFAULT_VIDEO_PATH
LOG_PATH = "bubble_log_Bubbles2.jsonl"
PROFILE_PATH = None

TARGET_FPS = 20
LOCK_AFTER_FRAMES = 2
LOST_AFTER_FRAMES = 4
MIN_TRAVEL_Y = 22

# ---- PIPE TUNING FOR Bubbles2.mp4 ----
# tuned from your screenshot
PIPE_CENTER_X_BIAS = -25

PIPE_WIDTH_RATIO = 0.22
PIPE_LOCK_WIDTH_RATIO = 0.08

PIPE_TOP_RATIO = 0.28
PIPE_BOTTOM_RATIO = 0.72
PIPE_EXIT_RATIO = 0.54

COUNT_BAND_HALF = 12

DETECT_EVERY_SECONDS = 0.08

MIN_RADIUS = 8
MAX_RADIUS = 22

MAX_MATCH_DISTANCE = 50

# KEEP AUTO-CENTER ON, but make it much tighter
AUTO_CENTER_ENABLED = True
AUTO_CENTER_SMOOTHING = 0.93
CENTER_SEARCH_WIDTH_RATIO = 0.16
AUTO_CENTER_MAX_OFFSET_PX = 45

DEBUG_DRAW = True

# ---- NOISE / FALSE-START CONTROL ----
STARTUP_IGNORE_FRAMES = 8
CANDIDATE_CONFIRM_FRAMES = 3
CANDIDATE_MATCH_DISTANCE = 30

# bubble must start very close to pipe exit line
SPAWN_BAND_TOP = 6
SPAWN_BAND_BOTTOM = 10

# reject detections that jump too far downward during tracking
DOWNWARD_TOLERANCE = 6

app = Flask(__name__)
cap = None

active_bubble = None
candidate_bubble = None
bubble_history = []
next_bubble_id = 1
bubble_count_total = 0
last_detect_time = 0.0
dynamic_pipe_center_x = None

state_lock = threading.Lock()

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
    <h1>Bubble Video Stream Test - Bubbles2</h1>

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
                        ' | Candidate: ' + (data.candidate_bubble ? 'YES' : 'NO') +
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
    "min_travel_y": "MIN_TRAVEL_Y",
    "detect_every_seconds": "DETECT_EVERY_SECONDS",
    "startup_ignore_frames": "STARTUP_IGNORE_FRAMES",
    "min_radius": "MIN_RADIUS",
    "max_radius": "MAX_RADIUS",
    "max_match_distance": "MAX_MATCH_DISTANCE",
    "candidate_confirm_frames": "CANDIDATE_CONFIRM_FRAMES",
    "candidate_match_distance": "CANDIDATE_MATCH_DISTANCE",
    "spawn_band_top": "SPAWN_BAND_TOP",
    "spawn_band_bottom": "SPAWN_BAND_BOTTOM",
    "downward_tolerance": "DOWNWARD_TOLERANCE",
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


def validate_bg_profile_path(profile_path):
    resolved_path = Path(profile_path)
    if not resolved_path.is_file():
        raise SystemExit(f"Profile file not found: {profile_path}")
    return str(resolved_path)


def resolve_profile_path(video_path, explicit_profile_path=None):
    if explicit_profile_path is not None:
        return validate_bg_profile_path(explicit_profile_path)

    default_profile = build_default_profile_path(video_path)
    if default_profile.is_file():
        return str(default_profile)

    fallback_profile = Path("profiles") / "Bubbles2.json"
    if fallback_profile.is_file():
        return str(fallback_profile)

    return None


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

    if profile_path is None:
        return

    PROFILE_PATH = validate_bg_profile_path(profile_path)
    profile_data = load_profile_data(PROFILE_PATH)

    apply_profile_mapping(profile_data.get("shared", {}), SHARED_PROFILE_FIELDS)
    apply_profile_mapping(profile_data.get("test_video", {}), TEST_VIDEO_PROFILE_FIELDS)


def load_runtime_dependencies():
    global cv2, np, detect_bubbles

    import cv2 as cv2_module
    import numpy as np_module
    from livestream import detect_bubbles as detect_bubbles_func

    cv2 = cv2_module
    np = np_module
    detect_bubbles = detect_bubbles_func


def configure_video_source(video_path):
    global VIDEO_PATH, LOG_PATH, cap

    VIDEO_PATH = validate_video_path(video_path)
    LOG_PATH = build_log_path(VIDEO_PATH)

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


def estimate_pipe_center_x(gray_roi, fallback_center_x, previous_center_x=None):
    """
    Estimate pipe center using only the vertical band where the pipe exists.
    This prevents locking onto background reflections.
    """
    roi_h, roi_w = gray_roi.shape[:2]

    y_top = max(0, int(roi_h * PIPE_TOP_RATIO))
    y_bottom = min(roi_h, int(roi_h * PIPE_BOTTOM_RATIO))
    band = gray_roi[y_top:y_bottom, :]
    if band.size == 0:
        return fallback_center_x

    search_half_width = max(12, int(roi_w * CENTER_SEARCH_WIDTH_RATIO / 2))
    fallback_local_x = fallback_center_x

    x_left = max(0, fallback_local_x - search_half_width)
    x_right = min(roi_w, fallback_local_x + search_half_width)

    search = band[:, x_left:x_right]
    if search.size == 0:
        return fallback_center_x

    search_blur = cv2.GaussianBlur(search, (5, 5), 0)
    edges = cv2.Canny(search_blur, 60, 150)

    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=28,
        minLineLength=max(18, int((y_bottom - y_top) * 0.35)),
        maxLineGap=10
    )

    candidate_xs = []

    if lines is not None:
        for line in lines[:, 0]:
            x1, y1, x2, y2 = line
            dx = abs(x2 - x1)
            dy = abs(y2 - y1)

            if dy >= 18 and dx <= 8:
                length = float(np.hypot(x2 - x1, y2 - y1))
                center_x = int((x1 + x2) / 2) + x_left
                candidate_xs.append((length, center_x))

    if candidate_xs:
        candidate_xs.sort(reverse=True, key=lambda item: item[0])
        best = candidate_xs[:3]
        estimated = int(np.mean([x for _, x in best]))
    else:
        estimated = fallback_center_x

    min_center_x = fallback_local_x - AUTO_CENTER_MAX_OFFSET_PX
    max_center_x = fallback_local_x + AUTO_CENTER_MAX_OFFSET_PX
    estimated = max(min_center_x, min(max_center_x, estimated))

    if previous_center_x is None:
        return estimated

    smoothed = int(
        AUTO_CENTER_SMOOTHING * previous_center_x +
        (1.0 - AUTO_CENTER_SMOOTHING) * estimated
    )
    return smoothed


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
        "counted": False,
    }


def generate_frames():
    global active_bubble
    global candidate_bubble
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
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            with state_lock:
                active_bubble = None
                candidate_bubble = None
            continue

        frame_count += 1
        h, w = frame.shape[:2]

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

        fallback_pipe_center_x = (roi_w // 2) + PIPE_CENTER_X_BIAS

        if AUTO_CENTER_ENABLED:
            estimated_local_center_x = estimate_pipe_center_x(
                gray_roi=gray,
                fallback_center_x=fallback_pipe_center_x,
                previous_center_x=dynamic_pipe_center_x
            )
            dynamic_pipe_center_x = estimated_local_center_x
            pipe_center_x = x_offset + dynamic_pipe_center_x
        else:
            dynamic_pipe_center_x = fallback_pipe_center_x
            pipe_center_x = x_offset + fallback_pipe_center_x

        pipe_width = int(roi_w * PIPE_WIDTH_RATIO)
        pipe_lock_width = int(roi_w * PIPE_LOCK_WIDTH_RATIO)

        pipe_top = y_offset + int(roi_h * PIPE_TOP_RATIO)
        pipe_bottom = y_offset + int(roi_h * PIPE_BOTTOM_RATIO)
        pipe_exit_y = y_offset + int(roi_h * PIPE_EXIT_RATIO)

        count_y1 = pipe_exit_y - COUNT_BAND_HALF
        count_y2 = pipe_exit_y + COUNT_BAND_HALF

        spawn_y1 = pipe_exit_y - SPAWN_BAND_TOP
        spawn_y2 = pipe_exit_y + SPAWN_BAND_BOTTOM

        if DEBUG_DRAW:
            cv2.line(frame, (pipe_center_x, y1), (pipe_center_x, y2), (255, 255, 0), 2)

            cv2.rectangle(
                frame,
                (pipe_center_x - pipe_width, pipe_top),
                (pipe_center_x + pipe_width, pipe_bottom),
                (255, 0, 255), 2
            )

            cv2.rectangle(
                frame,
                (pipe_center_x - pipe_lock_width, pipe_top),
                (pipe_center_x + pipe_lock_width, pipe_bottom),
                (0, 255, 255), 1
            )

            cv2.line(frame, (x1, pipe_exit_y), (x2, pipe_exit_y), (0, 0, 255), 2)
            cv2.rectangle(frame, (x1, count_y1), (x2, count_y2), (0, 100, 255), 1)
            cv2.rectangle(frame, (x1, spawn_y1), (x2, spawn_y2), (255, 165, 0), 1)

            cv2.putText(
                frame, "COUNT BAND", (x1 + 10, count_y1 - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1
            )
            cv2.putText(
                frame, "SPAWN BAND", (x1 + 10, spawn_y1 - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 165, 0), 1
            )

        detections = []

        if frame_count > STARTUP_IGNORE_FRAMES and (time.time() - last_detect_time > DETECT_EVERY_SECONDS):
            detections = detect_bubbles(
                small_gray=small,
                scale_x=roi_w / 64,
                scale_y=roi_h / 48,
                x_offset=x_offset,
                y_offset=y_offset
            )
            last_detect_time = time.time()

            detections = [
                (cx, cy, r)
                for (cx, cy, r) in detections
                if MIN_RADIUS <= r <= MAX_RADIUS
            ]

            detections = [
                (cx, cy, r)
                for (cx, cy, r) in detections
                if abs(cx - pipe_center_x) <= pipe_lock_width and pipe_top <= cy <= pipe_bottom
            ]

            with state_lock:
                local_active = None if active_bubble is None else dict(active_bubble)

            if local_active is None:
                detections = [
                    (cx, cy, r)
                    for (cx, cy, r) in detections
                    if spawn_y1 <= cy <= spawn_y2
                ]
                detections = sorted(
                    detections,
                    key=lambda d: (
                        abs(d[0] - pipe_center_x),
                        abs(d[1] - pipe_exit_y),
                        -d[2]
                    )
                )
            else:
                detections = [
                    (cx, cy, r)
                    for (cx, cy, r) in detections
                    if cy <= (local_active["cy"] + DOWNWARD_TOLERANCE)
                ]
                detections = sorted(
                    detections,
                    key=lambda d: distance((d[0], d[1]), (local_active["cx"], local_active["cy"]))
                )

        if DEBUG_DRAW:
            for (cx, cy, r) in detections:
                cv2.circle(frame, (cx, cy), int(r), (0, 255, 255), 2)

        now = time.time()

        with state_lock:
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
                        print(f"START bubble {next_bubble_id} at ({active_bubble['cx']}, {active_bubble['cy']})")
                        next_bubble_id += 1
                        candidate_bubble = None
                else:
                    candidate_bubble = None
            else:
                best_det = None
                best_dist = 999999.0

                for det in detections:
                    cx, cy, r = det
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

        with state_lock:
            active_snapshot = None if active_bubble is None else dict(active_bubble)

        if active_snapshot is not None:
            bubble_id = active_snapshot["id"]
            cx = active_snapshot["cx"]
            cy = active_snapshot["cy"]

            travel_y = active_snapshot["start_y"] - active_snapshot["min_y"]

            if (
                not active_snapshot["counted"]
                and active_snapshot["seen_frames"] >= LOCK_AFTER_FRAMES
                and travel_y >= MIN_TRAVEL_Y
                and count_y1 <= cy <= count_y2
            ):
                with state_lock:
                    if active_bubble is not None and not active_bubble["counted"]:
                        bubble_count_total += 1
                        active_bubble["counted"] = True
                        print(f"COUNTED bubble {bubble_id} total={bubble_count_total}")

        with state_lock:
            should_end = active_bubble is not None and active_bubble["lost_frames"] >= LOST_AFTER_FRAMES

        if should_end:
            with state_lock:
                ended = dict(active_bubble)
            ended_entry = {
                "id": ended["id"],
                "counted": ended["counted"],
                "start_x": ended["start_x"],
                "start_y": ended["start_y"],
                "end_x": ended["cx"],
                "end_y": ended["cy"],
                "min_y": ended["min_y"],
                "seen_frames": ended["seen_frames"],
                "ended_at": now,
            }

            bubble_history.append(ended_entry)
            log_bubble_event(ended_entry)

            print(f"ENDED bubble {ended['id']} counted={ended['counted']}")
            with state_lock:
                active_bubble = None
                candidate_bubble = None

        with state_lock:
            active_draw = None if active_bubble is None else dict(active_bubble)
            candidate_draw = None if candidate_bubble is None else dict(candidate_bubble)
            count_draw = bubble_count_total

        if active_draw is not None:
            cx = active_draw["cx"]
            cy = active_draw["cy"]
            r = active_draw["r"]
            bid = active_draw["id"]

            color = (0, 255, 0) if active_draw["counted"] else (0, 255, 255)

            cv2.circle(frame, (cx, cy), int(r), color, 2)
            cv2.circle(frame, (cx, cy), 3, color, -1)

            label = f"ID {bid}"
            label += " COUNTED" if active_draw["counted"] else " TRACKING"

            cv2.putText(
                frame, label, (cx + 10, cy),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1
            )

        fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))

        overlay = frame.copy()
        box_width = 360
        box_height = 180
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
            f"Bubble: {1 if active_draw is not None else 0}",
            f"Candidate: {1 if candidate_draw is not None else 0}",
            f"Count: {count_draw}",
            f"Bias: {PIPE_CENTER_X_BIAS}",
            f"Center X: {pipe_center_x}",
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
    with state_lock:
        DEBUG_DRAW = not DEBUG_DRAW
        debug = DEBUG_DRAW
    return jsonify({"success": True, "debug": debug})


@app.route("/reset_counter", methods=["POST"])
def reset_counter():
    global bubble_count_total, bubble_history, active_bubble, candidate_bubble
    with state_lock:
        bubble_count_total = 0
        bubble_history = []
        active_bubble = None
        candidate_bubble = None
    return jsonify({"success": True})


@app.route("/status")
def status():
    with state_lock:
        debug = DEBUG_DRAW
        count = bubble_count_total
        active = active_bubble is not None
        candidate = candidate_bubble is not None
    return jsonify({
        "video": current_video_name(),
        "debug": debug,
        "count": count,
        "active_bubble": active,
        "candidate_bubble": candidate,
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
            "or profiles/Bubbles2.json."
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