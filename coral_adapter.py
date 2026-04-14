#!/usr/bin/env python3
"""Google Coral Edge TPU helper utilities for stepwise integration.

This module is intentionally standalone so the existing OpenCV pipeline can stay
unchanged while Coral dependencies are introduced in controlled phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class CoralDetection:
    """Normalized detection result returned from an Edge TPU model."""

    label: str
    score: float
    box: Tuple[float, float, float, float]


class CoralAdapter:
    """Thin wrapper around pycoral APIs with safe fallback behavior."""

    def __init__(
        self,
        model_path: str,
        labels_path: Optional[str] = None,
        score_threshold: float = 0.5,
        top_k: int = 5,
    ) -> None:
        self.model_path = model_path
        self.labels_path = labels_path
        self.score_threshold = score_threshold
        self.top_k = top_k
        self._interpreter = None
        self._input_size: Optional[Tuple[int, int]] = None
        self._labels = {}

    @staticmethod
    def is_available() -> bool:
        """Return True when pycoral runtime is importable."""
        try:
            from pycoral.adapters import common  # noqa: F401
            from pycoral.utils.edgetpu import make_interpreter  # noqa: F401
        except Exception:
            return False
        return True

    def load(self) -> None:
        """Load model, allocate tensors, and optional labels mapping."""
        from pycoral.adapters import common
        from pycoral.utils.dataset import read_label_file
        from pycoral.utils.edgetpu import make_interpreter

        model = Path(self.model_path)
        if not model.is_file():
            raise FileNotFoundError(f"Coral model not found: {self.model_path}")

        self._interpreter = make_interpreter(str(model))
        self._interpreter.allocate_tensors()
        self._input_size = common.input_size(self._interpreter)

        if self.labels_path:
            labels_file = Path(self.labels_path)
            if labels_file.is_file():
                self._labels = read_label_file(str(labels_file))

    @property
    def loaded(self) -> bool:
        return self._interpreter is not None and self._input_size is not None

    def infer(self, rgb_frame: np.ndarray) -> List[CoralDetection]:
        """Run inference and return normalized detection objects.

        `rgb_frame` should be uint8 in RGB format.
        """
        if not self.loaded:
            raise RuntimeError("CoralAdapter.load() must be called before infer().")

        from pycoral.adapters import common, detect

        h, w = rgb_frame.shape[:2]
        in_w, in_h = self._input_size  # type: ignore[misc]

        resized = np.asarray(rgb_frame)
        if (w, h) != (in_w, in_h):
            import cv2

            resized = cv2.resize(rgb_frame, (in_w, in_h), interpolation=cv2.INTER_AREA)

        common.set_input(self._interpreter, resized)
        self._interpreter.invoke()

        objects = detect.get_objects(self._interpreter, self.score_threshold, scale_x=1.0, scale_y=1.0)
        detections: List[CoralDetection] = []
        for obj in objects[: self.top_k]:
            bbox = obj.bbox
            label = self._labels.get(obj.id, str(obj.id))
            detections.append(
                CoralDetection(
                    label=label,
                    score=float(obj.score),
                    box=(float(bbox.xmin), float(bbox.ymin), float(bbox.xmax), float(bbox.ymax)),
                )
            )

        return detections