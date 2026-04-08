# Raspberry Pi Bubble-Tracking Workflow

This document tracks the current working tree in this folder as of 2026-04-02. It is focused on the newer bubble-detection workflow built around:

- `livestream.py` for the Raspberry Pi live camera stream 
- `test_video.py` for offline validation against `Bubbles.mp4`

The current code no longer matches the older README draft that described generic ROI tracking, motion detection, and recording controls. The sections below follow the behavior that is actually present in the code today.

## What the current workflow does

### Live camera app: `livestream.py`

- Streams MJPEG video to a browser.
- Captures still images from the web UI.
- Uses a fixed ROI and pipe-geometry filters.
- Acquires a new bubble only near the pipe mouth spawn band.
- Tracks one active bubble at a time.
- Counts that bubble only after it disappears and has traveled upward enough.
- Keeps in-memory bubble history for ended tracks.

### Video test harness: `test_video.py`

- Reads `Bubbles.mp4` instead of the Raspberry Pi camera.
- Reuses `detect_bubbles()` from `livestream.py`.
- Runs a browser view with debug controls and a status endpoint.
- Uses optional auto-centering to estimate the pipe center from the video.
- Logs ended bubble events to `bubble_log.jsonl`.

## Prerequisites

- Raspberry Pi 5 with a working camera stack for `livestream.py`
- Python 3
- A clean virtual environment
- `Bubbles.mp4` in this folder for `test_video.py`

The examples below assume your shell is in this directory:

```bash
/home/pi/Documents/RnD_Camera/RnD_Camera
```

## Setup

The dependency file is one directory above this folder.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r ../requirements.txt
```

Recommended setup notes:

- Use a fresh virtual environment instead of mixing system packages and old wheel caches.
- Start from `../requirements.txt` before adding anything else.
- If OpenCV import errors mention NumPy 2.x vs 1.x compiled modules, rebuild the virtual environment and reinstall the dependencies cleanly.

## Live camera app

### Run `livestream.py`

Default run:

```bash
python3 livestream.py
```

Example with explicit host, port, resolution, and FPS:

```bash
python3 livestream.py --host 0.0.0.0 --port 5000 --resolution 960 540 --fps 30
```

Current CLI flags:

- `--host` / `-H` default: `127.0.0.1`
- `--port` / `-p` default: `5000`
- `--resolution` / `-r` default: `960 540`
- `--fps` / `-f` default: `30`

Default browser URL:

```text
http://127.0.0.1:5000
```

If you run with `--host 0.0.0.0`, use the Raspberry Pi IP address from another device.

### Live app behavior

- Camera frames are captured through Picamera2.
- Frames are converted to BGR and cropped to the configured ROI.
- Detection runs on a reduced grayscale ROI with blur, resize, histogram equalization, and `cv2.HoughCircles`.
- Detections are filtered by radius, pipe lock width, and pipe height.
- When no bubble is active, new detections are accepted only inside the spawn band near the pipe mouth.
- When a bubble is active, new detections are matched to the current bubble and downward jumps larger than `DOWNWARD_TOLERANCE` are rejected.
- A bubble is counted only after it is lost and its upward travel meets the threshold.

### Live UI and outputs

- `GET /` serves the inline HTML page.
- `GET /video_feed` serves the MJPEG stream.
- `GET /capture` saves `image_YYYYMMDD_HHMMSS.jpg` and returns JSON.

Capture response shape:

```json
{"success": true, "filename": "image_YYYYMMDD_HHMMSS.jpg"}
```

Failure shape:

```json
{"success": false, "error": "message"}
```

### Live overlay guides

With `DEBUG_DRAW = True`, the live stream draws:

- ROI box
- pipe center line
- pipe boundary box
- pipe lock zone
- spawn band
- upward-check band
- filtered detections
- active bubble marker
- FPS / frame / tracking / count summary box

## Video test harness

### Run `test_video.py`

```bash
python3 test_video.py
```

Current behavior:

- Opens `Bubbles.mp4`
- Streams the processed video at port `5001`
- Loops back to the start when the video ends
- Uses full-frame ROI for testing
- Can auto-estimate the pipe center when `AUTO_CENTER_ENABLED = True`

Browser access:

- Local: `http://127.0.0.1:5001`
- Network: `http://<raspberry-pi-ip>:5001`

