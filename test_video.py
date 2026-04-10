import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import argparse
import atexit
import threading
import time
from pathlib import Path

import cv2
from flask import Flask, Response, jsonify, render_template_string

DEFAULT_DATASET_DIR = "Prototype Dataset 1"

VIDEO_PATH = None
DATASET_DIR = DEFAULT_DATASET_DIR
PLAYLIST_MODE = True
PLAYLIST = []
CURRENT_VIDEO_INDEX = 0
TARGET_FPS = 20

app = Flask(__name__)
cap = None
playback_lock = threading.Lock()
playback_frame_count = 0


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Bubble Video Viewer</title>
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
    <h1>Bubble Video Viewer</h1>

    <div class="toolbar">
        <button class="btn" onclick="prevVideo()">Prev Video</button>
        <button class="btn" onclick="nextVideo()">Next Video</button>
    </div>

    <div class="status" id="status">Loading...</div>

    <img id="stream" src="/video_feed" alt="Bubble stream">

    <script>
        function refreshStatus() {
            fetch('/status')
                .then(r => r.json())
                .then(data => {
                    const parts = [
                        'Video: ' + data.current_video,
                        'Clip: ' + data.video_index + '/' + data.video_count,
                        'Mode: ' + (data.playlist_mode ? 'PLAYLIST' : 'SINGLE'),
                        'FPS target: ' + data.target_fps,
                    ];
                    document.getElementById('status').textContent = parts.join(' | ');
                })
                .catch(() => {
                    document.getElementById('status').textContent = 'Status unavailable';
                });
        }

        function nextVideo() {
            fetch('/next_video', { method: 'POST' })
                .then(() => refreshStatus());
        }

        function prevVideo() {
            fetch('/prev_video', { method: 'POST' })
                .then(() => refreshStatus());
        }

        setInterval(refreshStatus, 1000);
        refreshStatus();
    </script>
</body>
</html>
"""


def validate_video_path(video_path: str) -> str:
    resolved_path = Path(video_path)
    if not resolved_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")
    return str(resolved_path)


def build_dataset_playlist(dataset_dir: str) -> list[str]:
    dataset_path = Path(dataset_dir)
    if not dataset_path.is_dir():
        raise SystemExit(f"Dataset directory not found: {dataset_dir}")

    playlist = sorted(str(path) for path in dataset_path.glob("*.mp4"))
    if not playlist:
        raise SystemExit(
            "No normalized .mp4 videos found in "
            f"{dataset_dir}. Run normalize_bubble_videos.py --dataset-dir \"{dataset_dir}\" first."
        )
    return playlist


def current_video_name() -> str:
    if VIDEO_PATH is None:
        return "none"
    return Path(VIDEO_PATH).name


def current_clip_position() -> tuple[int, int]:
    count = max(1, len(PLAYLIST))
    return CURRENT_VIDEO_INDEX + 1, count


def reset_playback_state() -> None:
    global playback_frame_count
    playback_frame_count = 0


def open_video_capture(video_path: str) -> cv2.VideoCapture:
    video_capture = cv2.VideoCapture(video_path)
    if not video_capture.isOpened():
        raise SystemExit(f"Failed to open video: {video_path}")
    return video_capture


def switch_to_video_unlocked(index: int) -> None:
    global cap, CURRENT_VIDEO_INDEX, VIDEO_PATH

    if not PLAYLIST:
        raise SystemExit("No videos available for playback")

    CURRENT_VIDEO_INDEX = index % len(PLAYLIST)
    VIDEO_PATH = validate_video_path(PLAYLIST[CURRENT_VIDEO_INDEX])
    reset_playback_state()

    if cap is not None:
        cap.release()
    cap = open_video_capture(VIDEO_PATH)

    print(f"Using video: {VIDEO_PATH}")


def cleanup_video_capture() -> None:
    global cap
    if cap is not None:
        cap.release()
        cap = None


def generate_frames():
    global playback_frame_count

    while True:
        start_time = time.time()

        with playback_lock:
            if cap is None:
                switch_to_video_unlocked(CURRENT_VIDEO_INDEX)

            ret, frame = cap.read()
            active_video_index = CURRENT_VIDEO_INDEX

            if not ret:
                next_index = active_video_index + 1 if PLAYLIST_MODE else active_video_index
                switch_to_video_unlocked(next_index)
                continue

            playback_frame_count += 1
            frame_count = playback_frame_count
            active_video_path = VIDEO_PATH
            playlist_count = len(PLAYLIST)

        fps = min(30, 1.0 / max(time.time() - start_time, 1e-6))
        overlay = frame.copy()
        box_width = 360
        box_height = 110
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
            f"Video: {Path(active_video_path).name if active_video_path else 'none'}",
            f"Clip: {active_video_index + 1}/{max(1, playlist_count)}",
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
        frame_interval = 1.0 / TARGET_FPS
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)


@app.route("/video_feed")
def video_feed():
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/next_video", methods=["POST"])
def next_video():
    with playback_lock:
        switch_to_video_unlocked(CURRENT_VIDEO_INDEX + 1)
        return jsonify({"success": True, "current_video": current_video_name()})


@app.route("/prev_video", methods=["POST"])
def prev_video():
    with playback_lock:
        switch_to_video_unlocked(CURRENT_VIDEO_INDEX - 1)
        return jsonify({"success": True, "current_video": current_video_name()})


@app.route("/status")
def status():
    with playback_lock:
        video_index, video_count = current_clip_position()
        return jsonify(
            {
                "video": VIDEO_PATH,
                "current_video": current_video_name(),
                "video_index": video_index,
                "video_count": video_count,
                "dataset_dir": DATASET_DIR,
                "playlist_mode": PLAYLIST_MODE,
                "target_fps": TARGET_FPS,
            }
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bubble video viewer")
    parser.add_argument("--video", default=None, help="Video file path to stream")
    parser.add_argument("--dataset-dir", default=DEFAULT_DATASET_DIR, help="Directory of normalized dataset proxy videos")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()

    DATASET_DIR = args.dataset_dir

    if args.video:
        PLAYLIST_MODE = False
        PLAYLIST = [validate_video_path(args.video)]
    else:
        PLAYLIST_MODE = True
        PLAYLIST = build_dataset_playlist(DATASET_DIR)

    with playback_lock:
        switch_to_video_unlocked(0)

    atexit.register(cleanup_video_capture)
    app.run(host=args.host, port=args.port, threaded=True)
