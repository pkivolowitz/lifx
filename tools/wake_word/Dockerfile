# openWakeWord custom wake word training environment.
#
# Builds a fully isolated training container with:
#   - Piper TTS for synthetic data generation
#   - PyTorch + CUDA for GPU-accelerated training
#   - openWakeWord training pipeline
#   - ONNX export (no TFLite / no TensorFlow needed)
#
# Volume mounts:
#   /data    — persistent cache for downloaded datasets (~3GB first run)
#   /output  — trained .onnx model written here
#
# Built and run by train.sh on the host machine.
# Perry Kivolowitz, 2026. MIT License.

FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# --- System dependencies ---------------------------------------------------
# espeak-ng: phoneme backend for Piper TTS
# ffmpeg: audio format conversion
# libsndfile1: soundfile Python library backend
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev \
    build-essential \
    git wget curl \
    ffmpeg espeak-ng libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# --- Python virtual environment --------------------------------------------
# Isolated from system Python — nothing leaks out.
RUN python3 -m venv /opt/oww
ENV PATH="/opt/oww/bin:${PATH}"

# --- PyTorch with CUDA 12.4 ------------------------------------------------
RUN pip install --no-cache-dir \
    torch torchaudio \
    --index-url https://download.pytorch.org/whl/cu124

# --- Clone openWakeWord and Piper sample generator --------------------------
RUN git clone --depth 1 https://github.com/dscripka/openWakeWord /opt/openwakeword \
    && git clone --depth 1 https://github.com/dscripka/piper-sample-generator /opt/piper-sample-generator

# --- Download Piper TTS voice model -----------------------------------------
# en-us-libritts-high: multi-speaker English model required by dscripka's
# fork of piper-sample-generator (v1.0.0 release, not v2.0.0).
RUN mkdir -p /opt/piper-sample-generator/models \
    && wget --progress=dot:giga -O \
       /opt/piper-sample-generator/models/en-us-libritts-high.pt \
       'https://github.com/rhasspy/piper-sample-generator/releases/download/v1.0.0/en-us-libritts-high.pt'

# --- Install openWakeWord (editable, for train.py access) -------------------
RUN pip install --no-cache-dir -e /opt/openwakeword

# --- Training dependencies -------------------------------------------------
# Pinned where openWakeWord setup.py requires specific versions.
# No tensorflow — we export ONNX only, skip TFLite conversion.
RUN pip install --no-cache-dir \
    "setuptools<71" \
    piper-phonemize \
    espeak-phonemizer \
    webrtcvad \
    mutagen==1.47.0 \
    torchinfo==1.8.0 \
    torchmetrics==1.2.0 \
    speechbrain==0.5.14 \
    audiomentations==0.33.0 \
    torch-audiomentations==0.11.0 \
    acoustics==0.2.6 \
    pronouncing==0.2.0 \
    deep-phonemizer==0.0.19 \
    onnx==1.14.0 \
    soundfile \
    pyyaml

# --- Download openWakeWord embedding models ---------------------------------
# These are the frozen feature extractors (melspectrogram + audio embedding).
# The classifier we train sits on top of these embeddings.
RUN mkdir -p /opt/openwakeword/openwakeword/resources/models \
    && wget --progress=dot:giga -O \
       /opt/openwakeword/openwakeword/resources/models/embedding_model.onnx \
       'https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx' \
    && wget --progress=dot:giga -O \
       /opt/openwakeword/openwakeword/resources/models/melspectrogram.onnx \
       'https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx'

# PyTorch 2.6+ defaults torch.load to weights_only=True, which blocks
# loading the Piper TTS model (saved with pickle, contains model classes).
# Patch generate_samples.py to allow full unpickling — the model file is
# from a trusted source (rhasspy GitHub release).
RUN sed -i 's/torch.load(model_path)/torch.load(model_path, weights_only=False)/' \
    /opt/piper-sample-generator/generate_samples.py

# piper-sample-generator's generate_samples.py must be importable
# by openWakeWord's train.py (top-level import, before config is read).
ENV PYTHONPATH="/opt/piper-sample-generator:${PYTHONPATH}"

WORKDIR /workspace
COPY entrypoint.sh /entrypoint.sh
COPY hey_asshole.yml /workspace/hey_asshole.yml
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
