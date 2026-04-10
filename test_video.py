#!/usr/bin/env python3
"""Offline video harness for bubble tracker."""

import json
import time
from pathlib import Path

import cv2
from flask import Flask, Response, jsonify, render_template_string

from livestream import detect_bubbles

VIDEO_PATH = "Bubbles2.mp4"
LOG_PATH = "bubble_log.jsonl"
TARGET_FPS = 20
DETECT_EVERY_SECONDS = 0.10

LOCK_AFTER_FRAMES = 2
LOST_AFTER_FRAMES = 3
MIN_TRAVEL_Y = 18
MIN_RADIUS = 10
MAX_RADIUS = 20
MAX_MATCH_DISTANCE = 60
DOWNWARD_TOLERANCE = 10

PIPE_CENTER_X_BIAS = -50
PIPE_WIDTH_RATIO = 0.35
PIPE_LOCK_WIDTH_RATIO = 0.18
PIPE_TOP_RATIO = 0.25
PIPE_BOTTOM_RATIO = 0.75
PIPE_EXIT_RATIO = 0.55
COUNT_BAND_HALF = 12
SPAWN_BAND_HALF = 22

DEBUG_DRAW = True

app = Flask(__name__)
cap = cv2.VideoCapture(VIDEO_PATH)

active_bubble = None
candidate_bubble = None
bubble_history = []
next_bubble_id = 1
bubble_count_total = 0
last_detect_time = 0.0

HTML_PAGE = """
<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Bubble Video Stream Test</title></head>
<body style='font-family:Arial;background:#111;color:#fff;text-align:center'>
<h2>Bubble Video Stream Test</h2>
<p>
<button onclick='toggleDebug()'>Toggle Debug</button>
<button onclick='resetCounter()'>Reset Counter</button>
</p>
<div id='status'>Loading...</div>
<img src='/video_feed' style='max-width:95%;border:2px solid #444;border-radius:8px'>
<script>
function refreshStatus(){fetch('/status').then(r=>r.json()).then(d=>{document.getElementById('status').textContent=`Debug:${d.debug?'ON':'OFF'} | Count:${d.count} | Active:${d.active_bubble?'YES':'NO'} | Candidate:${d.candidate_bubble?'YES':'NO'}`;});}
function toggleDebug(){fetch('/toggle_debug',{method:'POST'}).then(refreshStatus)}
function resetCounter(){fetch('/reset_counter',{method:'POST'}).then(refreshStatus)}
setInterval(refreshStatus,1000);refreshStatus();
</script>
</body></html>
"""


