# Raspberry Pi Bubble-Tracking Workflow

This document tracks the current workflow in this folder as of 2026-04-09.

The project now runs a shared precision-first tracker across live and offline flows:

- `livestream.py` for Raspberry Pi camera streaming
- `test_video.py` for offline tuning against the prototype dataset playlist
- `bubble_tracker.py` for shared tracker, detection, auto-centering, and profile helpers

The workflow is profile-driven calibration, not ML training.

## Canonical dataset

The default dataset is now the 5-video shopfloor prototype dataset in `Prototype Dataset 1/`:

- `IMG_3271.MOV`
- `IMG_3272.MOV`
- `IMG_3273.MOV`
- `IMG_3274.MOV`
- `IMG_3275.MOV`

Keep the raw `.MOV` files untouched as masters. Use normalized `.mp4` proxies for OpenCV playback and tuning.

Legacy files such as `Bubbles.mp4`, `Bubbles2.mp4`, and `profiles/Bubbles*.json` remain in the repo only as manual reference assets. They are no longer the default workflow.

## Recommended media workflow

Do not rename raw `.MOV` files to `.mp4`.

Recommended approach:

- keep raw `.MOV` files as local masters
- generate normalized `.mp4` proxy videos beside them
- tune each proxy with a JSON profile in `profiles/`

Why:

- container rename does not convert codec or pixel format
- OpenCV playback is more stable on normalized H.264 MP4
- raw masters stay untouched for future exports

Normalize the full prototype dataset with:

```bash
python3 normalize_bubble_videos.py --dataset-dir "Prototype Dataset 1"
```

Overwrite existing proxies with:

```bash
python3 normalize_bubble_videos.py --force --dataset-dir "Prototype Dataset 1"
```

Normalize individual files with:

```bash
python3 normalize_bubble_videos.py "Prototype Dataset 1/IMG_3271.MOV"
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

The canonical shared fallback profile is:

```text
profiles/PrototypeDataset.json
```

Clip-specific profiles currently exist for:

- `profiles/IMG_3271.json`
- `profiles/IMG_3272.json`
- `profiles/IMG_3273.json`
- `profiles/IMG_3274.json`
- `profiles/IMG_3275.json`

This allows offline clip tuning first, then reuse of the same pipe-only tracking behavior in live mode.

## Shared precision-first behavior

`bubble_tracker.py` enforces identical core behavior in both scripts:

- states: `idle`, `candidate`, `active`
- at most one candidate and one active track
- strict start gating: pipe-mouth corridor + spawn band + below-exit threshold
- candidate confirmation requires upward movement while staying in the mouth corridor
- broader continuation corridor for already-confirmed bubbles
- direction-aware rejection: downward, lateral, and step-jump limits
- count once only after lock, upward travel, count-band hit, and disappearance
- bottle-surface bubbles may be detected transiently but must be rejected before activation and never counted

## Offline test harness (`test_video.py`)

Default run:

```bash
python3 test_video.py
```

This now starts in dataset-playlist mode and cycles through the normalized `.mp4` files in `Prototype Dataset 1/`.

Use a different dataset directory:

```bash
python3 test_video.py --dataset-dir "Prototype Dataset 1"
```

Specific video:

```bash
python3 test_video.py --video "Prototype Dataset 1/IMG_3273.mp4"
```

Specific video + explicit profile:

```bash
python3 test_video.py --video "Prototype Dataset 1/IMG_3273.mp4" --profile profiles/IMG_3273.json
```

Profile resolution order:

1. explicit `--profile`
2. `profiles/<video_stem>.json`
3. fallback `profiles/PrototypeDataset.json`

Routes:

- `GET /`
- `GET /video_feed`
- `POST /toggle_debug`
- `POST /reset_counter`
- `POST /next_video`
- `POST /prev_video`
- `GET /status`

Current status response shape:

```json
{
  "video": "Prototype Dataset 1/IMG_3271.mp4",
  "current_video": "IMG_3271.mp4",
  "video_index": 1,
  "video_count": 5,
  "dataset_dir": "Prototype Dataset 1",
  "playlist_mode": true,
  "profile": "profiles/IMG_3271.json",
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

## Live camera app (`livestream.py`)

Default run:

```bash
python3 livestream.py
```

Run with explicit profile:

```bash
python3 livestream.py --profile profiles/IMG_3273.json
```

Run with stream-name profile discovery:

```bash
python3 livestream.py --stream-name livestream
```

Full example:

```bash
python3 livestream.py --host 0.0.0.0 --port 5000 --resolution 960 540 --fps 30 --profile profiles/IMG_3273.json --stream-name livestream
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
3. fallback `profiles/PrototypeDataset.json`

Live routes:

- `GET /`
- `GET /video_feed`
- `GET /capture`

Behavior highlights:

- shared precision-first tracker core
- live defaults now sync to the prototype dataset fallback profile
- fixed pipe-focused ROI plus optional live auto-centering from profile
- stricter pipe-mouth acquisition corridor than continuation corridor
- counter advances only when a qualified upward bubble track is lost
- debug overlay shows ROI, pipe corridor, pipe-mouth corridor, spawn band, and count band

Capture response:

```json
{"success": true, "filename": "image_YYYYMMDD_HHMMSS.jpg"}
```

## Tuning categories

Profiles currently cover:

- pipe geometry and continuation corridor
- pipe-mouth acquisition corridor
- ROI and detection cadence
- radius filters
- candidate confirmation, loss, and minimum upward qualification
- start and count band gates
- jitter limits (`downward_tolerance`, `max_lateral_shift`, `max_step_distance`)
- auto-centering controls including `auto_center_max_offset_px`

## Setup

The dependency file is one directory above this folder.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r ../requirements.txt
```

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
- `Prototype Dataset 1/`
- `../requirements.txt`
