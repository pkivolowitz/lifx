"""Speech-to-text facade for the voice coordinator.

Selects a primary STT engine from config, loads a fallback engine
alongside it (pre-warmed so transitions are latency-free), and writes
``~/.glowup/stt_state.json`` so the morning report can detect and
render degraded state.

Each engine load runs under a deadline: if the configured
``model_root`` path (typically a USB/TB-attached disk like Mini-Dock)
stalls — common if a macOS accessory-permission prompt fires before a
GUI session exists — the facade abandons the stalled load and retries
against the engine's HF cache on internal disk.  This keeps the
coordinator startable at boot before the user has logged in, even
when the preferred external storage is momentarily unreachable.

Engine implementations live in ``voice.coordinator.stt_engines``.
See ``docs/36-stt-stack.md`` for stack design and configuration.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "3.1"

import logging
import os
import threading
from typing import Any, Optional, Tuple

from voice.coordinator.stt_engines import (
    FasterWhisperEngine,
    MLXWhisperEngine,
    MockEngine,
    STTEngine,
    STTEngineLoadError,
    write_state,
)

logger: logging.Logger = logging.getLogger("glowup.voice.stt")


# Default per-engine load deadlines (seconds).  Overridable via the
# ``stt`` config block with ``primary_load_timeout_s`` /
# ``fallback_load_timeout_s``.  Both default to 60s — MLX-Whisper
# JIT-compiles Metal kernels on first run after a reboot and
# faster-whisper's 1.5 GB CT2 blob takes real wallclock time to load
# from disk when the rest of the boot is fighting for I/O.  The
# 2026-04-20 reboot validation observed a 23s faster-whisper load at
# load avg 139; a 30s deadline tripped there even though the dock was
# not actually stuck.  60s is the safer default — set lower in config
# if a specific host wants faster failover.
DEFAULT_PRIMARY_LOAD_TIMEOUT_S: float = 60.0
DEFAULT_FALLBACK_LOAD_TIMEOUT_S: float = 60.0


# Name → factory.  A factory returns an un-loaded engine instance
# configured with the supplied model / model_path / language.
_ENGINE_FACTORIES: dict = {
    "mlx-whisper": lambda model, model_path, lang, _device, _ct: MLXWhisperEngine(
        model=model, model_path=model_path, language=lang,
    ),
    "faster-whisper": lambda model, model_path, lang, device, ct: FasterWhisperEngine(
        model=model, model_path=model_path, language=lang,
        device=device, compute_type=ct,
    ),
}


def _resolve_model_path(
    model_root: str,
    engine_name: str,
    model: str,
) -> Optional[str]:
    """Return an on-disk model path if it exists under ``model_root``.

    Convention: ``<model_root>/<engine_name>/<model>/``.  If the
    directory is not present, return None so the engine falls back to
    its own discovery (typically the HF cache).
    """
    if not model_root:
        return None
    candidate: str = os.path.join(model_root, engine_name, model)
    return candidate if os.path.isdir(candidate) else None


def _build_engine_at_path(
    engine_name: str,
    model: str,
    model_path: Optional[str],
    language: str,
    device: str,
    compute_type: str,
) -> STTEngine:
    """Instantiate an un-loaded engine with an explicit model_path.

    ``model_path=None`` tells the engine to use its own default model
    discovery (HF cache).
    """
    factory = _ENGINE_FACTORIES.get(engine_name)
    if factory is None:
        raise STTEngineLoadError(
            f"Unknown STT engine '{engine_name}' — "
            f"valid: {sorted(_ENGINE_FACTORIES)}"
        )
    return factory(model, model_path, language, device, compute_type)


class LoadTimeout(RuntimeError):
    """Raised by ``_load_with_deadline`` when an engine load exceeds
    its allotted time.  Named distinctly from ``TimeoutError`` so
    callers can tell a facade-enforced deadline apart from an OS
    timeout bubbling up from elsewhere."""


def _load_with_deadline(engine: STTEngine, timeout_s: float) -> None:
    """Run ``engine.load()`` with a hard deadline.

    On timeout, raises ``LoadTimeout``.  The in-flight load thread is
    daemonised and abandoned — this is deliberate.  An abandoned
    thread stuck in a macOS accessory prompt is harmless: it holds no
    locks we care about, the engine instance is discarded, and daemon
    threads die when the coordinator exits.  ``ThreadPoolExecutor``
    is not used because its worker threads are non-daemon in
    Python 3.9+, which would prevent the process from exiting cleanly
    while an abandoned load thread is still blocked.
    """
    holder: dict = {"exc": None, "done": False}

    def worker() -> None:
        try:
            engine.load()
        except BaseException as exc:  # noqa: BLE001 — we re-raise below
            holder["exc"] = exc
        finally:
            holder["done"] = True

    t = threading.Thread(
        target=worker,
        name=f"stt-load-{engine.name}",
        daemon=True,
    )
    t.start()
    t.join(timeout=timeout_s)

    if not holder["done"]:
        raise LoadTimeout(
            f"{engine.name}.load() exceeded {timeout_s:.1f}s deadline"
        )
    if holder["exc"] is not None:
        raise holder["exc"]


def _attempt_load_with_hf_fallback(
    engine_name: str,
    model: str,
    model_root: str,
    language: str,
    device: str,
    compute_type: str,
    timeout_s: float,
) -> Tuple[STTEngine, str]:
    """Load an engine, preferring ``model_root`` then falling back to HF cache.

    Returns ``(loaded_engine, degradation_reason)``.  Empty reason
    means the preferred (model_root) path loaded cleanly; non-empty
    means we fell off to the HF cache on internal disk and the
    morning report should flag the dock as acting up.

    Raises ``STTEngineLoadError`` if both the model_root attempt and
    the HF cache attempt fail.
    """
    degradation: str = ""
    local_path: Optional[str] = _resolve_model_path(
        model_root, engine_name, model,
    )

    # Attempt 1: the configured model_root path (usually Mini-Dock).
    if local_path is not None:
        engine: STTEngine = _build_engine_at_path(
            engine_name, model, local_path, language, device, compute_type,
        )
        try:
            _load_with_deadline(engine, timeout_s)
            return engine, ""
        except LoadTimeout:
            degradation = (
                f"{engine_name} load from {local_path} exceeded "
                f"{timeout_s:.0f}s deadline — retrying with HF cache "
                "(is /Volumes/Mini-Dock stuck on an accessory prompt?)"
            )
            logger.error("%s", degradation)
        except STTEngineLoadError as exc:
            degradation = (
                f"{engine_name} load from {local_path} failed "
                f"({exc}) — retrying with HF cache"
            )
            logger.error("%s", degradation)

    # Attempt 2: HF cache on internal disk.
    engine = _build_engine_at_path(
        engine_name, model, None, language, device, compute_type,
    )
    try:
        _load_with_deadline(engine, timeout_s)
        return engine, degradation
    except LoadTimeout as exc:
        raise STTEngineLoadError(
            f"{engine_name} load from HF cache also exceeded "
            f"{timeout_s:.0f}s deadline; preceding error: {degradation}"
        ) from exc
    except STTEngineLoadError as exc:
        if degradation:
            raise STTEngineLoadError(
                f"{degradation}; HF cache load also failed: {exc}"
            ) from exc
        raise


class SpeechToText:
    """Primary/fallback STT facade with load-deadline + HF-cache retry.

    Config schema (coordinator_config.json, ``stt`` block)::

        {
          "engine":                  "mlx-whisper",
          "fallback_engine":         "faster-whisper",
          "model":                   "large-v3-turbo",
          "model_root":              "/Volumes/Mini-Dock/glowup/models",
          "language":                "en",
          "device":                  "cpu",      # faster-whisper only
          "compute_type":            "int8",     # faster-whisper only
          "primary_load_timeout_s":  60,         # default 60
          "fallback_load_timeout_s": 60          # default 60
        }

    Legacy keys ``model_size`` (alias for ``model``) are accepted for
    one migration cycle so an un-migrated coordinator_config.json does
    not hard-fail.

    Both engines are loaded at construction time so a runtime fallback
    transition is latency-free.  Each load runs under its own deadline
    so a stalled ``model_root`` does not hang the coordinator — the
    facade abandons the stalled attempt and retries against the
    engine's HF cache on internal disk.  Any such retry is recorded
    in ``fallback_reason`` so the morning report renders the Daedalus
    row in red.

    If the primary engine ultimately cannot load, the fallback becomes
    the active engine.  If both fail, ``STTEngineLoadError`` is raised
    and the coordinator refuses to start.
    """

    def __init__(
        self,
        config: Optional[dict[str, Any]] = None,
        *,
        model_size: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
    ) -> None:
        cfg: dict[str, Any] = dict(config or {})

        # Legacy kwarg path — the old SpeechToText signature is still
        # reachable via kwargs for any caller that has not migrated.
        if model_size is not None:
            cfg.setdefault("model", model_size)
        if device is not None:
            cfg.setdefault("device", device)
        if compute_type is not None:
            cfg.setdefault("compute_type", compute_type)

        primary_name: str = cfg.get("engine", "mlx-whisper")
        fallback_name: str = cfg.get("fallback_engine", "faster-whisper")
        model: str = cfg.get("model", cfg.get("model_size", "large-v3-turbo"))
        model_root: str = cfg.get("model_root", "")
        language: str = cfg.get("language", "en")
        fw_device: str = cfg.get("device", "cpu")
        fw_compute_type: str = cfg.get("compute_type", "int8")
        primary_timeout: float = float(cfg.get(
            "primary_load_timeout_s", DEFAULT_PRIMARY_LOAD_TIMEOUT_S,
        ))
        fallback_timeout: float = float(cfg.get(
            "fallback_load_timeout_s", DEFAULT_FALLBACK_LOAD_TIMEOUT_S,
        ))

        # Fallback collapses to no-op if it matches the primary.
        if fallback_name == primary_name:
            fallback_name = ""

        # Load primary (with deadline + HF-cache retry).
        primary: Optional[STTEngine] = None
        primary_degradation: str = ""
        primary_error: Optional[Exception] = None
        try:
            primary, primary_degradation = _attempt_load_with_hf_fallback(
                primary_name, model, model_root, language,
                fw_device, fw_compute_type, primary_timeout,
            )
            logger.info(
                "STT primary engine loaded: %s (model=%s%s)",
                primary.name, model,
                " — degraded" if primary_degradation else "",
            )
        except STTEngineLoadError as exc:
            primary_error = exc
            logger.error(
                "STT primary engine %s failed to load — "
                "will fall back: %s", primary_name, exc,
            )

        # Load fallback (pre-warm).
        fallback: Optional[STTEngine] = None
        fallback_degradation: str = ""
        fallback_error: Optional[Exception] = None
        if fallback_name:
            try:
                fallback, fallback_degradation = _attempt_load_with_hf_fallback(
                    fallback_name, model, model_root, language,
                    fw_device, fw_compute_type, fallback_timeout,
                )
                logger.info(
                    "STT fallback engine loaded: %s (pre-warmed%s)",
                    fallback.name,
                    " — degraded" if fallback_degradation else "",
                )
            except STTEngineLoadError as exc:
                fallback_error = exc
                logger.error(
                    "STT fallback engine %s failed to load: %s",
                    fallback_name, exc,
                )

        # Decide active engine + compose the state-file reason.
        reasons: list[str] = []
        if primary is not None:
            active: STTEngine = primary
            if primary_degradation:
                reasons.append(primary_degradation)
            if fallback_error is not None:
                reasons.append(
                    f"fallback {fallback_name} also failed: {fallback_error}"
                )
            elif fallback_degradation:
                reasons.append(
                    f"fallback pre-warm degraded: {fallback_degradation}"
                )
        elif fallback is not None:
            active = fallback
            reasons.append(
                f"primary {primary_name} failed: {primary_error}"
            )
            if fallback_degradation:
                reasons.append(
                    f"fallback also degraded: {fallback_degradation}"
                )
        else:
            # No engine available.
            reasons.append(
                f"primary {primary_name} failed: {primary_error}"
            )
            if fallback_error is not None:
                reasons.append(
                    f"fallback {fallback_name} failed: {fallback_error}"
                )
            elif not fallback_name:
                reasons.append("no fallback engine configured")
            write_state(
                engine="none",
                fallback_reason=" | ".join(reasons),
                primary_engine=primary_name,
            )
            raise STTEngineLoadError(
                f"No STT engine could be loaded. {' | '.join(reasons)}"
            )

        fallback_reason: str = " | ".join(reasons)

        self._primary: Optional[STTEngine] = primary
        self._fallback: Optional[STTEngine] = fallback
        self._active: STTEngine = active
        self._primary_name: str = primary_name
        self._fallback_reason: str = fallback_reason

        write_state(
            engine=active.name,
            fallback_reason=fallback_reason,
            primary_engine=primary_name,
        )

    @property
    def engine_name(self) -> str:
        return self._active.name

    @property
    def primary_name(self) -> str:
        return self._primary_name

    @property
    def fallback_reason(self) -> str:
        return self._fallback_reason

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        return self._active.transcribe(pcm, sample_rate)


class MockSpeechToText:
    """Mock STT wrapper used when ``mock_stt: true`` is set in config.

    The daemon imports this name explicitly; keeping it here as a thin
    wrapper over ``MockEngine`` preserves the import surface that the
    pre-refactor ``stt.py`` exposed.
    """

    def __init__(self, transcript: Optional[str] = None) -> None:
        self._engine: MockEngine = MockEngine(transcript=transcript)
        self._engine.load()
        write_state(
            engine=self._engine.name,
            fallback_reason="",
            primary_engine=self._engine.name,
        )

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        return self._engine.transcribe(pcm, sample_rate)
