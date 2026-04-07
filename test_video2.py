import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import argparse
import atexit
import json
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string

from livestream import detect_bubbles

cv2 = None
np = None


def ensure_runtime_dependencies():
    global cv2, np
    if cv2 is None:
        import cv2 as cv2_module
        cv2 = cv2_module
    if np is None:
        import numpy as np_module
        np = np_module


DEFAULT_VIDEO_PATH = "Bubbles.mp4"
VIDEO_PATH = DEFAULT_VIDEO_PATH
LOG_PATH = "bubble_log_Bubbles2_mode.jsonl"
PROFILE_PATH = None

TARGET_FPS = 20
LOCK_AFTER_FRAMES = 2
LOST_AFTER_FRAMES = 3
MIN_TRAVEL_Y = 18

PIPE_CENTER_X_BIAS = -50
PIPE_WIDTH_RATIO = 0.35
PIPE_LOCK_WIDTH_RATIO = 0.18
PIPE_TOP_RATIO = 0.25
PIPE_BOTTOM_RATIO = 0.75
PIPE_EXIT_RATIO = 0.55
COUNT_BAND_HALF = 12

DETECT_EVERY_SECONDS = 0.10
MIN_RADIUS = 10
MAX_RADIUS = 20
MAX_MATCH_DISTANCE = 60

AUTO_CENTER_ENABLED = True
AUTO_CENTER_SMOOTHING = 0.85
CENTER_SEARCH_WIDTH_RATIO = 0.35
AUTO_CENTER_MAX_OFFSET_PX = 120

DEBUG_DRAW = True
STARTUP_IGNORE_FRAMES = 8
CANDIDATE_CONFIRM_FRAMES = 2
CANDIDATE_MATCH_DISTANCE = 50
MIN_START_BELOW_EXIT = 16

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
    "auto_center_enabled": "AUTO_CENTER_ENABLED",
    "auto_center_smoothing": "AUTO_CENTER_SMOOTHING",
    "center_search_width_ratio": "CENTER_SEARCH_WIDTH_RATIO",
    "auto_center_max_offset_px": "AUTO_CENTER_MAX_OFFSET_PX",
    "debug_draw": "DEBUG_DRAW",
}

app = Flask(__name__)
cap = None
active_bubble = None
candidate_bubble = None
bubble_history = []
next_bubble_id = 1
bubble_count_total = 0
last_detect_time = 0.0
dynamic_pipe_center_x = None

HTML_PAGE = """
<!DOCTYPE html>
<html><head><title>Bubble Video Stream Test 2</title><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>body{font-family:Arial,sans-serif;text-align:center;background:#111;color:white;margin:0;padding:20px}.toolbar{margin-bottom:16px}button{padding:10px 16px;margin:0 6px;border:none;border-radius:6px;cursor:pointer;font-weight:bold}.btn{background:#3498db;color:white}.btn:hover{background:#2980b9}.status{margin-top:10px;font-size:14px;color:#9f9f9f}img{max-width:95%;border:2px solid #444;border-radius:8px}</style>
</head><body>
<h1>Bubble Video Stream Test 2</h1>
<div class="toolbar"><button class="btn" onclick="toggleDebug()">Toggle Debug Overlay</button><button class="btn" onclick="resetCounter()">Reset Counter</button></div>
<div class="status" id="status">Loading...</div>
<img id="stream" src="/video_feed" alt="Bubble stream">
<script>
function refreshStatus(){fetch('/status').then(r=>r.json()).then(data=>{document.getElementById('status').textContent='Video: '+data.video+' | Debug: '+(data.debug?'ON':'OFF')+' | Count: '+data.count+' | Active bubble: '+(data.active_bubble?'YES':'NO')+' | Candidate: '+(data.candidate_bubble?'YES':'NO')+' | Auto center: '+(data.auto_center?'ON':'OFF');}).catch(()=>{document.getElementById('status').textContent='Status unavailable';});}
function toggleDebug(){fetch('/toggle_debug',{method:'POST'}).then(()=>refreshStatus());}
function resetCounter(){fetch('/reset_counter',{method:'POST'}).then(()=>refreshStatus());}
setInterval(refreshStatus,1000);refreshStatus();
</script></body></html>
"""


