# Raspberry Pi Bubble-Tracking Workflow

This document tracks the current workflow in this folder as of 2026-04-09.

The project now runs a shared precision-first tracker across live and offline flows:

- `livestream.py` for Raspberry Pi camera streaming
- `test_video.py` for offline tuning against sample videos
- `bubble_tracker.py` for shared tracker, detection, auto-centering, and profile helpers

The workflow is profile-driven calibration, not ML training.

## Recommended media workflow

Do not rename raw `.MOV` files to `.mp4`.

Recommended approach:

- keep raw `.MOV` files as local masters
- generate normalized `.mp4` proxy videos for repo workflows
- tune each proxy with a JSON profile in `profiles/`

Why:

- container rename does not convert codec/pixel format
- OpenCV playback is more stable on normalized H.264 MP4
- raw masters stay untouched for future exports

Generate proxy videos with:

```bash
python3 normalize_bubble_videos.py Bubbles3.MOV Bubbles4.MOV Bubbles5.MOV Bubbles6.MOV
```

Use `--force` to overwrite existing proxies:

```bash
python3 normalize_bubble_videos.py --force Bubbles3.MOV
```

## Profile workflow

Profiles live under `profiles/` and follow this schema:

```json
{
  "shared": {},
  "test_video": {},
  "livestream": {}
}
```

Section meanings:

- `shared`: geometry and values used by both scripts
- `test_video`: offline-only tuning
- `livestream`: live-camera-only tuning

This allows offline calibration first, then reuse in live mode.

## Setup

The dependency file is one directory above this folder.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r ../requirements.txt
```

## Shared precision-first behavior

`bubble_tracker.py` enforces identical core behavior in both scripts:

- states: `idle`, `candidate`, `active`
- at most one candidate and one active track
- strict start gating: lock corridor + spawn band + below exit threshold
- nearest valid continuation only while tracking/candidate exists
- direction-aware rejection: downward, lateral, and step-jump limits
- count once only after lock/travel/count-band and disappearance conditions
- drop non-qualifying tracks without incrementing count

## Live camera app (`livestream.py`)

Default run:

```bash
python3 livestream.py
```

Run with explicit profile:

```bash
python3 livestream.py --profile profiles/Bubbles3.json
```

Run with stream-name profile discovery:

```bash
python3 livestream.py --stream-name livestream
```

Full example:

```bash
python3 livestream.py --host 0.0.0.0 --port 5000 --resolution 960 540 --fps 30 --profile profiles/Bubbles3.json --stream-name livestream
```

Current CLI flags:

- `--host` / `-H` default: `127.0.0.1`
- `--port` / `-p` default: `5000`
- `--resolution` / `-r` default: `960 540`
- `--fps` / `-f` default: `30`
- `--profile` optional
- `--stream-name` default: `livestream`

Live profile resolution order:

1. explicit `--profile`
2. `profiles/<stream_name>.json`
3. fallback `profiles/Bubbles.json`

Live routes:

- `GET /`
- `GET /video_feed`
- `GET /capture`

Behavior highlights:

- shared precision-first tracker core
- fixed ROI, plus optional live auto-centering from profile
- strict start gating and jitter rejection
- counter advances only when a qualified upward bubble track is lost
- debug overlay shows state, candidate/active markers, and guide bands

Capture response:

```json
{"success": true, "filename": "image_YYYYMMDD_HHMMSS.jpg"}
```

## Offline test harness (`test_video.py`)

Default run:

```bash
python3 test_video.py
```

Specific video:

```bash
python3 test_video.py --video Bubbles3.mp4
```

Specific video + explicit profile:

```bash
python3 test_video.py --video Bubbles3.mp4 --profile profiles/Bubbles3.json
```

Profile resolution order:

1. explicit `--profile`
2. `profiles/<video_stem>.json`
3. fallback `profiles/Bubbles.json`

Routes:

- `GET /`
- `GET /video_feed`
- `POST /toggle_debug`
- `POST /reset_counter`
- `GET /status`

Current status response shape:

```json
{
  "video": "Bubbles3.mp4",
  "profile": "profiles/Bubbles3.json",
  "debug": true,
  "count": 0,
  "state": "idle",
  "active_bubble": false,
  "candidate_bubble": false,
  "auto_center": true
}
```

Event logs are appended to:

```text
bubble_log.jsonl
```

## Tuning categories

Profiles currently cover:

- pipe geometry and lock corridor
- ROI and detection cadence
- radius filters
- candidate confirmation/loss behavior
- start and count band gates
- jitter limits (`downward_tolerance`, `max_lateral_shift`, `max_step_distance`)
- auto-centering controls including `auto_center_max_offset_px`

## Branch and publish workflow

Recommended repo workflow:

1. make calibration/code changes on a side branch
2. commit only scoped files
3. push the branch
4. open a PR into `main`
5. merge after review

## Known environment notes

- `livestream.py` requires Picamera2 on Raspberry Pi.
- Both scripts require compatible OpenCV/NumPy versions.
- If `cv2` import fails, rebuild the venv and reinstall from `../requirements.txt`.

## Related files

- `bubble_tracker.py`
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

