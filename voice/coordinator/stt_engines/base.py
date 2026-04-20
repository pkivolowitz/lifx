"""Shared primitives for STT engines.

Defines the ``STTEngine`` protocol every engine implements, a
``STTEngineLoadError`` that engines raise on load failure (so the
facade can distinguish load failures from runtime transcription
failures), a shared PCM→WAV conversion helper, and the state-file
writer that records which engine is currently serving transcriptions
so the morning report can render degraded state in red.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

logger: logging.Logger = logging.getLogger("glowup.voice.stt")


STT_STATE_DIR: Path = Path.home() / ".glowup"
STT_STATE_FILE: Path = STT_STATE_DIR / "stt_state.json"


class STTEngineLoadError(RuntimeError):
    """Raised by an engine's ``load()`` when it cannot initialize.

    Distinct from generic exceptions so the facade can decide whether
    to try the fallback engine.  Runtime transcription failures use
    plain exceptions and are not classified as load failures.
    """


@runtime_checkable
class STTEngine(Protocol):
    """Protocol every STT engine implements.

    Concrete engines load their model in ``load()`` (called once by the
    facade) and produce text in ``transcribe()``.  Engines must be
    self-contained: they own their model, their cache, and their
    logging.  They do not write the state file — the facade owns that.
    """

    name: str

    @classmethod
    def is_available(cls) -> bool:
        """Return True if this engine can be imported on this host.

        Used to skip unavailable engines (e.g. MLX on non-Apple-Silicon)
        without treating them as load failures.
        """
        ...

    def load(self) -> None:
        """Load the model into memory.  Raise ``STTEngineLoadError`` on failure."""
        ...

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        """Return the transcription of raw 16-bit signed LE mono PCM."""
        ...


def pcm_to_wav(
    pcm: bytes,
    sample_rate: int,
    ffmpeg_bin: str,
) -> Optional[str]:
    """Convert raw 16-bit signed LE mono PCM to a WAV temp file.

    Returns the WAV file path on success (caller is responsible for
    deleting it) or ``None`` on conversion failure.  The raw PCM temp
    file is deleted before return regardless of outcome.
    """
    raw_fd, raw_path = tempfile.mkstemp(suffix=".raw")
    wav_path: str = raw_path.replace(".raw", ".wav")

    try:
        with os.fdopen(raw_fd, "wb") as f:
            f.write(pcm)

        result = subprocess.run(
            [
                ffmpeg_bin, "-y",
                "-f", "s16le",
                "-ar", str(sample_rate),
                "-ac", "1",
                "-i", raw_path,
                wav_path,
            ],
            capture_output=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.warning(
                "ffmpeg conversion failed: %s",
                result.stderr.decode("utf-8", errors="replace")[:200],
            )
            return None
        return wav_path
    finally:
        try:
            os.unlink(raw_path)
        except OSError:
            pass


def write_state(
    engine: str,
    fallback_reason: str = "",
    primary_engine: str = "",
) -> None:
    """Atomically write the STT engine state file.

    Consumed by ``services/morning_report.py`` (via SSH+cat) to decide
    whether Daedalus STT is healthy.  Degraded state = ``engine`` is
    not the configured primary, OR ``fallback_reason`` is non-empty.

    Args:
        engine:          Name of the engine currently serving
                         transcriptions (e.g. "mlx-whisper",
                         "faster-whisper").
        fallback_reason: Empty if the primary engine is live; the
                         primary-engine load error message if the
                         facade fell back.
        primary_engine:  The configured primary engine name.  Saved so
                         the reader (morning report) does not need to
                         duplicate the config.
    """
    STT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "engine": engine,
        "primary_engine": primary_engine or engine,
        "fallback_reason": fallback_reason,
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Per-writer unique tmp name — multiple coordinators (or, pathologically,
    # a rogue duplicate launchd instance) must not race on a shared `.tmp`
    # path where the second renamer can hit FileNotFoundError after the first
    # has already moved the file into place.  mkstemp gives us an exclusive
    # inode in the same directory as the target, so os.replace is atomic
    # within the filesystem and per-writer-isolated across processes.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=STT_STATE_FILE.name + ".",
        suffix=".tmp",
        dir=str(STT_STATE_DIR),
    )
    tmp_path: Path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp_path, STT_STATE_FILE)
    except BaseException:
        # Leave the target untouched and clean up our tmp on any failure
        # (including KeyboardInterrupt) so /.glowup does not accumulate
        # orphan tmp files across crashes.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
