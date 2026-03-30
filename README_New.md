# Raspberry Pi 5 Smart Bubble Detection Livestream

Advanced livestream system using Picamera2 + OpenCV for: - Real-time
video streaming - Manual ROI (Region of Interest) - Bubble detection
using HoughCircles - Bubble tracking with IDs - Motion detection
(stable + crash-safe) - Web interface (Flask)

------------------------------------------------------------------------

## Features

-   📡 Live MJPEG stream in browser
-   🎯 Manual ROI for focused detection
-   🟢 Bubble detection + tracking
-   🔴 Debug visualization (raw / filtered / tracked)
-   ⚙️ Stable frame processing (no OpenCV crashes)
-   📸 Capture images from UI

------------------------------------------------------------------------

## Installation

``` bash
python3 -m venv venv
source venv/bin/activate
pip install picamera2 flask opencv-python numpy pillow
```

------------------------------------------------------------------------

## ▶️ Run

``` bash
python3 livestream.py
```

Open:

    http://localhost:5000

------------------------------------------------------------------------

## ROI (IMPORTANT)

Currently using manual ROI:

``` python
ROI = (200, 100, 600, 400)
```

-   Detection ONLY happens inside this box
-   Blue rectangle shows active region

------------------------------------------------------------------------

## Detection Pipeline

1.  Capture frame
2.  Extract ROI
3.  Validate ROI:
    -   not empty
    -   not too small
4.  Convert ROI → grayscale
5.  Resize → speed optimization
6.  Detect circles (HoughCircles)
7.  Filter detections
8.  Track bubbles
9.  Draw results
10. Stream frame

------------------------------------------------------------------------

## Debug Visualization

  Color         Meaning
  ------------- ---------------------
  🔴 Red        Raw detections
  🟡 Yellow     Filtered detections
  🟢 Green      Tracked bubbles
  🔵 Blue box   ROI

------------------------------------------------------------------------

## ⚙️ Key Parameters

### Bubble Detection

``` python
param1 = 120
param2 = 50
minRadius = 8
maxRadius = 40
```

Tune: - Too many detections → increase param2 - No detection → decrease
param2

------------------------------------------------------------------------

### Frame Control

``` python
TARGET_FPS = 20
CIRCLE_EVERY_N_FRAMES = 8
```

Set:

    CIRCLE_EVERY_N_FRAMES = 1

for debugging

------------------------------------------------------------------------

## 🛡 Stability Fixes Implemented

-   ROI empty check:

``` python
if roi.size == 0: continue
```

-   ROI size check:

``` python
if roi.shape[0] < 20 or roi.shape[1] < 20: continue
```

-   Motion detection safety:

``` python
if prev_frame is None or prev_frame.shape != gray.shape:
    prev_frame = gray.copy()
    continue
```

------------------------------------------------------------------------

## Color Fix

Removed incorrect color conversion:

``` python
# Removed: cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
```

------------------------------------------------------------------------

## Bubble Tracking

Each bubble gets unique ID:

``` python
tracked_bubbles[bid] = (cx, cy, r, timestamp)
```

-   Smooth movement applied
-   Old bubbles removed after timeout

------------------------------------------------------------------------

## Web Features

-   Live stream
-   Capture image
-   Start/stop recording (extendable)

------------------------------------------------------------------------

## Debug Tips

-   If no circles → lower param2
-   If too noisy → increase param2
-   If tracking unstable → increase smoothing
-   If detection off-center → adjust ROI

------------------------------------------------------------------------

## Outputs

-   Images: `.jpg`
-   Video (optional): `.mp4`

------------------------------------------------------------------------

## Future Improvements

-   Auto ROI (YOLO integration)
-   FPS overlay
-   Bubble count analytics
-   Save detection logs

------------------------------------------------------------------------

## Status

✔ Stable\
✔ Crash-free\
✔ Real-time capable

------------------------------------------------------------------------

## Notes

This version is optimized for: - Raspberry Pi 5 - Real-time
performance - Debug visibility

------------------------------------------------------------------------
