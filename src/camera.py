"""Camera abstraction layer for both Pi camera and video file input."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import cv2


class CameraInterface(ABC):
    """Abstract camera interface."""

    @abstractmethod
    def start(self) -> None:
        """Initialize and start the camera."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop and release the camera."""
        pass

    @abstractmethod
    def capture_frame(self) -> Optional[tuple]:
        """Capture a frame. Returns (frame_bgr, frame_rgb) or None if failed."""
        pass

    @abstractmethod
    def is_running(self) -> bool:
        """Check if camera is running."""
        pass


class PiCamera(CameraInterface):
    """Raspberry Pi camera using Picamera2."""

    def __init__(self, frame_size: tuple = (640, 480)):
        self.frame_size = frame_size
        self.picam2 = None
        self.running = False

    def start(self) -> None:
        try:
            from picamera2 import Picamera2

            self.picam2 = Picamera2()
            camera_config = self.picam2.create_preview_configuration(
                main={"size": self.frame_size, "format": "RGB888"}
            )
            self.picam2.configure(camera_config)
            self.picam2.start()
            self.running = True
        except Exception as exc:
            print(f"Pi camera startup failed: {exc}")
            self.running = False

    def stop(self) -> None:
        if self.picam2:
            self.picam2.stop()
            self.picam2 = None
        self.running = False

    def capture_frame(self) -> Optional[tuple]:
        if not self.running or not self.picam2:
            return None
        try:
            frame_rgb = self.picam2.capture_array()
            if frame_rgb is None:
                return None
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            return (frame_bgr, frame_rgb)
        except Exception:
            return None

    def is_running(self) -> bool:
        return self.running


class VideoFileCamera(CameraInterface):
    """Camera that reads from a video file."""

    def __init__(self, video_path: str, frame_size: tuple = (640, 480)):
        self.video_path = Path(video_path)
        self.frame_size = frame_size
        self.cap = None
        self.running = False

    def start(self) -> None:
        if not self.video_path.exists():
            print(f"Video file not found: {self.video_path}")
            self.running = False
            return

        self.cap = cv2.VideoCapture(str(self.video_path))
        if not self.cap.isOpened():
            print(f"Failed to open video: {self.video_path}")
            self.running = False
            return

        self.running = True

    def stop(self) -> None:
        if self.cap:
            self.cap.release()
            self.cap = None
        self.running = False

    def capture_frame(self) -> Optional[tuple]:
        if not self.running or not self.cap:
            return None

        ret, frame_bgr = self.cap.read()
        if not ret:
            # Video ended, restart it
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame_bgr = self.cap.read()
            if not ret:
                return None

        # Resize to expected frame size
        if frame_bgr.shape[:2] != self.frame_size[::-1]:
            frame_bgr = cv2.resize(frame_bgr, self.frame_size)

        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        return (frame_bgr, frame_rgb)

    def is_running(self) -> bool:
        return self.running


def create_camera(
    camera_type: str = "pi", video_path: Optional[str] = None, frame_size: tuple = (640, 480)
) -> CameraInterface:
    """Factory function to create appropriate camera.

    Args:
        camera_type: "pi" for Raspberry Pi camera, "video" for video file
        video_path: Path to video file (required if camera_type="video")
        frame_size: Tuple of (width, height)

    Returns:
        CameraInterface instance
    """
    if camera_type == "video":
        if not video_path:
            raise ValueError("video_path required for video camera")
        return VideoFileCamera(video_path, frame_size)
    else:
        return PiCamera(frame_size)
