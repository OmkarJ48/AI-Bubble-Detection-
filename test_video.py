import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import argparse
import atexit
import json
import time
from pathlib import Path

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

DEFAULT_VIDEO_PATH = "Bubbles.mp4"
VIDEO_PATH = DEFAULT_VIDEO_PATH
PROFILE_PATH = None
LOG_PATH = "bubble_log.jsonl"

TARGET_FPS = 20
LOCK_AFTER_FRAMES = 2
LOST_AFTER_FRAMES = 3
MIN_TRAVEL_Y = 18

# ---- PIPE TUNING ----
PIPE_CENTER_X_BIAS = -50
PIPE_WIDTH_RATIO = 0.35
PIPE_LOCK_WIDTH_RATIO = 0.18
PIPE_TOP_RATIO = 0.25
PIPE_BOTTOM_RATIO = 0.75
PIPE_EXIT_RATIO = 0.55

SPAWN_BAND_HALF = 22
COUNT_BAND_HALF = 12
COUNT_BAND_OFFSET = 0
MIN_START_BELOW_EXIT = 18

DETECT_EVERY_SECONDS = 0.10
MIN_RADIUS = 10
MAX_RADIUS = 20
MAX_MATCH_DISTANCE = 60
DOWNWARD_TOLERANCE = 10
MAX_LATERAL_SHIFT = 35
MAX_STEP_DISTANCE = 80
CANDIDATE_CONFIRM_FRAMES = 2
CANDIDATE_MATCH_DISTANCE = 50
CANDIDATE_LOST_AFTER_FRAMES = 1

AUTO_CENTER_ENABLED = True
AUTO_CENTER_SMOOTHING = 0.80
CENTER_SEARCH_WIDTH_RATIO = 0.35
AUTO_CENTER_MAX_OFFSET_PX = 120

DEBUG_DRAW = True


app = Flask(__name__)
cap = None
tracker = PrecisionBubbleTracker(TrackerConfig())
last_detect_time = 0.0
dynamic_pipe_center_x = None
last_detection_snapshot = {
    "raw_detections": [],
    "filtered_detections": [],
    "start_candidates": [],
    "state": "idle",
    "started_id": None,
    "counted": False,
    "ended_event": None,
}


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
                        'Debug: ' + (data.debug ? 'ON' : 'OFF') +
                        ' | Count: ' + data.count +
                        ' | State: ' + data.state +
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
    "min_radius": "MIN_RADIUS",
    "max_radius": "MAX_RADIUS",
    "max_match_distance": "MAX_MATCH_DISTANCE",
    "downward_tolerance": "DOWNWARD_TOLERANCE",
    "spawn_band_half": "SPAWN_BAND_HALF",
    "min_start_below_exit": "MIN_START_BELOW_EXIT",
    "candidate_confirm_frames": "CANDIDATE_CONFIRM_FRAMES",
    "candidate_match_distance": "CANDIDATE_MATCH_DISTANCE",
    "candidate_lost_after_frames": "CANDIDATE_LOST_AFTER_FRAMES",
    "max_lateral_shift": "MAX_LATERAL_SHIFT",
    "max_step_distance": "MAX_STEP_DISTANCE",
    "auto_center_enabled": "AUTO_CENTER_ENABLED",
    "auto_center_smoothing": "AUTO_CENTER_SMOOTHING",
    "center_search_width_ratio": "CENTER_SEARCH_WIDTH_RATIO",
    "auto_center_max_offset_px": "AUTO_CENTER_MAX_OFFSET_PX",
    "count_band_offset": "COUNT_BAND_OFFSET",
    "debug_draw": "DEBUG_DRAW",
}


def build_tracker() -> PrecisionBubbleTracker:
    return PrecisionBubbleTracker(
        TrackerConfig(
            lock_after_frames=int(LOCK_AFTER_FRAMES),
            lost_after_frames=int(LOST_AFTER_FRAMES),
            min_upward_travel=int(MIN_TRAVEL_Y),
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


def validate_video_path(video_path):
    resolved_path = Path(video_path)
    if not resolved_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")
    return str(resolved_path)


def apply_test_video_profile(profile_path):
    global PROFILE_PATH
    PROFILE_PATH = profile_path
    profile_data = load_profile_data(PROFILE_PATH)
    apply_profile_mapping(profile_data.get("shared", {}), SHARED_PROFILE_FIELDS, globals())
    apply_profile_mapping(profile_data.get("test_video", {}), TEST_VIDEO_PROFILE_FIELDS, globals())


def cleanup_video_capture():
    global cap
    if cap is not None:
        cap.release()
        cap = None


def log_bubble_event(entry):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry) + "\n")
    except Exception as e:
        print("Failed to write log:", e)


