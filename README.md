# Leak Detector

This repository currently contains a small OpenCV-based leak detection prototype for a USB webcam. The script captures a clean background frame, compares each new frame against it, and highlights motion or bubbles large enough to pass a configurable contour-area threshold.

## Files

- `leak_detector.py` - main webcam leak detection script
- `requirements.txt` - Python dependency list
- `.gitignore` - local environment and runtime artifact exclusions

## How It Works

1. Open the default camera with `cv2.VideoCapture(0)`.
2. Wait briefly for the camera exposure to settle.
3. Capture the first blurred grayscale frame as the background.
4. Compare each live frame to that saved background.
5. Threshold and dilate the difference image.
6. Draw red boxes around contours larger than `MIN_BUBBLE_AREA`.
7. Show a live status overlay of either `STATUS: CLEAR` or `LEAK DETECTED`.

## Requirements

- Python 3.10 or newer is recommended
- A connected webcam or USB camera
- A desktop session that can display OpenCV windows via `cv2.imshow`

## Setup

### Windows PowerShell

```powershell
python -m venv .venv
venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python leak_detector.py
```

If script execution is blocked when activating the virtual environment, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### Linux or macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python leak_detector.py
```

## Usage Notes

- Start the program while the water and camera view are as still as possible.
- The first good frame becomes the reference background.
- Press `r` to recapture the background if the lighting changes or the camera moves.
- Press `q` to quit.

## Tunable Parameters

The current script keeps its configuration at the top of `main()`:

- `MIN_BUBBLE_AREA` filters out tiny contours caused by noise
- `THRESHOLD_VALUE` controls how different a pixel must be from the background to count as motion
- `BLUR_SIZE` smooths ripples and sensor noise before detection

You can also change the camera index in `cv2.VideoCapture(0)` if your leak camera is not the default device.

## Troubleshooting

- `ModuleNotFoundError: No module named 'cv2'`: activate the virtual environment and reinstall from `requirements.txt`
- `ImportError: numpy.core.multiarray failed to import`: on Raspberry Pi OS, a `opencv-python` wheel from `~/.local` can conflict with the distro-provided `numpy`/`cv2` packages. Use the project `.venv`, avoid mixing in `~/.local` OpenCV installs, and prefer the system OpenCV build when the venv was created with `--system-site-packages`
- Camera opens but no frames appear: check whether another app is already using the camera
- Too many false detections: increase `MIN_BUBBLE_AREA`, increase `THRESHOLD_VALUE`, or stabilize lighting
- Legitimate bubbles are missed: reduce `MIN_BUBBLE_AREA` or reduce `THRESHOLD_VALUE`
