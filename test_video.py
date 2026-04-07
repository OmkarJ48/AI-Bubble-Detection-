import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import json
import time
import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template_string

from livestream import detect_bubbles

VIDEO_PATH = "Bubbles2.mp4"
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

COUNT_BAND_HALF = 12

DETECT_EVERY_SECONDS = 0.10

MIN_RADIUS = 10
MAX_RADIUS = 20
MAX_MATCH_DISTANCE = 60

AUTO_CENTER_ENABLED = True
AUTO_CENTER_SMOOTHING = 0.80
CENTER_SEARCH_WIDTH_RATIO = 0.35

DEBUG_DRAW = True

# ---- NOISE / FALSE-START CONTROL ----
STARTUP_IGNORE_FRAMES = 8
CANDIDATE_CONFIRM_FRAMES = 2
CANDIDATE_MATCH_DISTANCE = 50
MIN_START_BELOW_EXIT = 18   # new bubble must start this many px below exit line


app = Flask(__name__)
cap = cv2.VideoCapture(VIDEO_PATH)

active_bubble = None
candidate_bubble = None
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
                        'Debug: ' + (data.debug ? 'ON' : 'OFF') +
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
    Estimate pipe center from near-vertical edges in the middle region.
    Falls back to manual bias if no stable line is found.
    """
    roi_h, roi_w = gray_roi.shape[:2]

    search_half_width = int(roi_w * CENTER_SEARCH_WIDTH_RATIO / 2)
    fallback_local_x = fallback_center_x
    x_left = max(0, fallback_local_x - search_half_width)
    x_right = min(roi_w, fallback_local_x + search_half_width)

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
        maxLineGap=15
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
        estimated = int(np.mean([x for _, x in best]))
    else:
        estimated = fallback_center_x

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

        # ---- PIPE GEOMETRY ----
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
            pipe_center_x = x_offset + fallback_pipe_center_x

        pipe_width = int(roi_w * PIPE_WIDTH_RATIO)
        pipe_lock_width = int(roi_w * PIPE_LOCK_WIDTH_RATIO)

        pipe_top = y_offset + int(roi_h * PIPE_TOP_RATIO)
        pipe_bottom = y_offset + int(roi_h * PIPE_BOTTOM_RATIO)
        pipe_exit_y = y_offset + int(roi_h * PIPE_EXIT_RATIO)

        count_y1 = pipe_exit_y - COUNT_BAND_HALF
        count_y2 = pipe_exit_y + COUNT_BAND_HALF

        # ---- DRAW GUIDES ----
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

            cv2.putText(
                frame, "COUNT BAND", (x1 + 10, count_y1 - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1
            )

        detections = []

        # ---- DETECT ----
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
                if MIN_RADIUS < r < MAX_RADIUS
            ]

            detections = [
                (cx, cy, r)
                for (cx, cy, r) in detections
                if abs(cx - pipe_center_x) <= pipe_lock_width and pipe_top <= cy <= pipe_bottom
            ]

            # only allow new starts below exit line to avoid random top noise
            detections = [
                (cx, cy, r)
                for (cx, cy, r) in detections
                if cy >= (pipe_exit_y + MIN_START_BELOW_EXIT)
            ] + [
                (cx, cy, r)
                for (cx, cy, r) in detections
                if active_bubble is not None
            ]

            detections = sorted(detections, key=lambda d: abs(d[0] - pipe_center_x))

        if DEBUG_DRAW:
            for (cx, cy, r) in detections:
                cv2.circle(frame, (cx, cy), int(r), (0, 255, 255), 2)

        now = time.time()

        # ---- CANDIDATE -> ACTIVE ----
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
            else:
                active_bubble["lost_frames"] += 1

        # ---- COUNT ONCE ----
        if active_bubble is not None:
            bubble_id = active_bubble["id"]
            cx = active_bubble["cx"]
            cy = active_bubble["cy"]

            travel_y = abs(cy - active_bubble["start_y"])

            if (
                not active_bubble["counted"]
                and active_bubble["seen_frames"] >= LOCK_AFTER_FRAMES
                and travel_y >= MIN_TRAVEL_Y
                and count_y1 <= cy <= count_y2
            ):
                bubble_count_total += 1
                active_bubble["counted"] = True
                print(f"COUNTED bubble {bubble_id} total={bubble_count_total}")

        # ---- END LOST BUBBLE ----
        if active_bubble is not None and active_bubble["lost_frames"] >= LOST_AFTER_FRAMES:
            ended_entry = {
                "id": active_bubble["id"],
                "counted": active_bubble["counted"],
                "start_x": active_bubble["start_x"],
                "start_y": active_bubble["start_y"],
                "end_x": active_bubble["cx"],
                "end_y": active_bubble["cy"],
                "seen_frames": active_bubble["seen_frames"],
                "ended_at": now,
            }

            bubble_history.append(ended_entry)
            log_bubble_event(ended_entry)

            print(f"ENDED bubble {active_bubble['id']} counted={active_bubble['counted']}")
            active_bubble = None

        # ---- DRAW ACTIVE BUBBLE ----
        if active_bubble is not None:
            cx = active_bubble["cx"]
            cy = active_bubble["cy"]
            r = active_bubble["r"]
            bid = active_bubble["id"]

            color = (0, 255, 0) if active_bubble["counted"] else (0, 255, 255)

            cv2.circle(frame, (cx, cy), int(r), color, 2)
            cv2.circle(frame, (cx, cy), 3, color, -1)

            label = f"ID {bid}"
            label += " COUNTED" if active_bubble["counted"] else " TRACKING"

            cv2.putText(
                frame, label, (cx + 10, cy),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1
            )

        # ---- DRAW CANDIDATE ----
        if candidate_bubble is not None and DEBUG_DRAW:
            cv2.circle(
                frame,
                (candidate_bubble["cx"], candidate_bubble["cy"]),
                int(candidate_bubble["r"]),
                (255, 165, 0),
                1
            )
            cv2.putText(
                frame,
                f"CANDIDATE {candidate_bubble['seen_frames']}",
                (candidate_bubble["cx"] + 10, candidate_bubble["cy"] - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (255, 165, 0),
                1
            )

        # ---- OVERLAY ----
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

        y = oy1 + 22
        line_height = 20
        texts = [
            f"FPS: {fps:.1f}",
            f"Frame: {frame_count}",
            f"Bubble: {1 if active_bubble is not None else 0}",
            f"Candidate: {1 if candidate_bubble is not None else 0}",
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
    global bubble_count_total, bubble_history, active_bubble, candidate_bubble
    bubble_count_total = 0
    bubble_history = []
    active_bubble = None
    candidate_bubble = None
    return jsonify({"success": True})


@app.route("/status")
def status():
    return jsonify({
        "debug": DEBUG_DRAW,
        "count": bubble_count_total,
        "active_bubble": active_bubble is not None,
        "candidate_bubble": candidate_bubble is not None,
        "auto_center": AUTO_CENTER_ENABLED,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, threaded=True) 

 