def generate_frames():
    global last_detect_time, dynamic_pipe_center_x, tracker, last_detection_snapshot

    frame_count = 0
    frame_interval = 1.0 / TARGET_FPS

    while True:
        start_time = time.time()
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            tracker.reset()
            dynamic_pipe_center_x = None
            last_detection_snapshot = {
                "raw_detections": [],
                "filtered_detections": [],
                "start_candidates": [],
                "state": "idle",
                "started_id": None,
                "counted": False,
                "ended_event": None,
            }
            continue

        frame_count += 1
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = 0, 0, w, h
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
        if frame_count > 8 and (now - last_detect_time > DETECT_EVERY_SECONDS):
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
                print(f"START bubble {last_detection_snapshot['started_id']}")
            if last_detection_snapshot["counted"]:
                print(f"COUNTED total={tracker.bubble_count_total}")
            if last_detection_snapshot["ended_event"] is not None:
                log_bubble_event(last_detection_snapshot["ended_event"])
                print(
                    "ENDED bubble "
                    f"{last_detection_snapshot['ended_event']['id']} "
                    f"counted={last_detection_snapshot['ended_event']['counted']}"
                )

        if DEBUG_DRAW:
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
            cv2.putText(frame, "SPAWN BAND", (x1 + 10, spawn_y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)
            cv2.rectangle(frame, (x1, count_y1), (x2, count_y2), (0, 0, 255), 1)
            cv2.putText(frame, "COUNT BAND", (x1 + 10, count_y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

            for (cx, cy, r) in last_detection_snapshot["filtered_detections"]:
                cv2.circle(frame, (cx, cy), int(max(6, r)), (0, 255, 255), 2)

        if tracker.active_bubble is not None:
            bubble = tracker.active_bubble
            cx = bubble["cx"]
            cy = bubble["cy"]
            r = int(max(8, bubble["r"]))
            cv2.circle(frame, (cx, cy), r, (0, 255, 0), 2)
            cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)
            cv2.putText(frame, f"ID {bubble['id']} TRACKING", (cx + 10, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        elif tracker.candidate_bubble is not None and DEBUG_DRAW:
            cand = tracker.candidate_bubble
            cv2.circle(frame, (cand["cx"], cand["cy"]), int(max(6, cand["r"])), (255, 165, 0), 1)
            cv2.putText(
                frame,
                f"CAND {cand['seen_frames']}/{CANDIDATE_CONFIRM_FRAMES}",
                (cand["cx"] + 10, cand["cy"] - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (255, 165, 0),
                1,
            )

        fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))
        overlay = frame.copy()
        box_width = 310
        box_height = 170
        ox1 = frame.shape[1] - box_width - 10
        oy1 = 10
        ox2 = frame.shape[1] - 10
        oy2 = oy1 + box_height
        cv2.rectangle(overlay, (ox1, oy1), (ox2, oy2), (0, 0, 0), -1)
        frame = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0)

        y = oy1 + 22
        for text in [
            f"FPS: {fps:.1f}",
            f"Frame: {frame_count}",
            f"State: {tracker.state}",
            f"Bubble: {1 if tracker.active_bubble is not None else 0}",
            f"Candidate: {1 if tracker.candidate_bubble is not None else 0}",
            f"Count: {tracker.bubble_count_total}",
            f"Auto center: {'ON' if AUTO_CENTER_ENABLED else 'OFF'}",
            f"Video: {Path(VIDEO_PATH).name}",
        ]:
            cv2.putText(frame, text, (ox1 + 10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
            y += 20

        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
        if not ok:
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
        )

        elapsed = time.time() - start_time
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)


@app.route("/video_feed")
def video_feed():
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


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
    global tracker, last_detection_snapshot, dynamic_pipe_center_x, last_detect_time
    tracker.reset()
    dynamic_pipe_center_x = None
    last_detect_time = 0.0
    last_detection_snapshot = {
        "raw_detections": [],
        "filtered_detections": [],
        "start_candidates": [],
        "state": "idle",
        "started_id": None,
        "counted": False,
        "ended_event": None,
    }
    return jsonify({"success": True})


@app.route("/status")
def status():
    return jsonify(
        {
            "video": VIDEO_PATH,
            "profile": PROFILE_PATH,
            "debug": DEBUG_DRAW,
            "count": tracker.bubble_count_total,
            "state": tracker.state,
            "active_bubble": tracker.active_bubble is not None,
            "candidate_bubble": tracker.candidate_bubble is not None,
            "auto_center": AUTO_CENTER_ENABLED,
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bubble video stream test harness")
    parser.add_argument("--video", default=DEFAULT_VIDEO_PATH, help="Video file path to stream")
    parser.add_argument(
        "--profile",
        default=None,
        help="Profile file path (default: profiles/<video_stem>.json, fallback profiles/Bubbles.json)",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()

    VIDEO_PATH = validate_video_path(args.video)
    resolved_profile_path = resolve_profile_path(VIDEO_PATH, args.profile)
    if resolved_profile_path is not None:
        apply_test_video_profile(resolved_profile_path)

    tracker = build_tracker()

    cap = cv2.VideoCapture(VIDEO_PATH)
    if not cap.isOpened():
        raise SystemExit(f"Failed to open video: {VIDEO_PATH}")

    atexit.register(cleanup_video_capture)
    print(f"Using video: {VIDEO_PATH}")
    print(f"Using profile: {PROFILE_PATH or 'none'}")
    app.run(host=args.host, port=args.port, threaded=True)
