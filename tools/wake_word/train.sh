#!/usr/bin/env bash
# Build and run the openWakeWord training container on ML.
#
# Usage (from this directory on ML):
#   ./train.sh              # build image + train
#   ./train.sh --build-only # just build the Docker image
#   ./train.sh --run-only   # run without rebuilding
#
# Volume mounts:
#   ~/oww_data/    — cached datasets (~3GB, persists between runs)
#   ~/oww_output/  — trained .onnx model lands here
#
# Pinned to GPU 1 (RTX 4070 Ti Super). The 5070 Ti (GPU 0) uses a
# subtly different bfloat16 definition than the 4070s — mixing
# architectures in the same training run causes silent numerical
# divergence. One 4070 Ti Super is more than enough for this model.
#
# Perry Kivolowitz, 2026. MIT License.

set -euo pipefail

IMAGE="oww-trainer:latest"
DATA_DIR="${HOME}/oww_data"
OUTPUT_DIR="${HOME}/oww_output"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse arguments.
BUILD=true
RUN=true
if [ "${1:-}" = "--build-only" ]; then
    RUN=false
elif [ "${1:-}" = "--run-only" ]; then
    BUILD=false
fi

# Ensure output directories exist on the host.
mkdir -p "${DATA_DIR}" "${OUTPUT_DIR}"

# --- Build ------------------------------------------------------------------
if [ "${BUILD}" = true ]; then
    echo "=== Building Docker image: ${IMAGE} ==="
    docker build -t "${IMAGE}" "${SCRIPT_DIR}"
    echo ""
fi

# --- Run --------------------------------------------------------------------
if [ "${RUN}" = true ]; then
    echo "=== Starting training ==="
    echo "  Data cache: ${DATA_DIR}"
    echo "  Output:     ${OUTPUT_DIR}"
    echo ""

    WORKSPACE_DIR="${HOME}/oww_workspace"
    mkdir -p "${WORKSPACE_DIR}"

    docker run --rm \
        --gpus '"device=1"' \
        --shm-size=8g \
        -v "${DATA_DIR}:/data" \
        -v "${OUTPUT_DIR}:/output" \
        -v "${WORKSPACE_DIR}:/workspace" \
        "${IMAGE}"

    echo ""
    echo "=== Done ==="
    echo "Model output:"
    ls -la "${OUTPUT_DIR}"/*.onnx 2>/dev/null || echo "  (no .onnx files found — check logs above)"
fi