### Test UI and outputs

- `GET /` serves the HTML test page.
- `GET /video_feed` serves the MJPEG test stream.
- `POST /toggle_debug` flips the overlay state.
- `POST /reset_counter` clears the counter, history, and active bubble.
- `GET /status` reports the current UI state.

Status response shape:

```json
{
  "debug": true,
  "count": 0,
  "active_bubble": false,
  "auto_center": true
}
```

The test harness also appends ended bubble events to:

```text
bubble_log.jsonl
```

Each log line is JSON and includes fields such as bubble ID, whether it was counted, start/end coordinates, seen frames, and end time.

### Counting behavior in the test harness

The test harness is similar to the live tracker but not identical:

- it still tracks one active bubble at a time
- it counts once the bubble has moved enough and enters the count band
- it logs the ended bubble event after the track is lost

## Tuning reference

These are the main constants worth adjusting in the current code.

### ROI

Live app:

- `ROI_X1 = 250`
- `ROI_Y1 = 140`
- `ROI_X2 = 760`
- `ROI_Y2 = 500`

### Pipe geometry

Used by both scripts:

- `PIPE_CENTER_X_BIAS = -50`
- `PIPE_WIDTH_RATIO = 0.35`
- `PIPE_LOCK_WIDTH_RATIO = 0.18`
- `PIPE_TOP_RATIO = 0.25`
- `PIPE_BOTTOM_RATIO = 0.75`
- `PIPE_EXIT_RATIO = 0.55`

### Detection timing

- Live app: `DETECT_INTERVAL = 0.08`
- Test harness: `DETECT_EVERY_SECONDS = 0.10`

### Bubble size filters

- Live app: `MIN_RADIUS = 4`, `MAX_RADIUS = 20`
- Test harness: `MIN_RADIUS = 10`, `MAX_RADIUS = 20`

### Tracking thresholds

Live app:

- `LOCK_AFTER_FRAMES = 2`
- `LOST_AFTER_FRAMES = 4`
- `MIN_UPWARD_TRAVEL = 30`
- `MAX_MATCH_DISTANCE = 70`
- `DOWNWARD_TOLERANCE = 10`
- `SPAWN_BAND_HALF = 22`

Test harness:

- `LOCK_AFTER_FRAMES = 2`
- `LOST_AFTER_FRAMES = 3`
- `MIN_TRAVEL_Y = 18`
- `MAX_MATCH_DISTANCE = 60`
- `COUNT_BAND_HALF = 12`

### Auto-centering in the test harness

- `AUTO_CENTER_ENABLED = True`
- `AUTO_CENTER_SMOOTHING = 0.80`
- `CENTER_SEARCH_WIDTH_RATIO = 0.35`

### Debug drawing and frame rate

- `TARGET_FPS = 20` in both scripts
- `DEBUG_DRAW = True` in both scripts

## HTTP interface summary

### `livestream.py`

- `GET /`
- `GET /video_feed`
- `GET /capture`

### `test_video.py`

- `GET /`
- `GET /video_feed`
- `POST /toggle_debug`
- `POST /reset_counter`
- `GET /status`

## File outputs

- Captured live images: `image_YYYYMMDD_HHMMSS.jpg`
- Test-harness event log: `bubble_log.jsonl`

`Bubbles.mp4` is the sample input video used by `test_video.py`.

## Known issues and environment notes

The current workflow assumes a compatible Python environment for NumPy and OpenCV.

If `livestream.py` fails before startup with an OpenCV import error, the usual cause is a NumPy/OpenCV wheel mismatch in the active virtual environment.

Practical impact:

- the app code can be valid while the local environment still refuses to import `cv2`
- rebuilding the virtual environment and reinstalling from `../requirements.txt` is the recommended first fix

## Related files

- `livestream.py`
- `test_video.py`
- `Bubbles.mp4`
- `bubble_log.jsonl`
- `../requirements.txt`
