#!/usr/bin/env bash
# Container entrypoint for openWakeWord training.
#
# Downloads required datasets to /data on first run (cached for re-runs),
# then executes the 3-step training pipeline: generate → augment → train.
#
# Volume mounts expected:
#   /data    — persistent dataset cache
#   /output  — receives the trained .onnx model
#
# Perry Kivolowitz, 2026. MIT License.

set -euo pipefail

CONFIG="${1:-/workspace/hey_asshole.yml}"
echo "=== openWakeWord training ==="
echo "Config: ${CONFIG}"
echo "GPU:    $(python3 -c 'import torch; print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU ONLY")')"
echo ""

# ---------------------------------------------------------------------------
# Step 0 — Download datasets to /data if not already cached
# ---------------------------------------------------------------------------

mkdir -p /data

# Pre-computed ACAV100M features (~1.7GB) — negative speech examples.
ACAV="/data/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
if [ ! -f "${ACAV}" ]; then
    echo ">>> Downloading ACAV100M features (~1.7GB)..."
    wget --progress=dot:giga -O "${ACAV}" \
        'https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy'
else
    echo ">>> ACAV100M features cached."
fi

# Validation set features (~200MB) — false positive evaluation.
VAL="/data/validation_set_features.npy"
if [ ! -f "${VAL}" ]; then
    echo ">>> Downloading validation features..."
    wget --progress=dot:giga -O "${VAL}" \
        'https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy'
else
    echo ">>> Validation features cached."
fi

# Background noise — synthetic generation.
# HuggingFace datasets library has too many version conflicts for
# reliable audio dataset downloads. Synthetic noise is deterministic,
# reproducible, and covers the frequency range needed for augmentation.
AUDIOSET_DIR="/data/audioset_16k"
if [ ! -d "${AUDIOSET_DIR}" ]; then
    echo ">>> Generating synthetic background noise (500 clips)..."
    python3 -c "
import numpy as np
import soundfile as sf
import os

os.makedirs('/data/audioset_16k', exist_ok=True)
rng = np.random.default_rng(123)
sr = 16000

for i in range(500):
    # 5-15 second clips of varied noise types.
    duration = rng.uniform(5.0, 15.0)
    n_samples = int(sr * duration)
    noise_type = rng.integers(0, 5)

    if noise_type == 0:
        # White noise.
        audio = rng.standard_normal(n_samples).astype(np.float32) * 0.3
    elif noise_type == 1:
        # Pink noise (1/f spectrum via cumulative sum + HP filter).
        white = rng.standard_normal(n_samples).astype(np.float32)
        pink = np.cumsum(white)
        pink -= np.mean(pink)
        pink /= (np.max(np.abs(pink)) + 1e-8)
        audio = pink * 0.3
    elif noise_type == 2:
        # Babble (sum of random frequency sinusoids = speech-like).
        audio = np.zeros(n_samples, dtype=np.float32)
        n_voices = rng.integers(5, 20)
        for _ in range(n_voices):
            freq = rng.uniform(100, 4000)
            phase = rng.uniform(0, 2 * np.pi)
            amp = rng.uniform(0.01, 0.05)
            t = np.arange(n_samples, dtype=np.float32) / sr
            audio += amp * np.sin(2 * np.pi * freq * t + phase)
    elif noise_type == 3:
        # Music-like (harmonic series with random fundamentals).
        audio = np.zeros(n_samples, dtype=np.float32)
        n_notes = rng.integers(3, 8)
        for _ in range(n_notes):
            fund = rng.uniform(80, 800)
            t = np.arange(n_samples, dtype=np.float32) / sr
            for h in range(1, 6):
                amp = rng.uniform(0.01, 0.04) / h
                audio += amp * np.sin(2 * np.pi * fund * h * t)
    else:
        # Brownian noise (random walk).
        steps = rng.standard_normal(n_samples).astype(np.float32)
        audio = np.cumsum(steps)
        audio -= np.mean(audio)
        audio /= (np.max(np.abs(audio)) + 1e-8)
        audio *= 0.3

    # Random gain variation.
    audio *= rng.uniform(0.1, 0.8)
    # Clip to valid range.
    audio = np.clip(audio, -1.0, 1.0)
    sf.write(f'/data/audioset_16k/bg_{i:05d}.wav', audio, sr)

