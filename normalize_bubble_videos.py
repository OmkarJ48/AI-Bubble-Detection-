#!/usr/bin/env python3
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 30
DEFAULT_CRF = 20
DEFAULT_DATASET_DIR = "Prototype Dataset 1"


def build_output_path(input_path: Path) -> Path:
    return input_path.with_suffix(".mp4")


def ensure_ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed or not available on PATH")


def normalize_video(input_path: Path, force: bool) -> Path:
    if not input_path.is_file():
        raise SystemExit(f"Video file not found: {input_path}")

    output_path = build_output_path(input_path)
    if output_path.exists() and not force:
        raise SystemExit(
            f"Output already exists: {output_path}. "
            "Use --force to overwrite it."
        )

    cmd = [
        "ffmpeg",
        "-y" if force else "-n",
        "-i",
        str(input_path),
        "-vf",
        f"fps={DEFAULT_FPS},scale={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:flags=lanczos",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(DEFAULT_CRF),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(output_path),
    ]

    subprocess.run(cmd, check=True)
    return output_path


def resolve_inputs(inputs: list[str], dataset_dir: str | None) -> list[Path]:
    resolved_inputs = [Path(raw_input) for raw_input in inputs]

    if dataset_dir is not None:
        dataset_path = Path(dataset_dir)
        if not dataset_path.is_dir():
            raise SystemExit(f"Dataset directory not found: {dataset_dir}")
        resolved_inputs.extend(sorted(dataset_path.glob("*.MOV")))

    unique_inputs = []
    seen = set()
    for path in resolved_inputs:
        normalized = str(path)
        if normalized not in seen:
            unique_inputs.append(path)
            seen.add(normalized)

    if not unique_inputs:
        raise SystemExit("No input videos provided")

    return unique_inputs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate normalized MP4 proxy videos for bubble tuning"
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Input video files to normalize, typically .MOV bubble captures",
    )
    parser.add_argument(
        "--dataset-dir",
        default=None,
        help=(
            "Normalize every .MOV file in a dataset directory. "
            f"Example: --dataset-dir \"{DEFAULT_DATASET_DIR}\""
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing proxy files",
    )
    args = parser.parse_args()

    ensure_ffmpeg_available()

    for input_path in resolve_inputs(args.inputs, args.dataset_dir):
        output_path = normalize_video(input_path, force=args.force)
        print(f"{input_path} -> {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
