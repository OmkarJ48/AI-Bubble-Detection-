# Raspberry Pi 5 Camera Module

Capture images, record videos, and livestream from the Raspberry Pi 5 camera module.

## Prerequisites
On Raspberry Pi 5, ensure the camera interface is enabled:
```bash
sudo raspi-config
# Go to Interface Options > Camera and enable it
```

## Installation

```bash

#Uninstall all packages
rm -rf venv

# Create the virtual environment
python3 -m venv --system-site-packages venv

# Activate it
source venv/bin/activate

# Install dependencies
pip install picamera2 av flask werkzeug
pip install flask pillow
```
# Catching syntax errors
python3 -m py_compile yourfile.py

## Usage

### Image Capture

**Capture a single image:**
```bash
python3 camera.py image
```
Creates: `image_YYYYMMDD_HHMMSS.jpg`

**Capture multiple images:**
```bash
python3 camera.py image --count 5 --output photo.jpg
```
Creates: `photo_1.jpg`, `photo_2.jpg`, etc.

**Custom resolution:**
```bash
python3 camera.py image --resolution 2560 1920
```

### Video Recording

**Record indefinitely (press Ctrl+C to stop):**
```bash
python3 camera.py video
```
Creates: `recording_YYYYMMDD_HHMMSS.mp4`

**Record for N seconds:**
```bash
python3 camera.py video --duration 30
```

**Custom output filename:**
```bash
python3 camera.py video --output my_video.mp4
```

**Custom resolution and framerate:**
```bash
python3 camera.py video --resolution 1280 720 --fps 24
```

### Livestream to Web Browser

**Start the livestream server:**
```bash
python3 livestream.py
```

**Access in web browser:**
- Open: `http://localhost:5000`
- Features:
  - Live video feed with real-time streaming
  - Capture images directly from the stream
  - Record video while streaming
  - Adjust camera settings

**Network access (from other devices):**
```bash
python3 livestream.py --host 0.0.0.0
```
Then access: `http://<raspberry-pi-ip>:5000`

**Custom port:**
```bash
python3 livestream.py --port 8080
```

**Full options:**
```bash
python3 livestream.py --help
```

## File Outputs

- **Images**: Saved as JPEG format (`.jpg`)
- **Videos**: Saved as MP4 format (`.mp4`)

All files are saved in the current directory with automatic timestamps.

## Help

For each script:
```bash
python3 camera.py --help
python3 livestream.py --help
python3 video.py --help
```