#!/usr/bin/env python3
"""Shared precision-first bubble tracking utilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

Detection = Tuple[int, int, int]


def distance(p1: Tuple[int, int], p2: Tuple[int, int]) -> float:
    return float(math.hypot(p1[0] - p2[0], p1[1] - p2[1]))


def detect_bubbles(small_gray, scale_x, scale_y, x_offset, y_offset) -> List[Detection]:
    """Detect circles in the reduced ROI and map centers back to full-frame coordinates."""
    small_gray = cv2.GaussianBlur(small_gray, (5, 5), 0)

    circles = cv2.HoughCircles(
        small_gray,
        cv2.HOUGH_GRADIENT,
        dp=1.6,
        minDist=20,
        param1=70,
        param2=10,
        minRadius=3,
        maxRadius=20,
    )

    results: List[Detection] = []
    if circles is None:
        return results

    circles = np.round(circles[0]).astype(int)
    for (x, y, r) in circles:
        if x < 0 or y < 0 or x >= small_gray.shape[1] or y >= small_gray.shape[0]:
            continue
        cx = int(x * scale_x) + x_offset
        cy = int(y * scale_y) + y_offset
        results.append((cx, cy, int(r)))
    return results


def validate_profile_path(profile_path: str) -> str:
    resolved_path = Path(profile_path)
    if not resolved_path.is_file():
        raise SystemExit(f"Profile file not found: {profile_path}")
    return str(resolved_path)


def resolve_profile_path(
    source_name: str,
    explicit_profile_path: Optional[str] = None,
    profiles_dir: str = "profiles",
    fallback_name: str = "Bubbles",
) -> Optional[str]:
    if explicit_profile_path:
        return validate_profile_path(explicit_profile_path)

    stem_name = Path(source_name).stem
    default_profile = Path(profiles_dir) / f"{stem_name}.json"
    if default_profile.is_file():
        return str(default_profile)

    fallback_profile = Path(profiles_dir) / f"{fallback_name}.json"
    if fallback_profile.is_file():
        return str(fallback_profile)

    return None


def load_profile_data(profile_path: str) -> Dict:
    with open(profile_path, "r", encoding="utf-8") as profile_file:
        data = json.load(profile_file)
    if not isinstance(data, dict):
        raise SystemExit(f"Profile must be a JSON object: {profile_path}")
    return data


def apply_profile_mapping(profile_section: Dict, mapping: Dict[str, str], target_globals: Dict) -> None:
    if not isinstance(profile_section, dict):
        return
    for key, global_name in mapping.items():
        if key in profile_section:
            target_globals[global_name] = profile_section[key]


def estimate_pipe_center_x(
    gray_roi,
    fallback_center_x: int,
    previous_center_x: Optional[int] = None,
    search_width_ratio: float = 0.35,
    smoothing: float = 0.8,
    max_offset_px: Optional[int] = None,
) -> int:
    """
    Estimate pipe center from near-vertical edges.
    Falls back to configured center when no stable edge is found.
    """
    roi_h, roi_w = gray_roi.shape[:2]
    search_half_width = int(roi_w * search_width_ratio / 2)
    x_left = max(0, fallback_center_x - search_half_width)
    x_right = min(roi_w, fallback_center_x + search_half_width)

    search = gray_roi[:, x_left:x_right]
    if search.size == 0:
        return fallback_center_x

    edges = cv2.Canny(search, 50, 150)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=40,
        minLineLength=max(20, int(roi_h * 0.25)),
        maxLineGap=15,
    )

    candidate_xs = []
    if lines is not None:
        for line in lines[:, 0]:
            x1, y1, x2, y2 = line
            dx = abs(x2 - x1)
            dy = abs(y2 - y1)
            if dy > 20 and dx <= 12:
                length = float(math.hypot(x2 - x1, y2 - y1))
                center_x = int((x1 + x2) / 2) + x_left
                candidate_xs.append((length, center_x))

    if candidate_xs:
        candidate_xs.sort(reverse=True, key=lambda item: item[0])
        estimated = int(np.mean([x for _, x in candidate_xs[:2]]))
    else:
        estimated = fallback_center_x

    if max_offset_px is not None:
        lower = fallback_center_x - int(max_offset_px)
        upper = fallback_center_x + int(max_offset_px)
        estimated = min(max(estimated, lower), upper)

    if previous_center_x is None:
        return estimated

    smoothed = int(smoothing * previous_center_x + (1.0 - smoothing) * estimated)
    return min(max(smoothed, 0), roi_w - 1)


@dataclass
class PipeGeometry:
    pipe_center_x: int
    pipe_lock_width: int
    pipe_top: int
    pipe_bottom: int
    pipe_exit_y: int
    spawn_y1: int
    spawn_y2: int
    count_y1: int
    count_y2: int


@dataclass
class TrackerConfig:
    lock_after_frames: int = 2
    lost_after_frames: int = 4
    min_upward_travel: int = 30
    max_match_distance: int = 70
    downward_tolerance: int = 10
    max_lateral_shift: int = 35
    max_step_distance: int = 80
    min_radius: int = 4
    max_radius: int = 20
    candidate_confirm_frames: int = 2
    candidate_match_distance: int = 50
    candidate_lost_after_frames: int = 1
    min_start_below_exit: int = 18


class PrecisionBubbleTracker:
    """Single-track precision-first bubble tracker with idle/candidate/active states."""

    def __init__(self, config: TrackerConfig):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.active_bubble: Optional[Dict] = None
        self.candidate_bubble: Optional[Dict] = None
        self.bubble_history: List[Dict] = []
        self.next_bubble_id = 1
        self.bubble_count_total = 0

    @property
    def state(self) -> str:
        if self.active_bubble is not None:
            return "active"
        if self.candidate_bubble is not None:
            return "candidate"
        return "idle"

    def _filter_detections(self, detections: List[Detection], g: PipeGeometry) -> List[Detection]:
        return [
            (cx, cy, r)
            for (cx, cy, r) in detections
            if self.config.min_radius <= r <= self.config.max_radius
            and abs(cx - g.pipe_center_x) <= g.pipe_lock_width
            and g.pipe_top <= cy <= g.pipe_bottom
        ]

    def _start_eligible(self, det: Detection, g: PipeGeometry) -> bool:
        cx, cy, _ = det
        return (
            abs(cx - g.pipe_center_x) <= g.pipe_lock_width
            and g.spawn_y1 <= cy <= g.spawn_y2
            and cy >= (g.pipe_exit_y + self.config.min_start_below_exit)
        )

    def _best_match(self, ref: Dict, detections: List[Detection], max_dist: int) -> Optional[Detection]:
        best_det = None
        best_dist = 1e9

        for det in detections:
            cx, cy, _ = det
            dx = cx - ref["cx"]
            dy = cy - ref["cy"]
            if dy > self.config.downward_tolerance:
                continue
            if abs(dx) > self.config.max_lateral_shift:
                continue
            step_dist = float(math.hypot(dx, dy))
            if step_dist > max_dist or step_dist > self.config.max_step_distance:
                continue
            if step_dist < best_dist:
                best_dist = step_dist
                best_det = det

        return best_det

    @staticmethod
    def _smooth_update(track: Dict, cx: int, cy: int, r: int, now: float) -> None:
        track["cx"] = int(0.7 * track["cx"] + 0.3 * cx)
        track["cy"] = int(0.7 * track["cy"] + 0.3 * cy)
        track["r"] = r
        track["last_seen"] = now
        track["seen_frames"] += 1
        track["lost_frames"] = 0
        track["min_y"] = min(track["min_y"], track["cy"])

    def step(self, detections: List[Detection], now: float, geometry: PipeGeometry) -> Dict:
        filtered = self._filter_detections(detections, geometry)
        starts = sorted(
            [det for det in filtered if self._start_eligible(det, geometry)],
            key=lambda d: (abs(d[0] - geometry.pipe_center_x), -d[2]),
        )

        started_id = None
        ended_event = None
        counted = False

        if self.active_bubble is None:
            if self.candidate_bubble is None:
                if starts:
                    cx, cy, r = starts[0]
                    self.candidate_bubble = {
                        "cx": cx,
                        "cy": cy,
                        "r": r,
                        "first_seen": now,
                        "last_seen": now,
                        "seen_frames": 1,
                        "lost_frames": 0,
                    }
            else:
                match = self._best_match(
                    self.candidate_bubble,
                    filtered,
                    max_dist=self.config.candidate_match_distance,
                )
                if match is not None:
                    self._smooth_update(self.candidate_bubble, match[0], match[1], match[2], now)
                else:
                    self.candidate_bubble["lost_frames"] += 1

                if self.candidate_bubble["lost_frames"] > self.config.candidate_lost_after_frames:
                    self.candidate_bubble = None

            if (
                self.candidate_bubble is not None
                and self.candidate_bubble["seen_frames"] >= self.config.candidate_confirm_frames
            ):
                bid = self.next_bubble_id
                self.active_bubble = {
                    "id": bid,
                    "cx": self.candidate_bubble["cx"],
                    "cy": self.candidate_bubble["cy"],
                    "r": self.candidate_bubble["r"],
                    "start_x": self.candidate_bubble["cx"],
                    "start_y": self.candidate_bubble["cy"],
                    "min_y": self.candidate_bubble["cy"],
                    "last_seen": now,
                    "seen_frames": self.candidate_bubble["seen_frames"],
                    "lost_frames": 0,
                    "hit_count_band": geometry.count_y1 <= self.candidate_bubble["cy"] <= geometry.count_y2,
                }
                started_id = bid
                self.next_bubble_id += 1
                self.candidate_bubble = None
        else:
            match = self._best_match(self.active_bubble, filtered, max_dist=self.config.max_match_distance)
            if match is not None:
                self._smooth_update(self.active_bubble, match[0], match[1], match[2], now)
                if geometry.count_y1 <= self.active_bubble["cy"] <= geometry.count_y2:
                    self.active_bubble["hit_count_band"] = True
            else:
                self.active_bubble["lost_frames"] += 1

        if self.active_bubble is not None and self.active_bubble["lost_frames"] >= self.config.lost_after_frames:
            traveled_up = self.active_bubble["start_y"] - self.active_bubble["min_y"]
            should_count = (
                self.active_bubble["seen_frames"] >= self.config.lock_after_frames
                and traveled_up >= self.config.min_upward_travel
                and bool(self.active_bubble["hit_count_band"])
            )

            if should_count:
                self.bubble_count_total += 1
                counted = True

            ended_event = {
                "id": self.active_bubble["id"],
                "counted": should_count,
                "start_x": self.active_bubble["start_x"],
                "start_y": self.active_bubble["start_y"],
                "end_x": self.active_bubble["cx"],
                "end_y": self.active_bubble["cy"],
                "min_y": self.active_bubble["min_y"],
                "seen_frames": self.active_bubble["seen_frames"],
                "traveled_up": traveled_up,
                "hit_count_band": self.active_bubble["hit_count_band"],
                "ended_at": now,
            }
            self.bubble_history.append(ended_event)
            self.active_bubble = None

        return {
            "raw_detections": detections,
            "filtered_detections": filtered,
            "start_candidates": starts,
            "state": self.state,
            "started_id": started_id,
            "counted": counted,
            "ended_event": ended_event,
        }
