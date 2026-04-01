#!/usr/bin/env python3
"""
Raspberry Pi 5 Camera Livestream Server

"""



import io
import threading
import logging
import time
import numpy as np
import cv2

from dataclasses import dataclass

from datetime import datetime
from flask import Flask, render_template_string, Response, jsonify

# --- CONFIG ---

COOLDOWN = 2
output = None





#motion_detected = False

TARGET_FPS = 20
FRAME_TIME = 1.0 / TARGET_FPS  # Limit to TARGET_FPS


CIRCLE_EVERY_N_FRAMES = 2
MATCH_MAX_DIST = 80
MIN_TRACK_HITS = 2
TRACK_TTL_SECONDS = 0.60
SMOOTHING_ALPHA = 0.35
MIN_UPWARD_DELTA = 3






# --- GLOBALS ---
tracked_bubbles = {}
bubble_count_total = 0
bubble_id_counter = 0
last_record_time = 0
motion_timer = 0
MOTION_RECORD_SECONDS = 3

objects = {}


@dataclass
class TrackState:
    id: int
    cx: int
    cy: int
    r: int
    prev_cy: int
    max_cy: int
    first_seen: float
    last_seen: float
    hits: int
    misses: int
    counted: bool

def update_objects(ids):
    now = time.time()

    for obj_id in objects:

        objects[obj_id]["seen"] = False

    for obj_id in ids:
        if obj_id not in objects:
            objects[obj_id] = {"first_seen": now, "last_seen": now, "seen": True}
        else:
            objects[obj_id]["last_seen"] = now
            objects[obj_id]["seen"] = True

    for obj_id in list(objects.keys()):
        if not objects[obj_id]["seen"]:
           if now - objects[obj_id]["last_seen"] > 2:
            del objects[obj_id]


def next_track_id():
    global bubble_id_counter

    bubble_id_counter += 1
    return bubble_id_counter


def is_confirmed_track(track):
    return track.hits >= MIN_TRACK_HITS


def build_track_state(cx, cy, r, now):
    track_id = next_track_id()
    return TrackState(
        id=track_id,
        cx=int(cx),
        cy=int(cy),
        r=int(r),
        prev_cy=int(cy),
        max_cy=int(cy),
        first_seen=now,
        last_seen=now,
        hits=1,
        misses=0,
        counted=False,
    )


def greedy_match_detections(detections, track_positions, max_dist):
    candidates = []

    for det_index, (cx, cy, _) in enumerate(detections):
        for track_id, px, py in track_positions:
            dist = float(np.hypot(cx - px, cy - py))
            if dist <= max_dist:
                candidates.append((dist, det_index, track_id))

    candidates.sort(key=lambda item: item[0])

    matched_detections = set()
    matched_tracks = set()
    matches = {}

    for _, det_index, track_id in candidates:
        if det_index in matched_detections or track_id in matched_tracks:
            continue

        matched_detections.add(det_index)
        matched_tracks.add(track_id)
        matches[track_id] = det_index

    unmatched_detections = set(range(len(detections))) - matched_detections
    unmatched_tracks = {track_id for track_id, _, _ in track_positions} - matched_tracks
    return matches, unmatched_tracks, unmatched_detections


def update_live_tracks(current_tracks, detections, now):
    if not current_tracks and not detections:
        return {}

    track_positions = [
        (track_id, track.cx, track.cy)
        for track_id, track in current_tracks.items()
    ]
    matches, unmatched_track_ids, unmatched_detection_indices = greedy_match_detections(
        detections,
        track_positions,
        MATCH_MAX_DIST,
    )

    for track_id, det_index in matches.items():
        track = current_tracks[track_id]
        detected_cx, detected_cy, detected_r = detections[det_index]

        track.prev_cy = track.cy
        track.cx = int(round((1.0 - SMOOTHING_ALPHA) * track.cx + SMOOTHING_ALPHA * detected_cx))
        track.cy = int(round((1.0 - SMOOTHING_ALPHA) * track.cy + SMOOTHING_ALPHA * detected_cy))
        track.r = int(round((1.0 - SMOOTHING_ALPHA) * track.r + SMOOTHING_ALPHA * detected_r))
        track.max_cy = max(track.max_cy, track.cy)
        track.last_seen = now
        track.hits += 1
        track.misses = 0

    for track_id in unmatched_track_ids:
        current_tracks[track_id].misses += 1

    for det_index in unmatched_detection_indices:
        cx, cy, r = detections[det_index]
        track = build_track_state(cx, cy, r, now)
        current_tracks[track.id] = track

    return current_tracks


