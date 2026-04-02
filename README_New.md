# Raspberry Pi Bubble-Tracking Workflow

This document tracks the current bubble-detection workflow in this folder as of 2026-04-02.

The project now uses one shared calibration format across:

- `livestream.py` for the Raspberry Pi camera stream
- `test_video.py` for offline tuning against sample videos

The current workflow is profile-based calibration, not ML model training.

## Recommended media workflow

Do not rename raw `.MOV` files to `.mp4`.

Recommended approach:

- keep the original `.MOV` files as local master captures
- generate normalized `.mp4` proxy videos for the repo workflow
- tune each proxy with its own JSON profile

Why this is the safer path:

- a container rename does not actually convert the codec or pixel format
- OpenCV and Raspberry Pi playback are more reliable with normalized H.264 MP4 files
- the raw `.MOV` masters stay untouched if you need to re-export later

Raw masters are intended to stay local and untracked. The repo workflow uses the generated `Bubbles*.mp4` proxies.

## Supported sample videos

Main baseline video:

- `Bubbles.mp4`

Per-video calibration inputs:

- `Bubbles2.mp4`
- `Bubbles3.mp4`
- `Bubbles4.mp4`
- `Bubbles5.mp4`
- `Bubbles6.mp4`

## Setup

The dependency file is one directory above this folder.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r ../requirements.txt
```

Recommended setup notes:

- start from a fresh virtual environment
- install from `../requirements.txt` before adding anything else
- if OpenCV import errors mention NumPy version mismatch, rebuild the environment cleanly

## Generate proxy videos from raw `.MOV`

Use the helper script to normalize new captures without touching the originals:

```bash
python3 normalize_bubble_videos.py Bubbles3.MOV Bubbles4.MOV Bubbles5.MOV Bubbles6.MOV
```

The helper generates same-stem `.mp4` proxies with:

- H.264 video
- `yuv420p`
- `+faststart`
- 30 fps output
- `1280x720` resolution

Examples:

- `Bubbles3.MOV` -> `Bubbles3.mp4`
- `Bubbles4.MOV` -> `Bubbles4.mp4`
- `Bubbles5.MOV` -> `Bubbles5.mp4`
- `Bubbles6.MOV` -> `Bubbles6.mp4`

Use `--force` if you want to overwrite an existing proxy:

```bash
python3 normalize_bubble_videos.py --force Bubbles3.MOV
```

## Profile workflow

Profiles live under `profiles/`:

- `profiles/Bubbles.json`
- `profiles/Bubbles2.json`
- `profiles/Bubbles3.json`
- `profiles/Bubbles4.json`
- `profiles/Bubbles5.json`
- `profiles/Bubbles6.json`

Each profile uses the same top-level schema:

```json
{
  "shared": {},
  "test_video": {},
  "livestream": {}
}
```

Section meaning:

- `shared`: pipe geometry and values used by both scripts
- `test_video`: offline video harness tuning
- `livestream`: Raspberry Pi live camera tuning

This lets us tune a video offline first, then reuse the same profile in the live camera workflow.

## Live camera app

Default run:

```bash
python3 livestream.py
```

Run with an explicit profile:

```bash
python3 livestream.py --profile profiles/Bubbles3.json
```

Run with full CLI overrides:

```bash
python3 livestream.py --host 0.0.0.0 --port 5000 --resolution 960 540 --fps 30 --profile profiles/Bubbles3.json
```

Current CLI flags:

- `--host` / `-H` default: `127.0.0.1`
- `--port` / `-p` default: `5000`
- `--resolution` / `-r` default: `960 540`
- `--fps` / `-f` default: `30`
- `--profile` optional: live-camera tuning overrides

Default browser URL:

```text
http://127.0.0.1:5000
```

Current live behavior:

- Picamera2 provides the camera frames
- detection runs inside the configured ROI
- new bubbles are acquired only near the pipe-mouth spawn band
- only one active bubble is tracked at a time
- a bubble is counted only after it disappears and moved upward enough
- captured images are saved as `image_YYYYMMDD_HHMMSS.jpg`

Routes:

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

Default run:

```bash
python3 test_video.py
```

Run a specific proxy video:

```bash
python3 test_video.py --video Bubbles3.mp4
```

Run a specific video with an explicit profile:

```bash
python3 test_video.py --video Bubbles3.mp4 --profile profiles/Bubbles3.json
```

Current profile resolution order:

1. explicit `--profile`
2. `profiles/<video_stem>.json`
3. fallback `profiles/Bubbles.json`

Current behavior:

- `Bubbles.mp4` remains the default baseline video
- the server runs on port `5001`
- the stream loops when the video reaches the end
- ended bubble events are written to a per-video JSONL log
- the page status line and on-frame overlay show the active video name

Routes:

- `GET /`
- `GET /video_feed`
- `POST /toggle_debug`
- `POST /reset_counter`
- `GET /status`

Status response shape:

```json
{
  "video": "Bubbles3.mp4",
  "debug": true,
  "count": 0,
  "active_bubble": false,
  "auto_center": true
}
```

Log naming:

```text
bubble_log_<video_stem>.jsonl
```

Examples:

- `Bubbles.mp4` -> `bubble_log_Bubbles.jsonl`
- `Bubbles3.mp4` -> `bubble_log_Bubbles3.jsonl`
- `Bubbles6.mp4` -> `bubble_log_Bubbles6.jsonl`

## Shared tuning categories

The current profiles cover these main groups:

- pipe center bias
- pipe width and pipe-lock ratios
- pipe top, bottom, and exit ratios
- min and max radius filters
- match-distance and count thresholds
- auto-centering values for `test_video.py`
- ROI and live-only detection thresholds for `livestream.py`

Common baseline values currently used in the seeded profiles include:

- `pipe_center_x_bias = -50`
- `pipe_width_ratio = 0.35`
- `pipe_lock_width_ratio = 0.18`
- `pipe_top_ratio = 0.25`
- `pipe_bottom_ratio = 0.75`
- `pipe_exit_ratio = 0.55`
- `count_band_half = 12`

These baseline profiles are meant to be copied forward and tuned per video.

## Branch and publish workflow

Recommended repo workflow from now on:

1. make calibration and code changes on a side branch first
2. commit only the scoped files for that change
3. push the branch
4. open a PR into `main`
5. merge into `main` after review

That keeps the calibration history visible in both the side branch and the main branch while still letting us pull changes back cleanly if needed.

## Known environment notes

The scripts now support profile loading without importing camera/OpenCV dependencies on the `--help` path, but full runtime still depends on a compatible local Python environment.

If `cv2` fails to import at runtime, the most likely cause is an OpenCV and NumPy wheel mismatch in the active virtual environment. In that case, rebuild the virtual environment and reinstall from `../requirements.txt`.

## Related files

- `livestream.py`
- `test_video.py`
- `normalize_bubble_videos.py`
- `profiles/`
- `Bubbles.mp4`
- `Bubbles2.mp4`
- `Bubbles3.mp4`
- `Bubbles4.mp4`
- `Bubbles5.mp4`
- `Bubbles6.mp4`
- `../requirements.txt`
