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