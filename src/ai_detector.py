"""AI-based bubble detection using Edge Impulse model."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


class AIDetector(ABC):
    """Abstract AI detector interface."""

    @abstractmethod
    def load_model(self, model_path: str) -> bool:
        """Load the AI model. Returns True if successful."""
        pass

    @abstractmethod
    def detect(self, frame_rgb: np.ndarray) -> dict:
        """Run detection on frame. Returns dict with results."""
        pass

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if model is loaded and ready."""
        pass


class EdgeImpulseDetector(AIDetector):
    """Bubble detection using Edge Impulse trained model.

    To use this:
    1. Train a bubble detection model on Edge Impulse (https://edgeimpulse.com)
    2. Export as Python library
    3. Extract and place in models/ directory
    4. Update model_path to point to the Edge Impulse library
    """

    def __init__(self):
        self.model = None
        self.ready = False
        self.model_path = None

    def load_model(self, model_path: str) -> bool:
        """Load Edge Impulse model.

        Args:
            model_path: Path to Edge Impulse library (e.g., models/bubble_detection/)
        """
        try:
            model_dir = Path(model_path)
            if not model_dir.exists():
                print(f"Model directory not found: {model_path}")
                return False

            # Try to import the Edge Impulse library
            # This assumes the model was exported as a Python library
            import sys

            sys.path.insert(0, str(model_dir))

            # Import the model module (adjust based on Edge Impulse export)
            try:
                import edge_impulse_linux

                self.model = edge_impulse_linux
                self.model_path = model_path
                self.ready = True
                print(f"Loaded Edge Impulse model from {model_path}")
                return True
            except ImportError:
                print(f"Failed to import Edge Impulse model from {model_path}")
                print("Make sure the model directory contains the Edge Impulse Python library")
                return False

        except Exception as exc:
            print(f"Error loading model: {exc}")
            return False

    def detect(self, frame_rgb: np.ndarray) -> dict:
        """Run detection on frame.

        Args:
            frame_rgb: RGB frame from camera

        Returns:
            dict with keys:
                - 'detected': bool, True if bubbles detected
                - 'confidence': float, 0-1 confidence score
                - 'boxes': list of bounding boxes (x, y, w, h)
                - 'labels': list of detected class labels
        """
        if not self.ready or not self.model:
            return {"detected": False, "confidence": 0.0, "boxes": [], "labels": []}

        try:
            # Convert frame to format expected by Edge Impulse
            # This is model-specific and depends on training setup
            resized = cv2.resize(frame_rgb, (320, 320))  # Adjust based on model input size
            img_array = np.expand_dims(resized, axis=0)

            # Run inference
            results = self.model.classify(resized)

            # Parse results (format depends on Edge Impulse model type)
            # This is a template - adjust based on your specific model output
            detected = False
            confidence = 0.0
            boxes = []
            labels = []

            if results and "results" in results:
                for result in results["results"]:
                    if result.get("label") == "bubbles" or "bubble" in result.get("label", "").lower():
                        detected = True
                        confidence = max(confidence, result.get("value", 0.0))

            return {
                "detected": detected,
                "confidence": confidence,
                "boxes": boxes,
                "labels": labels,
            }

        except Exception as exc:
            print(f"Detection error: {exc}")
            return {"detected": False, "confidence": 0.0, "boxes": [], "labels": []}

    def is_ready(self) -> bool:
        return self.ready


class FallbackDetector(AIDetector):
    """Fallback detector using classical CV when AI model not available."""

    def __init__(self, min_area: int = 50, threshold: int = 25):
        self.ready = True
        self.min_area = min_area
        self.threshold = threshold
        self.background = None

    def load_model(self, model_path: str) -> bool:
        # Fallback doesn't need a model
        self.ready = True
        return True

    def detect(self, frame_rgb: np.ndarray) -> dict:
        """Fallback frame-differencing detection."""
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.background is None:
            self.background = gray.copy()
            return {"detected": False, "confidence": 0.0, "boxes": [], "labels": []}

        frame_delta = cv2.absdiff(self.background, gray)
        thresh = cv2.threshold(frame_delta, self.threshold, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        detected = False
        boxes = []
        confidence = 0.0

        for contour in contours:
            area = cv2.contourArea(contour)
            if area >= self.min_area:
                detected = True
                x, y, w, h = cv2.boundingRect(contour)
                boxes.append((x, y, w, h))
                confidence = max(confidence, min(area / 1000, 1.0))

        return {
            "detected": detected,
            "confidence": confidence,
            "boxes": boxes,
            "labels": ["bubble"] * len(boxes),
        }

    def is_ready(self) -> bool:
        return self.ready