def distance(p1, p2):
    return float(((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5)


def log_bubble_event(entry):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def begin_candidate(cx, cy, r, now):
    return {"cx": cx, "cy": cy, "r": r, "first_seen": now, "last_seen": now, "seen_frames": 1}


def promote_candidate_to_active(candidate, now, bubble_id):
    return {
        "id": bubble_id,
        "cx": candidate["cx"],
        "cy": candidate["cy"],
        "r": candidate["r"],
        "start_y": candidate["cy"],
        "min_y": candidate["cy"],
        "last_seen": now,
        "seen_frames": candidate["seen_frames"],
        "lost_frames": 0,
    }


def generate_frames():
    global cap, active_bubble, candidate_bubble, bubble_history
    global next_bubble_id, bubble_count_total, last_detect_time

    frame_count = 0
    frame_interval = 1.0 / TARGET_FPS

    while True:
        start = time.time()
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            active_bubble = None
            candidate_bubble = None
            continue

        frame_count += 1
        h, w = frame.shape[:2]
        roi = frame
        roi_h, roi_w = roi.shape[:2]

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        small = cv2.equalizeHist(cv2.resize(cv2.GaussianBlur(gray, (7, 7), 0), (64, 48), interpolation=cv2.INTER_AREA))

        pipe_center_x = (roi_w // 2) + PIPE_CENTER_X_BIAS
        pipe_width = int(roi_w * PIPE_WIDTH_RATIO)
        pipe_lock_width = int(roi_w * PIPE_LOCK_WIDTH_RATIO)
        pipe_top = int(roi_h * PIPE_TOP_RATIO)
        pipe_bottom = int(roi_h * PIPE_BOTTOM_RATIO)
        pipe_exit_y = int(roi_h * PIPE_EXIT_RATIO)
        spawn_y1, spawn_y2 = pipe_exit_y - SPAWN_BAND_HALF, pipe_exit_y + SPAWN_BAND_HALF

        detections = []
        now = time.time()
        if now - last_detect_time > DETECT_EVERY_SECONDS:
            raw = detect_bubbles(small, roi_w / 64, roi_h / 48, 0, 0)
            last_detect_time = now
            filtered = [(cx, cy, r) for cx, cy, r in raw if MIN_RADIUS <= r <= MAX_RADIUS]
            filtered = [(cx, cy, r) for cx, cy, r in filtered if abs(cx - pipe_center_x) <= pipe_lock_width and pipe_top <= cy <= pipe_bottom]
            if active_bubble is None:
                start_candidates = [(cx, cy, r) for cx, cy, r in filtered if spawn_y1 <= cy <= spawn_y2]
                start_candidates.sort(key=lambda d: (abs(d[0] - pipe_center_x), -d[2]))
                detections = start_candidates
            else:
                filtered.sort(key=lambda d: distance((d[0], d[1]), (active_bubble["cx"], active_bubble["cy"])))
                detections = filtered

        if active_bubble is None:
            if candidate_bubble is None:
                if detections:
                    candidate_bubble = begin_candidate(*detections[0], now)
            else:
                matched = False
                if detections:
                    cx, cy, r = detections[0]
                    if distance((cx, cy), (candidate_bubble["cx"], candidate_bubble["cy"])) < MAX_MATCH_DISTANCE:
                        candidate_bubble["cx"] = int(0.7 * candidate_bubble["cx"] + 0.3 * cx)
                        candidate_bubble["cy"] = int(0.7 * candidate_bubble["cy"] + 0.3 * cy)
                        candidate_bubble["r"] = r
                        candidate_bubble["last_seen"] = now
                        candidate_bubble["seen_frames"] += 1
                        matched = True
                if not matched:
                    candidate_bubble = None
                elif candidate_bubble["seen_frames"] >= LOCK_AFTER_FRAMES:
                    active_bubble = promote_candidate_to_active(candidate_bubble, now, next_bubble_id)
                    next_bubble_id += 1
                    candidate_bubble = None
        else:
            best = None
            best_dist = 1e9
            for cx, cy, r in detections:
                if cy > active_bubble["cy"] + DOWNWARD_TOLERANCE:
                    continue
                d = distance((cx, cy), (active_bubble["cx"], active_bubble["cy"]))
                if d < best_dist:
                    best_dist = d
                    best = (cx, cy, r)
            if best is not None and best_dist < MAX_MATCH_DISTANCE:
                cx, cy, r = best
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
            counted = active_bubble["seen_frames"] >= LOCK_AFTER_FRAMES and traveled_up >= MIN_TRAVEL_Y
            if counted:
                bubble_count_total += 1
            event = {
                "id": active_bubble["id"],
                "counted": counted,
                "seen_frames": active_bubble["seen_frames"],
                "traveled_up": traveled_up,
                "ended_at": now,
            }
            bubble_history.append(event)
            log_bubble_event(event)
            active_bubble = None

        if DEBUG_DRAW:
            cv2.line(frame, (pipe_center_x, 0), (pipe_center_x, roi_h), (255, 255, 0), 2)
            cv2.rectangle(frame, (pipe_center_x - pipe_width, pipe_top), (pipe_center_x + pipe_width, pipe_bottom), (255, 0, 255), 2)
            cv2.rectangle(frame, (0, spawn_y1), (roi_w, spawn_y2), (0, 165, 255), 1)
            cv2.rectangle(frame, (0, pipe_exit_y - COUNT_BAND_HALF), (roi_w, pipe_exit_y + COUNT_BAND_HALF), (0, 0, 255), 1)

            if candidate_bubble is not None:
                cv2.circle(frame, (candidate_bubble["cx"], candidate_bubble["cy"]), max(8, int(candidate_bubble["r"] * 2.0)), (255, 165, 0), 1)
            if active_bubble is not None:
                cv2.circle(frame, (active_bubble["cx"], active_bubble["cy"]), max(8, int(active_bubble["r"] * 2.0)), (0, 255, 0), 2)

        cv2.putText(frame, f"Count: {bubble_count_total}", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
        if ok:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"

        elapsed = time.time() - start
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
    global active_bubble, candidate_bubble, bubble_history, bubble_count_total, next_bubble_id
    active_bubble = None
    candidate_bubble = None
    bubble_history = []
    bubble_count_total = 0
    next_bubble_id = 1
    return jsonify({"success": True, "count": bubble_count_total})


@app.route("/status")
def status():
    return jsonify(
        {
            "debug": DEBUG_DRAW,
            "count": bubble_count_total,
            "active_bubble": active_bubble is not None,
            "candidate_bubble": candidate_bubble is not None,
            "auto_center": False,
            "video": Path(VIDEO_PATH).name,
        }
    )


if __name__ == "__main__":
    import atexit

    def _cleanup():
        if cap is not None:
            cap.release()

    atexit.register(_cleanup)
    app.run(host="0.0.0.0", port=5001, threaded=True)