def expire_stale_tracks(current_tracks, now):
    return {
        track_id: track
        for track_id, track in current_tracks.items()
        if now - track.last_seen <= TRACK_TTL_SECONDS
    }


def count_upward_crossings(current_tracks, count_y2):
    count_increment = 0

    for track in current_tracks.values():
        if not is_confirmed_track(track):
            continue
        if track.counted:
            continue
        if track.max_cy < count_y2:
            continue
        if track.prev_cy - track.cy < MIN_UPWARD_DELTA:
            continue
        if track.cy > count_y2:
            continue

        track.counted = True
        count_increment += 1

    return count_increment


def confirmed_tracks(current_tracks):
    return [
        track
        for track in current_tracks.values()
        if is_confirmed_track(track)
    ]




class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:

            self.frame = buf
            self.condition.notify_all()



try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder, JpegEncoder, MJPEGEncoder
    from picamera2.outputs import FfmpegOutput, FileOutput
except ImportError:
    print("Error: picamera2 is not installed")
    exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask app initialization
app = Flask(__name__)

# Global variables
camera = None

streaming = False
recording = False
recording_output = None


# HTML template for the web interface
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
            color: #666;
            margin-bottom: 20px;
            font-size: 0.9em;
        }

        .status.live {
            color: #27ae60;
            font-weight: bold;
        }

        .status.live::before {
            content: "● ";
            color: #27ae60;
            font-size: 1.2em;
            margin-right: 5px;
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
            letter-spacing: 0.5px;
        }

        button:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.2);
        }

        button:active:not(:disabled) {
            transform: translateY(0);
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

        .btn-record {
            background: #e74c3c;
            color: white;
        }

        .btn-record:hover:not(:disabled) {
            background: #c0392b;
        }

        .btn-record.recording {
            background: #27ae60;
            animation: pulse 1s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
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
        <div class="status live" id="status">LIVE</div>

        <div class="video-container">
            <img id="stream" src="/video_feed" alt="Camera Stream">
        </div>

        <div class="controls">
            <button class="btn-capture" onclick="captureImage()">📸 Capture</button>
            <button class="btn-record" id="recordBtn" onclick="toggleRecording()">🔴 Record</button>
        </div>

        <div id="message" class="info"></div>
        <div id="error" class="error"></div>
    </div>

    <script>
        let isRecording = false;
        let streamTimeout;

        // Keep refreshing the stream
        function refreshStream() {
            const img = document.getElementById('stream');

            img.onerror = () => {
                showError("Stream disconnected");
            };
            const now = new Date().getTime();
            img.src = '/video_feed?t=' + now;
            streamTimeout = setTimeout(refreshStream, 80);
        }

        function captureImage() {
            const button = event.target;
            button.disabled = true;

            fetch('/capture')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        showMessage('📸 Image captured: ' + data.filename);
                    } else {
                        showError('Failed to capture image: ' + data.error);
                    }
                })
                .catch(error => {
                    showError('Failed to capture image: ' + error);
                })
                .finally(() => {
                    button.disabled = false;
                });
        }

        function toggleRecording() {
            const button = event.target;
            button.disabled = true;

            const action = isRecording ? 'stop_recording' : 'start_recording';

            fetch('/' + action, { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        isRecording = !isRecording;
                        if (isRecording) {
                            button.classList.add('recording');
                            button.textContent = '⏹️ Stop Recording';
                            showMessage('🔴 Recording started: ' + data.filename);
                        } else {
                            button.classList.remove('recording');
                            button.textContent = '🔴 Record';
                            showMessage('Recording stopped');
                        }
                    } else {
                        showError('Failed: ' + data.error);
                    }
                })
                .catch(error => {
                    showError('Failed to toggle recording: ' + error);
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

        // Start streaming when page loads
        window.addEventListener('load', () => {
            document.getElementById('stream').src = '/video_feed';
        });
        window.addEventListener('beforeunload', () => clearTimeout(streamTimeout));
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    """Serve the main HTML page."""
    return render_template_string(HTML_TEMPLATE)


@app.route('/video_feed')
def video_feed():
    """Stream individual JPEG frames."""
    def generate():
        """Generate frames continuously."""
        if output is None:
             return "Camera not ready"
        while True:
            with output.condition:
                output.condition.wait()
                frame = output.frame

            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' +
                frame + b'\r\n'

            )

    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/capture')
def capture():
    """Capture a still image."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"image_{timestamp}.jpg"

        request_obj = camera.capture_request()
        request_obj.save("main", filename)
        request_obj.release()

        logger.info(f"Image captured: {filename}")
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        logger.error(f"Error capturing image: {e}")
        return jsonify({'success': False, 'error': str(e)})


#@app.route('/start_recording', methods=['POST'])
#def start_recording():
    #global recording, recording_output

    #if recording:
        #return jsonify({'success': False, 'error': 'Already recording'})

    #try:
        #timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        #filename = f"recording_{timestamp}.mp4"


        #return jsonify({'success': True, 'filename': filename})

    #except Exception as e:
        #return jsonify({'success': False, 'error': str(e)})


#@app.route('/stop_recording', methods=['POST'])
#def stop_recording():
    #"""Stop recording video."""
    #global recording, recording_output

    #if not recording:
        #return jsonify({'success': False, 'error': 'Not recording'})

    #try:
        #camera.stop_recording()
        #recording = False
        #logger.info("Recording stopped")
        #return jsonify({'success': True})
    #except Exception as e:
        #logger.error(f"Error stopping recording: {e}")
        #return jsonify({'success': False, 'error': str(e)})



def match_bubbles(detected, previous, max_dist=MATCH_MAX_DIST):
    """Match new detections to existing tracked bubbles."""
    updated = {}
    now = time.time()

    previous_positions = [
        (bubble_id, px, py)
        for bubble_id, (px, py, _, _) in previous.items()
    ]
    matches, _, unmatched_detection_indices = greedy_match_detections(
        detected,
        previous_positions,
        max_dist,
    )

    for bubble_id, det_index in matches.items():
        cx, cy, r = detected[det_index]
        updated[bubble_id] = (cx, cy, r, now)

    for det_index in unmatched_detection_indices:
        cx, cy, r = detected[det_index]
        bubble_id = next_track_id()
        updated[bubble_id] = (cx, cy, r, now)

    return updated

def detect_bubbles(small_gray, scale_x, scale_y, x_offset, y_offset):
    small_gray = cv2.GaussianBlur(small_gray, (5, 5), 0)
    """Detect circles and filter noise."""
    circles = cv2.HoughCircles(
        small_gray,
        cv2.HOUGH_GRADIENT,
        dp=2.0,
        minDist=50,
        param1=80,
        param2 =15,
        minRadius=5,
        maxRadius=30
    )

    results = []

    if circles is None:
        return results

    circles = np.uint16(np.around(circles))



    for (x, y, r) in circles[0]:
        if y >= small_gray.shape[0] or x >= small_gray.shape[1]:
            continue

        #if small_gray[y, x] < 120:
            #continue

        cx = int(x * scale_x) + x_offset
        cy = int(y * scale_y) + y_offset

        results.append((cx, cy, r,))

    return results


def frame_capture_thread():
    """Continuously capture frames from camera."""
    global tracked_bubbles, bubble_count_total
    frame_count = 0
    frame_interval = 1.0 / 30
    last_detect_time = 0

    print("THREAD STARTED")
    logger.info("Frame capture thread started")

    while streaming:
        start_time = time.time()
        fps = 0.0

        try:
            frame_count += 1

            request_obj = camera.capture_request()
            frame = camera.capture_array("main")
            lores = camera.capture_array("lores")
            request_obj.release()

            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            h, w = frame.shape[:2]

            # Warm-up
            if frame_count < 10:
                continue

            do_detection = (frame_count % 2 != 0)

            # Fixed ROI
            x1, y1, x2, y2 = 250, 140, 760, 500
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(w, x2)
            y2 = min(h, y2)

            if x2 <= x1 or y2 <= y1:
                print("Invalid ROI coordinates, skipping frame")
                continue

            roi = frame[y1:y2, x1:x2]
            if roi.size == 0:
                print("Empty ROI, skipping frame")
                continue

            if roi.shape[0] < 20 or roi.shape[1] < 20:
                print("ROI too small, skipping frame")
                continue

            roi_h, roi_w = roi.shape[:2]
            x_offset, y_offset = x1, y1

            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
            cv2.putText(
                frame, "ROI Active", (x1, y1 - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1
            )

            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (7, 7), 0)
            small = cv2.resize(blur, (64, 48), interpolation=cv2.INTER_AREA)
            small = cv2.equalizeHist(small)

            # Pipe zone
            pipe_center_x = x_offset + (roi_w // 2)
            PIPE_WIDTH = int(roi_w * 0.35)
            PIPE_TOP = y_offset + int(roi_h * 0.55)
            PIPE_BOTTOM = y_offset + int(roi_h * 0.90)
            PIPE_EXIT_Y = y_offset + int(roi_h * 0.75)

            # Draw pipe zone
            cv2.line(frame, (pipe_center_x, y1), (pipe_center_x, y2), (255, 255, 0), 2)
            cv2.rectangle(
                frame,
                (pipe_center_x - PIPE_WIDTH, PIPE_TOP),
                (pipe_center_x + PIPE_WIDTH, PIPE_BOTTOM),
                (255, 0, 255), 2
            )
            cv2.line(frame, (x1, PIPE_EXIT_Y), (x2, PIPE_EXIT_Y), (0, 0, 255), 2)
            cv2.putText(
                frame, "EXIT LINE", (x1, PIPE_EXIT_Y - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1
            )

            count_band_half_height = max(12, int(roi_h * 0.04))
            count_y1 = PIPE_EXIT_Y - count_band_half_height
            count_y2 = PIPE_EXIT_Y + count_band_half_height
            cv2.rectangle(
                frame,
                (pipe_center_x - PIPE_WIDTH, count_y1),
                (pipe_center_x + PIPE_WIDTH, count_y2),
                (0, 165, 255), 2
            )
            cv2.putText(
                frame, "COUNT BAND", (pipe_center_x - PIPE_WIDTH, count_y1 - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1
            )

            detections = []
            detection_ran = False

            if do_detection and (time.time() - last_detect_time > 0.15):
                detection_ran = True
                detections = detect_bubbles(
                    small_gray=small,
                    scale_x=roi_w / 64,
                    scale_y=roi_h / 48,
                    x_offset=x_offset,
                    y_offset=y_offset
                )
                last_detect_time = time.time()

                # Size filter
                detections = [(cx, cy, r) for (cx, cy, r) in detections if 10 < r < 20]

                # Spatial filter
                detections = [
                    (cx, cy, r) for (cx, cy, r) in detections
                    if abs(cx - pipe_center_x) <= PIPE_WIDTH and PIPE_TOP <= cy <= PIPE_BOTTOM
                ]
                tracked_bubbles = update_live_tracks(tracked_bubbles, detections, time.time())
                bubble_count_total += count_upward_crossings(tracked_bubbles, count_y2)

            # Draw raw detections
            for (cx, cy, r) in detections:
                cv2.circle(frame, (cx, cy), int(r), (0, 255, 255), 2)

            now = time.time()
            tracked_bubbles = expire_stale_tracks(tracked_bubbles, now)
            visible_tracks = confirmed_tracks(tracked_bubbles)

            # Draw tracked bubbles
            for track in visible_tracks:
                track_color = (0, 255, 0) if not track.counted else (0, 200, 255)
                cv2.circle(frame, (track.cx, track.cy), max(8, int(track.r)), track_color, 2)
                cv2.circle(frame, (track.cx, track.cy), 3, track_color, -1)
                cv2.putText(
                    frame,
                    f"ID {track.id} H{track.hits}",
                    (track.cx + 10, track.cy),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    track_color,
                    1
                )
                if track.counted:
                    cv2.putText(
                        frame,
                        "COUNTED",
                        (track.cx + 10, track.cy + 16),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.45,
                        track_color,
                        1
                    )

            if detection_ran:
                print(f"Tracked: {len(visible_tracks)} | Total Count: {bubble_count_total}")

            fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))

            # Overlay
            overlay = frame.copy()
            box_width = 260
            box_height = 140
            ox1 = frame.shape[1] - box_width - 10
            oy1 = 10
            ox2 = frame.shape[1] - 10
            oy2 = oy1 + box_height

            cv2.rectangle(overlay, (ox1, oy1), (ox2, oy2), (0, 0, 0), -1)
            frame = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0)

            y = oy1 + 25
            line_height = 25
            texts = [
                f"FPS: {fps:.1f}",
                f"Frame: {frame_count}",
                f"Bubbles: {len(visible_tracks)}",
                f"Count: {bubble_count_total}",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ]

            for text in texts:
                cv2.putText(
                    frame, text, (ox1 + 10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2
                )
                y += line_height

            # Encode and stream
            ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 40])
            if ret:
                with output.condition:
                    output.frame = jpeg.tobytes()
                    output.condition.notify_all()

            # FPS limiter
            elapsed = time.time() - start_time
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)

        except Exception as e:
            print("Thread error:", e)

def initialize_camera(resolution=(960, 540), fps=30):
    """Initialize the camera."""
    global camera, streaming, output


    try:
        logger.info("Initializing camera...")
        camera = Picamera2()

        config = camera.create_video_configuration(
            main={
                "size": resolution,
            },
            lores={
                "size": (320, 240),
                "format": "RGB888"
            },
            controls={
                "FrameRate": fps,
            },
            buffer_count=2


        )
        camera.configure(config)





        camera.start()

        output = StreamingOutput()




        streaming = True

        time.sleep(0.5)  # Allow camera to warm up

        # Start frame capture thread
        capture_thread = threading.Thread(
            target=frame_capture_thread,
            daemon=True
            )

        capture_thread.start()

        logger.info(f"Camera initialized: {resolution} @ {fps}fps")
        return True

    except Exception as e:
        logger.error(f"Error initializing camera: {e}")
        return False


def cleanup_camera():
    """Clean up camera resources."""
    global camera, streaming, recording

    streaming = False

    # Give thread time to exit
    time.sleep(0.2)

    if camera:
        try:
            if recording:
                try:
                    camera.stop_recording()
                except Exception:
                    pass
                recording = False

            camera.stop()
            camera.close()
            camera = None

            logger.info("Camera cleaned up")

        except Exception as e:
            logger.error(f"Error cleaning up camera: {e}")

    # Clean up temp frame file
    try:
        import os
        if os.path.exists("temp_frame.jpg"):
            os.remove("temp_frame.jpg")
    except Exception:
        pass



if __name__ == '__main__':
    import argparse
    import atexit
    parser = argparse.ArgumentParser(
        description="Raspberry Pi 5 Camera Livestream Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  # Start livestream server on localhost:5000
  python3 livestream.py

  # Make accessible from network (0.0.0.0 = all interfaces)
  python3 livestream.py --host 0.0.0.0

  # Custom port
  python3 livestream.py --port 8080

  # Custom resolution
  python3 livestream.py --resolution 960 540

ACCESS FROM BROWSER:
  Local: http://localhost:5000
  Network: http://<raspberry-pi-ip>:5000
        """
    )

    parser.add_argument('-p', '--port', type=int, default=5000)

    parser.add_argument('-H', '--host', type=str, default='127.0.0.1')

    parser.add_argument('-r', '--resolution', type=int, nargs=2, default=[960, 540])

    parser.add_argument('-f', '--fps', type=int, default=30)


    args = parser.parse_args()

    # ✅ ALWAYS cleanup on exit
    atexit.register(cleanup_camera)

    try:
        if initialize_camera(tuple(args.resolution), args.fps):
            logger.info(f"🚀 Starting livestream server on http://{args.host}:{args.port}")

            app.run(
                host=args.host,
                port=args.port,
                debug=False,
                threaded=True,
                use_reloader=False # ✅ ALWAYS cleanup on exit
            )

    except KeyboardInterrupt:
        logger.info("Shutting down...")

    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        #✅ GUARANTEED cleanup
        cleanup_camera()
