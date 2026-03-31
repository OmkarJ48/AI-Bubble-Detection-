import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import cv2, time, numpy as np
from flask import Flask, Response
from livestream import detect_bubbles, match_bubbles

VIDEO_PATH = "Bubbles.mp4"
PIPE_WIDTH = 40
MIN_CONFIRM_FRAMES = 2

app = Flask(__name__)
cap = cv2.VideoCapture(VIDEO_PATH)

tracked_bubbles = {}
bubble_counts = {}

last_detect_time = 0

def generate_frames():
    global tracked_bubbles, bubble_counts, last_detect_time
    while True:
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # Loop video
            continue

        start_time = time.time()
        ROI = (320, 120, 480, 360)
        x1, y1, x2, y2 = ROI
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            continue

        roi_h, roi_w = roi.shape[:2]
        x_offset, y_offset = x1, y1

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (64, 48))

        detections = []

        if time.time() - last_detect_time > 0.1:
            detections = detect_bubbles(
                small_gray=small,
                scale_x=roi_w/64,
                scale_y=roi_h/48,
                x_offset=x_offset,
                y_offset=y_offset
            )
            last_detect_time = time.time()

            # Size & pipe filter
            detections = [(cx, cy, r) for (cx, cy, r) in detections if 10<r<20]
            pipe_center_x = x_offset + roi_w//2
            PIPE_TOP = y_offset + int(roi_h*0.25)
            PIPE_BOTTOM = y_offset + int(roi_h*0.65)
            detections = [(cx, cy, r) for (cx, cy, r) in detections if abs(cx-pipe_center_x)<PIPE_WIDTH and PIPE_TOP<cy<PIPE_BOTTOM]

        # Tracking
        matched = match_bubbles(detections, tracked_bubbles)
        now = time.time()
        updated_bubbles = {}
        for bid, (cx, cy, r) in matched.items():
            bubble_counts[bid] = bubble_counts.get(bid, 0)+1
            if bubble_counts[bid] >= MIN_CONFIRM_FRAMES:
                updated_bubbles[bid] = (cx, cy, r, now)
        tracked_bubbles = updated_bubbles

        # Draw
        for bid, (cx, cy, r, _) in tracked_bubbles.items():
            cv2.circle(frame, (cx, cy), int(r), (0,255,0), 2)

        # Encode JPEG
        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/')
def index():
    return '<h1>Bubble Video Stream</h1><img src="/video_feed">'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, threaded=True)