def build_default_profile_path(video_path):
    return Path("profiles") / f"{Path(video_path).stem}.json"


def validate_video_path(video_path):
    p = Path(video_path)
    if not p.is_file():
        raise SystemExit(f"Video file not found: {video_path}")
    return str(p)


def validate_profile_path(profile_path):
    p = Path(profile_path)
    if not p.is_file():
        raise SystemExit(f"Profile file not found: {profile_path}")
    return str(p)


def resolve_profile_path(video_path, explicit_profile_path=None):
    if explicit_profile_path:
        return validate_profile_path(explicit_profile_path)
    default = build_default_profile_path(video_path)
    if default.is_file():
        return str(default)
    fallback = Path("profiles") / "Bubbles.json"
    if fallback.is_file():
        return str(fallback)
    return None


def load_profile_data(profile_path):
    with open(profile_path, "r", encoding="utf-8") as f:
        data = json.load(f)
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
    data = load_profile_data(PROFILE_PATH)
    apply_profile_mapping(data.get("shared", {}), SHARED_PROFILE_FIELDS)
    apply_profile_mapping(data.get("test_video", {}), TEST_VIDEO_PROFILE_FIELDS)


def distance(p1, p2):
    return float(np.hypot(p1[0] - p2[0], p1[1] - p2[1]))


def log_bubble_event(entry):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print("Failed to write log:", e)


def estimate_pipe_center_x(gray_roi, fallback_center_x, previous_center_x=None):
    roi_h, roi_w = gray_roi.shape[:2]
    search_half_width = int(roi_w * CENTER_SEARCH_WIDTH_RATIO / 2)
    x_left = max(0, fallback_center_x - search_half_width)
    x_right = min(roi_w, fallback_center_x + search_half_width)
    search = gray_roi[:, x_left:x_right]
    if search.size == 0:
        return fallback_center_x

    edges = cv2.Canny(search, 50, 150)
    lines = cv2.HoughLinesP(edges, rho=1, theta=np.pi / 180, threshold=40, minLineLength=max(20, int(roi_h * 0.25)), maxLineGap=15)

    candidate_xs = []
    if lines is not None:
        for line in lines[:, 0]:
            x1, y1, x2, y2 = line
            if abs(y2 - y1) > 20 and abs(x2 - x1) <= 12:
                length = np.hypot(x2 - x1, y2 - y1)
                center_x = int((x1 + x2) / 2) + x_left
                candidate_xs.append((length, center_x))

    estimated = fallback_center_x
    if candidate_xs:
        candidate_xs.sort(reverse=True, key=lambda item: item[0])
        best = candidate_xs[:2]
        estimated = int(np.mean([x for _, x in best]))
        estimated = max(
            fallback_center_x - AUTO_CENTER_MAX_OFFSET_PX,
            min(fallback_center_x + AUTO_CENTER_MAX_OFFSET_PX, estimated),
        )

    if previous_center_x is None:
        return estimated
    return int(AUTO_CENTER_SMOOTHING * previous_center_x + (1.0 - AUTO_CENTER_SMOOTHING) * estimated)


