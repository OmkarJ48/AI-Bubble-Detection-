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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate normalized MP4 proxy videos for bubble tuning"
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input video files to normalize, typically .MOV bubble captures",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing proxy files",
    )
    args = parser.parse_args()

    ensure_ffmpeg_available()

    for raw_input in args.inputs:
        input_path = Path(raw_input)
        output_path = normalize_video(input_path, force=args.force)
        print(f"{input_path.name} -> {output_path.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
