#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/Documents/RnD_Camera/RnD_Camera
source venv/bin/activate

HOST="0.0.0.0"
PORT="5000"
RES_W="960"
RES_H="540"
FPS="30"

echo "Starting livestream on http://${HOST}:${PORT}"
python livestream.py --host "${HOST}" --port "${PORT}" --resolution "${RES_W}" "${RES_H}" --fps "${FPS}"