def begin_candidate(cx, cy, r, now):
    return {"cx": cx, "cy": cy, "r": r, "first_seen": now, "last_seen": now, "seen_frames": 1}


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
    global active_bubble, candidate_bubble, bubble_history, next_bubble_id
    global bubble_count_total, last_detect_time, dynamic_pipe_center_x

    frame_count = 0
    frame_interval = 1.0 / TARGET_FPS

    while True:
        start_time = time.time()
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            active_bubble = None
            candidate_bubble = None
            continue

        frame_count += 1
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (7, 7), 0)
        small = cv2.resize(blur, (64, 48), interpolation=cv2.INTER_AREA)
        small = cv2.equalizeHist(small)

        fallback_center_x = (w // 2) + PIPE_CENTER_X_BIAS
        if AUTO_CENTER_ENABLED:
            dynamic_pipe_center_x = estimate_pipe_center_x(gray, fallback_center_x, dynamic_pipe_center_x)
            pipe_center_x = dynamic_pipe_center_x
        else:
            pipe_center_x = fallback_center_x

        pipe_width = int(w * PIPE_WIDTH_RATIO)
        pipe_lock_width = int(w * PIPE_LOCK_WIDTH_RATIO)
        pipe_top = int(h * PIPE_TOP_RATIO)
        pipe_bottom = int(h * PIPE_BOTTOM_RATIO)
        pipe_exit_y = int(h * PIPE_EXIT_RATIO)
        spawn_y1 = pipe_exit_y - COUNT_BAND_HALF
        spawn_y2 = pipe_exit_y + COUNT_BAND_HALF
        count_y1 = pipe_exit_y - COUNT_BAND_HALF
        count_y2 = pipe_exit_y + COUNT_BAND_HALF

        detections = []
        if frame_count > STARTUP_IGNORE_FRAMES and (time.time() - last_detect_time > DETECT_EVERY_SECONDS):
            detections = detect_bubbles(small_gray=small, scale_x=w / 64, scale_y=h / 48, x_offset=0, y_offset=0)
            last_detect_time = time.time()
            detections = [(cx, cy, r) for (cx, cy, r) in detections if MIN_RADIUS <= r <= MAX_RADIUS]
            detections = [(cx, cy, r) for (cx, cy, r) in detections if abs(cx - pipe_center_x) <= pipe_lock_width and pipe_top <= cy <= pipe_bottom]
            if active_bubble is None:
                detections = [(cx, cy, r) for (cx, cy, r) in detections if spawn_y1 <= cy <= spawn_y2 and cy >= (pipe_exit_y + MIN_START_BELOW_EXIT)]
                detections = sorted(detections, key=lambda d: (abs(d[0] - pipe_center_x), -d[2]))
            else:
                detections = sorted(detections, key=lambda d: distance((d[0], d[1]), (active_bubble["cx"], active_bubble["cy"])))

        now = time.time()
        if active_bubble is None:
            if detections:
                cx, cy, r = detections[0]
                if candidate_bubble is None:
                    candidate_bubble = begin_candidate(cx, cy, r, now)
                else:
                    if distance((cx, cy), (candidate_bubble["cx"], candidate_bubble["cy"])) < CANDIDATE_MATCH_DISTANCE:
                        candidate_bubble["cx"] = int(0.7 * candidate_bubble["cx"] + 0.3 * cx)
                        candidate_bubble["cy"] = int(0.7 * candidate_bubble["cy"] + 0.3 * cy)
                        candidate_bubble["r"] = r
                        candidate_bubble["seen_frames"] += 1
                        candidate_bubble["last_seen"] = now
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
            best_dist = 1e9
            for det in detections:
                d = distance((det[0], det[1]), (active_bubble["cx"], active_bubble["cy"]))
                if d < best_dist:
                    best_dist, best_det = d, det

            if best_det is not None and best_dist < MAX_MATCH_DISTANCE:
                cx, cy, r = best_det
                active_bubble["cx"] = int(0.7 * active_bubble["cx"] + 0.3 * cx)
                active_bubble["cy"] = int(0.7 * active_bubble["cy"] + 0.3 * cy)
                active_bubble["r"] = r
                active_bubble["min_y"] = min(active_bubble["min_y"], active_bubble["cy"])
                active_bubble["seen_frames"] += 1
                active_bubble["lost_frames"] = 0
            else:
                active_bubble["lost_frames"] += 1

        if active_bubble is not None and active_bubble["lost_frames"] >= LOST_AFTER_FRAMES:
            traveled_up = active_bubble["start_y"] - active_bubble["min_y"]
            should_count = active_bubble["seen_frames"] >= LOCK_AFTER_FRAMES and traveled_up >= MIN_TRAVEL_Y
            if should_count:
                bubble_count_total += 1
                active_bubble["counted"] = True

            ended = {
                "id": active_bubble["id"], "counted": should_count, "start_x": active_bubble["start_x"],
                "start_y": active_bubble["start_y"], "end_x": active_bubble["cx"], "end_y": active_bubble["cy"],
                "min_y": active_bubble["min_y"], "seen_frames": active_bubble["seen_frames"], "ended_at": now,
            }
            bubble_history.append(ended)
            log_bubble_event(ended)
            active_bubble = None

        if DEBUG_DRAW:
            cv2.line(frame, (pipe_center_x, 0), (pipe_center_x, h), (255, 255, 0), 2)
            cv2.rectangle(frame, (pipe_center_x - pipe_width, pipe_top), (pipe_center_x + pipe_width, pipe_bottom), (255, 0, 255), 2)
            cv2.rectangle(frame, (pipe_center_x - pipe_lock_width, pipe_top), (pipe_center_x + pipe_lock_width, pipe_bottom), (0, 255, 255), 1)
            cv2.line(frame, (0, pipe_exit_y), (w, pipe_exit_y), (0, 0, 255), 2)
            for (cx, cy, r) in detections:
                cv2.circle(frame, (cx, cy), max(8, int(r)), (0, 255, 255), 2)
            if candidate_bubble is not None:
                cv2.circle(frame, (candidate_bubble["cx"], candidate_bubble["cy"]), max(8, int(candidate_bubble["r"])), (255, 165, 0), 1)
            if active_bubble is not None:
                cv2.circle(frame, (active_bubble["cx"], active_bubble["cy"]), max(10, int(active_bubble["r"])), (0, 255, 0), 2)

        fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))
        cv2.putText(frame, f"Video: {Path(VIDEO_PATH).name}", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(frame, f"FPS:{fps:.1f} Count:{bubble_count_total} B:{1 if active_bubble else 0} C:{1 if candidate_bubble else 0}", (12, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

        ok, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
        if ok:
            yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")

        elapsed = time.time() - start_time
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/video_feed")
def video_feed():
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/toggle_debug", methods=["POST"])
def toggle_debug():
    global DEBUG_DRAW
    DEBUG_DRAW = not DEBUG_DRAW
    return jsonify({"success": True, "debug": DEBUG_DRAW})


@app.route("/reset_counter", methods=["POST"])
def reset_counter():
    global bubble_count_total, bubble_history, active_bubble, candidate_bubble
    bubble_count_total = 0
    bubble_history = []
    active_bubble = None
    candidate_bubble = None
    return jsonify({"success": True})


@app.route("/status")
def status():
    return jsonify({
        "video": Path(VIDEO_PATH).name,
        "debug": DEBUG_DRAW,
        "count": bubble_count_total,
        "active_bubble": active_bubble is not None,
        "candidate_bubble": candidate_bubble is not None,
        "auto_center": AUTO_CENTER_ENABLED,
        "profile": PROFILE_PATH,
    })


def cleanup_video():
    global cap
    if cap is not None:
        cap.release()
        cap = None


def main():
    global VIDEO_PATH, LOG_PATH, PROFILE_PATH, cap

    parser = argparse.ArgumentParser(description="Bubble test harness with dual-video friendly locking")
    parser.add_argument("--video", default=DEFAULT_VIDEO_PATH)
    parser.add_argument("--profile")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5002)
    args = parser.parse_args()

    ensure_runtime_dependencies()

    VIDEO_PATH = validate_video_path(args.video)
    profile_path = resolve_profile_path(VIDEO_PATH, args.profile)
    if profile_path:
        apply_test_video_profile(profile_path)

    LOG_PATH = f"bubble_log_{Path(VIDEO_PATH).stem}_v2.jsonl"
    cap = cv2.VideoCapture(VIDEO_PATH)
    if not cap.isOpened():
        raise SystemExit(f"Could not open video: {VIDEO_PATH}")

    atexit.register(cleanup_video)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()