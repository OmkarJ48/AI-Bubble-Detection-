# Leak Detector

An OpenCV leak detection prototype for the Raspberry Pi camera, served as a small
web app. The service captures a clean background frame inside a region of interest
(ROI), compares every new frame against it, and highlights differences large enough
to pass a contour-area threshold. The annotated video is streamed to a browser over
MJPEG, where the ROI can be dragged and resized live.

This is classical computer vision (frame differencing), not machine learning. There
is no model in this repository.

## Files

| Path | Purpose |
| --- | --- |
| `leak_detector.py` | FastAPI service, camera capture loop, and detection logic |
| `templates/leak_detector.html` | Single-page web UI |
| `static/leak_detector.js` | Stream wiring, ROI drag/resize, status polling |
| `static/leak_detector.css` | UI styling |
| `requirements.txt` | pip-installable Python dependencies |
| `.gitignore` | Local environment and runtime artifact exclusions |

## How It Works

1. `Picamera2` opens the camera at 640x480 in `RGB888` and warms up for two seconds.
2. A background thread captures frames continuously.
3. Each frame is converted to grayscale and Gaussian-blurred, then cropped to the ROI.
4. The first ROI crop after start (or after a reset) is stored as the background.
5. Every later frame is diffed against that background, thresholded, and dilated.
6. Contours larger than `MIN_BUBBLE_AREA` are drawn as red boxes on the full frame.
7. The running count increments when detection transitions from active back to clear.
8. Frames are JPEG-encoded and served as `multipart/x-mixed-replace` at `/stream.mjpg`.

## Requirements

- Raspberry Pi with a camera module (this build depends on `picamera2`; it will not
  run on a laptop or in CI without a camera abstraction layer)
- Raspberry Pi OS Bookworm or newer
- Python 3.10 or newer
- A browser on the same network as the Pi

## Setup

`picamera2` and `libcamera` are **system** packages, not pip packages. Install them
with apt first, then create the virtual environment with `--system-site-packages` so
the venv can see them:

```bash
sudo apt update
sudo apt install -y python3-libcamera python3-picamera2

python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python leak_detector.py
```

Then open `http://<pi-ip>:5000` in a browser. The Pi's address is printed on startup.

On Raspberry Pi OS, prefer the distro OpenCV (`sudo apt install -y python3-opencv`)
over the pip wheel — see Troubleshooting below.

## Usage

- Start the service while the water and the camera view are as still as possible.
  The first good frame becomes the reference background.
- **Drag the ROI box** on the video to move the monitored region. **Drag the circular
  handle** at its bottom-right corner to resize it. Moving or resizing the ROI resets
  the background automatically.
- **Reset Background** recaptures the reference frame. Do this after any lighting
  change, camera nudge, or water-level change.
- **Reset Count** returns the cycle counter to zero.
- Pressing **Space** anywhere on the page also resets the background.

The detection status and count are currently only visible on the video itself — red
boxes for detections, and the count burnt into the top-right corner of the frame.

## HTTP API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Web UI |
| `GET` | `/stream.mjpg` | MJPEG video stream |
| `GET` | `/status` | JSON: status text, count, ROI, frame size, last frame time, camera error |
| `POST` | `/reset-background` | Clear the stored background frame |
| `POST` | `/reset-count` | Reset the cycle counter |
| `POST` | `/roi` | Set the ROI. Body: `{"x": int, "y": int, "width": int, "height": int}` |

The service binds `0.0.0.0:5000` with no authentication. Run it on a trusted network
only, or put it behind a reverse proxy.

## Tunable Parameters

These are module-level constants at the top of `leak_detector.py`. Changing them
requires a restart:

| Constant | Default | Effect |
| --- | --- | --- |
| `MIN_BUBBLE_AREA` | `50` | Minimum contour area, in pixels, that counts as a detection |
| `THRESHOLD_VALUE` | `25` | How different a pixel must be from the background to register |
| `BLUR_SIZE` | `(21, 21)` | Gaussian blur kernel; smooths ripples and sensor noise |
| `FRAME_SIZE` | `(640, 480)` | Capture resolution |
| `ROI_TOP_LEFT` / `ROI_BOTTOM_RIGHT` | `(279, 232)` / `(349, 302)` | Starting ROI |
| `MIN_ROI_SIZE` | `20` | Smallest ROI edge the UI can produce |
| `JPEG_QUALITY` | `85` | Stream encoding quality |
| `PORT` | `5000` | Listening port |

## Known Limitations

Worth knowing before relying on this for anything:

- **The background is a single static frame.** Any lighting drift, camera movement,
  or water-level change invalidates it until someone resets it manually.
- **The count is not a bubble count.** It increments on the falling edge of "any
  motion in the ROI", so a continuous stream of bubbles counts once, and a single
  noisy frame also counts once. There is no debounce or minimum event duration.
- **Detection is not bubble-specific.** There is no shape, size-band, or
  upward-motion check, so a hand, a shadow, or a ripple registers as a detection.
- **Nothing is persisted.** The count resets on restart and there is no event log.
- **Failures are quiet.** If the camera fails to start, the page loads black with no
  message. If the capture thread dies, the stream freezes on its last frame while
  `/status` continues to report the last known state.
- **Sensitivity is not adjustable from the UI** — only the ROI is.

## Troubleshooting

- `ModuleNotFoundError: No module named 'picamera2'`: the venv was created without
  `--system-site-packages`, or the apt packages are missing. See Setup.
- `ImportError: numpy.core.multiarray failed to import`: an `opencv-python` wheel
  from `~/.local` is conflicting with the distro-provided `numpy`/`cv2`. Use the
  project `.venv`, avoid `~/.local` OpenCV installs, and prefer the system OpenCV
  build when the venv was created with `--system-site-packages`.
- Camera opens but no frames appear: check whether another process is holding the
  camera.
- Too many false detections: increase `MIN_BUBBLE_AREA`, increase `THRESHOLD_VALUE`,
  or stabilize the lighting.
- Legitimate bubbles are missed: reduce `MIN_BUBBLE_AREA` or `THRESHOLD_VALUE`.
- Stream is black but the page loads: check `/status` for a `camera_error` value.
