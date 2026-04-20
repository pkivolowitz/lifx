"""Speech-to-text facade for the voice coordinator.

Selects a primary STT engine from config, loads a fallback engine
alongside it (pre-warmed so transitions are latency-free), and writes
``~/.glowup/stt_state.json`` so the morning report can detect and
render degraded state.

Engine implementations live in ``voice.coordinator.stt_engines``.
See ``docs/36-stt-stack.md`` for stack design and configuration.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "3.0"

import logging
import os
from typing import Any, Optional

from voice.coordinator.stt_engines import (
    FasterWhisperEngine,
    MLXWhisperEngine,
    MockEngine,
    STTEngine,
    STTEngineLoadError,
    write_state,
)

logger: logging.Logger = logging.getLogger("glowup.voice.stt")


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


def _build_engine(
    engine_name: str,
    model: str,
    model_root: str,
    language: str,
    device: str,
    compute_type: str,
) -> STTEngine:
    """Instantiate an un-loaded engine by name."""
    factory = _ENGINE_FACTORIES.get(engine_name)
    if factory is None:
        raise STTEngineLoadError(
            f"Unknown STT engine '{engine_name}' — "
            f"valid: {sorted(_ENGINE_FACTORIES)}"
        )
    path: Optional[str] = _resolve_model_path(model_root, engine_name, model)
    return factory(model, path, language, device, compute_type)


class SpeechToText:
    """Primary/fallback STT facade.

    Config schema (coordinator_config.json, ``stt`` block)::

        {
          "engine":          "mlx-whisper",
          "fallback_engine": "faster-whisper",
          "model":           "large-v3-turbo",
          "model_root":      "/Volumes/Mini-Dock/glowup/models",
          "language":        "en",
          "device":          "cpu",          # faster-whisper only
          "compute_type":    "int8"          # faster-whisper only
        }

    Legacy keys ``model_size`` (alias for ``model``) are accepted for
    one migration cycle so an un-migrated coordinator_config.json does
    not hard-fail.

    Both engines are loaded at construction time so a runtime fallback
    transition is latency-free.  If the primary engine fails to load,
    the fallback becomes the active engine and ``write_state`` records
    the degradation.  If both fail, ``STTEngineLoadError`` is raised
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

        # Fallback collapses to no-op if it matches the primary.
        if fallback_name == primary_name:
            fallback_name = ""

        primary: STTEngine = _build_engine(
            primary_name, model, model_root, language, fw_device, fw_compute_type,
        )
        fallback: Optional[STTEngine] = None
        if fallback_name:
            fallback = _build_engine(
                fallback_name, model, model_root, language,
                fw_device, fw_compute_type,
            )

        # Load primary.  Fallback reason is kept so we can render it in
        # the state file and in the morning report email.
        fallback_reason: str = ""
        primary_loaded: bool = False
        try:
            primary.load()
            primary_loaded = True
            logger.info(
                "STT primary engine loaded: %s (model=%s)",
                primary.name, model,
            )
        except STTEngineLoadError as exc:
            fallback_reason = f"primary {primary.name} failed: {exc}"
            logger.error(
                "STT primary engine %s failed to load — will fall back: %s",
                primary.name, exc,
            )

        # Load fallback.  Pre-warm path: load it even when the primary
        # loaded successfully so a runtime swap has no model-load cost.
        fallback_loaded: bool = False
        if fallback is not None:
            try:
                fallback.load()
                fallback_loaded = True
                logger.info(
                    "STT fallback engine loaded: %s (pre-warmed)",
                    fallback.name,
                )
            except STTEngineLoadError as exc:
                logger.error(
                    "STT fallback engine %s failed to load: %s",
                    fallback.name, exc,
                )
                if not primary_loaded:
                    # Both engines down.  Refuse to start so the
                    # coordinator's launchd throttle holds it down
                    # for operator intervention.
                    write_state(
                        engine="none",
                        fallback_reason=(
                            f"{fallback_reason} | fallback {fallback.name} "
                            f"also failed: {exc}"
                        ),
                        primary_engine=primary.name,
                    )
                    raise STTEngineLoadError(
                        f"No STT engine could be loaded. Primary "
                        f"{primary.name}: {fallback_reason}. "
                        f"Fallback {fallback.name}: {exc}"
                    ) from exc

        if primary_loaded:
            active: STTEngine = primary
        elif fallback_loaded and fallback is not None:
            active = fallback
        else:
            # Primary failed, no fallback configured.
            write_state(
                engine="none",
                fallback_reason=fallback_reason
                or f"primary {primary.name} failed and no fallback configured",
                primary_engine=primary.name,
            )
            raise STTEngineLoadError(
                f"No STT engine available — primary {primary.name} "
                "failed and no fallback is configured."
            )

        self._primary: STTEngine = primary
        self._fallback: Optional[STTEngine] = fallback if fallback_loaded else None
        self._active: STTEngine = active
        self._primary_name: str = primary.name
        self._fallback_reason: str = fallback_reason

        write_state(
            engine=active.name,
            fallback_reason=fallback_reason,
            primary_engine=primary.name,
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
