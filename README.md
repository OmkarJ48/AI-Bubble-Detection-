# Bubble Detection

AI-powered bubble/leak detection for water systems using Edge Impulse models, served as a web app. The service runs inference on frames from a Raspberry Pi camera (or video file for testing) and streams annotated video over MJPEG to a browser.

## What's New

This is now **AI-based detection** using Edge Impulse trained models instead of classical computer vision. It includes:

- **Edge Impulse integration** — Deploy your own trained bubble detection model
- **Camera abstraction** — Seamlessly switch between Pi camera and video files for testing/CI
- **Fallback detector** — Automatic fallback to classical CV if Edge Impulse model unavailable
- **Flexible deployment** — Run locally for dev, on Pi for production

## Project Structure

```
.
├── README.md                    # This file
├── requirements.txt             # Python dependencies
├── .gitignore                   # Git ignore rules
├── src/
│   ├── leak_detector.py         # FastAPI service & detection loop
│   ├── camera.py                # Camera abstraction (Pi / video file)
│   └── ai_detector.py           # AI detector (Edge Impulse + fallback)
├── models/
│   └── bubble_detection/        # Place your Edge Impulse model here
├── templates/
│   └── leak_detector.html       # Web UI
└── static/
    ├── leak_detector.js         # Frontend logic
    └── leak_detector.css        # Styling
```

## How It Works

1. **Camera input** — Frames come from Picamera2 (Pi) or OpenCV (video file)
2. **AI inference** — Each frame is passed to the Edge Impulse model
3. **Detection** — Bubbles are identified; bounding boxes drawn on frame
4. **Event counting** — Count increments on detection state transitions
5. **Stream** — Annotated video served as MJPEG at `/stream.mjpg`

## Requirements

### For Raspberry Pi (production)

- Raspberry Pi with camera module
- Raspberry Pi OS Bookworm or newer
- Python 3.10 or newer
- An Edge Impulse trained bubble detection model

### For local development/testing

- Python 3.10 or newer
- OpenCV (for reading video files)
- Video samples (e.g., `Bubbles.mp4`) for testing

## Setup

### Step 1: System packages (Raspberry Pi only)

```bash
sudo apt update
sudo apt install -y python3-libcamera python3-picamera2 python3-opencv
```

### Step 2: Python environment

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Step 3: Get your Edge Impulse model

1. Go to [edgeimpulse.com](https://edgeimpulse.com) and create/train a bubble detection model
2. Export as **Python library**
3. Extract and place in `models/bubble_detection/`
4. Ensure `models/bubble_detection/edge_impulse_linux/` exists

### Step 4: Run

**On Raspberry Pi (Pi camera):**
```bash
python src/leak_detector.py
```

**On laptop (video file):**
```bash
export CAMERA_TYPE=video
export VIDEO_PATH=/path/to/Bubbles.mp4
python src/leak_detector.py
```

Then open `http://localhost:5000` in a browser.

## Configuration

Control behavior via environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CAMERA_TYPE` | `pi` | `"pi"` for Raspberry Pi, `"video"` for video file |
| `VIDEO_PATH` | None | Path to video file (required if `CAMERA_TYPE=video`) |
| `AI_MODEL_PATH` | `models/bubble_detection` | Path to Edge Impulse model directory |

Example:
```bash
CAMERA_TYPE=video VIDEO_PATH=test.mp4 python src/leak_detector.py
```

## HTTP API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Web UI |
| `GET` | `/stream.mjpg` | MJPEG video stream |
| `GET` | `/status` | JSON: status, count, camera info, detector info, errors |
| `POST` | `/reset-count` | Reset the event counter |

## How to train a bubble detection model

1. Collect videos of bubbles in your system (or use samples in history)
2. Annotate frames with bounding boxes around bubbles using Edge Impulse
3. Train object detection model (YOLO or MobileNet)
4. Export as Python library
5. Place in `models/bubble_detection/`
6. Test by running the service with `CAMERA_TYPE=video`

See [Edge Impulse docs](https://docs.edgeimpulse.com/docs) for detailed training steps.

## Fallback behavior

If the Edge Impulse model is not found or fails to load:
- The service automatically falls back to **classical CV** (frame differencing)
- Useful for testing without a trained model
- Check `/status` endpoint to see which detector is active

## Testing

Test with video files locally (no Pi camera needed):

```bash
CAMERA_TYPE=video VIDEO_PATH=Bubbles.mp4 python src/leak_detector.py
```

The video will loop, making it easy to test detection logic.

## Performance & optimization

- **On Pi 4 with TPU accelerator**: ~50ms per inference (20 FPS)
- **On Pi 4 without accelerator**: ~200-500ms per inference (2-5 FPS)
- To use Coral TPU, uncomment `pycoral` in `requirements.txt` and modify `ai_detector.py`

## Troubleshooting

- **"Model directory not found"** — Place your Edge Impulse export in `models/bubble_detection/`
- **"Failed to import Edge Impulse model"** — Check the model export includes `edge_impulse_linux` module
- **Camera not found** — Check `/status` for `camera_error`; on Pi, verify camera is enabled: `sudo raspi-config`
- **Slow inference** — Consider a TPU accelerator or reducing frame resolution
- **No stream** — Check browser console and `/status` endpoint for errors

## License

See LICENSE file (if present).

## Contributing

AI-based bubble detection is an open problem. Contributions welcome:
- Better training datasets
- Optimized Edge Impulse models
- TPU/accelerator support
- Web UI improvements