print('Generated 500 synthetic background clips')
"
else
    echo ">>> Background noise cached."
fi

# Synthetic room impulse responses — generated mathematically.
# Exponential decay with random early reflections simulates rooms
# of varying size and absorption. Good enough for augmentation;
# avoids HuggingFace datasets library issues.
RIR_DIR="/data/mit_rirs"
if [ ! -d "${RIR_DIR}" ]; then
    echo ">>> Generating synthetic room impulse responses..."
    python3 -c "
import numpy as np
import soundfile as sf
import os

os.makedirs('/data/mit_rirs', exist_ok=True)
rng = np.random.default_rng(42)
sr = 16000

for i in range(50):
    # Vary room characteristics.
    rt60 = rng.uniform(0.2, 1.5)          # Reverb time 0.2-1.5s.
    length = int(sr * rt60)
    # Exponential decay envelope.
    t = np.arange(length, dtype=np.float32) / sr
    decay = np.exp(-6.9 * t / rt60)       # -60dB at rt60.
    # White noise shaped by decay.
    rir = (rng.standard_normal(length) * decay).astype(np.float32)
    # Sharp initial impulse.
    rir[0] = 1.0
    # Random early reflections (2-8 discrete echoes).
    n_reflections = rng.integers(2, 9)
    for _ in range(n_reflections):
        delay = rng.integers(int(0.001 * sr), int(0.05 * sr))
        if delay < length:
            rir[delay] += rng.uniform(0.2, 0.8) * rng.choice([-1, 1])
    # Normalize.
    rir /= np.max(np.abs(rir))
    sf.write(f'/data/mit_rirs/rir_{i:04d}.wav', rir, sr)

print(f'Generated 50 synthetic RIRs')
"
else
    echo ">>> RIRs cached."
fi

echo ""
echo "=== All datasets ready ==="
echo ""

# ---------------------------------------------------------------------------
# Step 1 — Generate synthetic clips via Piper TTS
# ---------------------------------------------------------------------------
echo ">>> Step 1/3: Generating synthetic 'Hey Asshole' clips..."
python3 /opt/openwakeword/openwakeword/train.py \
    --training_config "${CONFIG}" \
    --generate_clips

# ---------------------------------------------------------------------------
# Step 2 — Augment clips with noise, reverb, room simulation
# ---------------------------------------------------------------------------
echo ""
echo ">>> Step 2/3: Augmenting clips..."
python3 /opt/openwakeword/openwakeword/train.py \
    --training_config "${CONFIG}" \
    --augment_clips

# ---------------------------------------------------------------------------
# Step 3 — Train the classifier
# ---------------------------------------------------------------------------
echo ""
echo ">>> Step 3/3: Training model..."
# train.py auto-attempts TFLite conversion after saving ONNX, which
# fails without onnx_tf (intentionally not installed). The ONNX file
# is already saved before the conversion attempt, so we allow the
# non-zero exit and check for the .onnx file instead.
python3 /opt/openwakeword/openwakeword/train.py \
    --training_config "${CONFIG}" \
    --train_model || true

# ---------------------------------------------------------------------------
# Step 4 — Copy output to /output volume
# ---------------------------------------------------------------------------
echo ""
echo "=== Training complete ==="

MODEL_DIR="/workspace/my_custom_model"
if [ -d "${MODEL_DIR}" ]; then
    cp -v "${MODEL_DIR}"/*.onnx /output/ 2>/dev/null || true
    cp -v "${MODEL_DIR}"/*.tflite /output/ 2>/dev/null || true
    echo ""
    echo "Model(s) copied to /output/:"
    ls -la /output/*.onnx 2>/dev/null || echo "  (no .onnx files found)"
else
    echo "ERROR: Model output directory not found at ${MODEL_DIR}"
    echo "Checking for output in other locations..."
    find /workspace -name "*.onnx" -type f 2>/dev/null
    exit 1
fi
