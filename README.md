# Raspberry Pi Bubble-Tracking Workflow

This document reflects the current code in this folder as of 2026-04-09.

The active workflow now uses a shared precision-first tracking core:

- `livestream.py` for Raspberry Pi camera streaming
- `test_video.py` for offline validation and tuning
- `bubble_tracker.py` for shared tracking, detection, auto-centering, and profile utilities

## What the current workflow does

### Shared tracker core: `bubble_tracker.py`

- Implements one precision-first state machine for both apps.
- States are strictly `idle`, `candidate`, and `active`.
- Enforces one candidate and one active bubble maximum.
- Applies strict start gating (pipe lock zone + spawn band + below exit threshold).
- Applies direction-aware matching (downward/lateral/step limits).
- Counts only after lock confirmation, minimum upward travel, count-band hit, and disappearance.

### Live camera app: `livestream.py`

- Streams MJPEG video to a browser.
- Captures still images from the web UI.
- Uses shared tracker logic from `bubble_tracker.py`.
- Supports fixed ROI plus optional live auto-centering.
- Applies profile-driven tuning from `shared` + `livestream` sections.

### Video test harness: `test_video.py`

- Reads an input video (`Bubbles.mp4` by default).
- Uses the same shared tracker logic as live mode.
- Runs a browser view with debug controls and `/status`.
- Supports optional auto-centering and profile-driven tuning from `shared` + `test_video`.
- Logs ended bubble events to `bubble_log.jsonl`.

## Setup

The dependency file is one directory above this folder.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r ../requirements.txt
```

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

Example with profile + stream-name selection:

```bash
python3 livestream.py --profile profiles/Bubbles2.json --stream-name livestream
```

Current CLI flags:

- `--host` / `-H` default: `127.0.0.1`
- `--port` / `-p` default: `5000`
- `--resolution` / `-r` default: `960 540`
- `--fps` / `-f` default: `30`
- `--profile` optional: explicit profile path
- `--stream-name` default: `livestream` (used for live profile discovery)

Live profile resolution order:

1. explicit `--profile`
2. `profiles/<stream_name>.json`
3. fallback `profiles/Bubbles.json`

Default browser URL:

```text
http://127.0.0.1:5000
```

### Live behavior and overlay

- Camera frames are captured through Picamera2.
- Detection runs on reduced grayscale ROI using `cv2.HoughCircles`.
- Shared tracker rejects side acquisitions and jitter-driven track switches.
- New tracks only start in strict start-eligible regions.
- Count increments only after a qualified bubble disappears.
- With debug enabled, overlay includes ROI, pipe guides, spawn band, count band, candidate/active markers, and status box with `State`.

### Live routes

- `GET /`
- `GET /video_feed`
- `GET /capture`

Capture response shape:

```json
{"success": true, "filename": "image_YYYYMMDD_HHMMSS.jpg"}
```

Failure shape:

```json
{"success": false, "error": "message"}
```

## Video test harness

### Run `test_video.py`

Default:

```bash
python3 test_video.py
```

Specific video:

```bash
python3 test_video.py --video Bubbles2.mp4
```

Specific video + explicit profile:

```bash
python3 test_video.py --video Bubbles2.mp4 --profile profiles/Bubbles2.json
```

Test profile resolution order:

1. explicit `--profile`
2. `profiles/<video_stem>.json`
3. fallback `profiles/Bubbles.json`

Default browser URL:

```text
http://127.0.0.1:5001
```

### Test routes and status

- `GET /`
- `GET /video_feed`
- `POST /toggle_debug`
- `POST /reset_counter`
- `GET /status`

Current status response shape:

```json
{
  "video": "Bubbles2.mp4",
  "profile": "profiles/Bubbles2.json",
  "debug": true,
  "count": 0,
  "state": "idle",
  "active_bubble": false,
  "candidate_bubble": false,
  "auto_center": true
}
```

### Event log output

- Test-harness event log: `bubble_log.jsonl`
- Captured live still images: `image_YYYYMMDD_HHMMSS.jpg`

## Profile schema and key tuning fields

Profiles under `profiles/` use:

```json
{
  "shared": {},
  "test_video": {},
  "livestream": {}
}
```

Commonly tuned fields now include:

- pipe geometry (`pipe_center_x_bias`, `pipe_width_ratio`, `pipe_lock_width_ratio`, `pipe_top_ratio`, `pipe_bottom_ratio`, `pipe_exit_ratio`)
- acquisition/count bands (`spawn_band_half`, `count_band_half`, `count_band_offset`, `min_start_below_exit`)
- tracking gates (`lock_after_frames`, `lost_after_frames`, `min_upward_travel`/`min_travel_y`)
- match/jitter limits (`max_match_distance`, `downward_tolerance`, `max_lateral_shift`, `max_step_distance`)
- candidate behavior (`candidate_confirm_frames`, `candidate_match_distance`, `candidate_lost_after_frames`)
- auto-centering (`auto_center_enabled`, `auto_center_smoothing`, `center_search_width_ratio`, `auto_center_max_offset_px`)

## Known environment notes

- `livestream.py` requires a working Picamera2 stack on Raspberry Pi.
- Both scripts require compatible OpenCV/NumPy wheels.
- If OpenCV import fails, rebuild the venv and reinstall from `../requirements.txt`.

