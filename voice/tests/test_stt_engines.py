"""Tests for the pluggable STT engine stack.

Covers:
    - write_state atomicity + schema
    - MockEngine behaviour (deterministic and prompt modes)
    - Availability gates on FasterWhisperEngine / MLXWhisperEngine
    - SpeechToText facade: unknown engine, primary load failure
      falls back cleanly, both engines failing raises,
      same-name fallback collapses.

Does not require faster-whisper or mlx-whisper to be installed.
Real-model smoke tests live in a separate integration suite that
only runs on Daedalus (with the weights and hardware present).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from voice.coordinator.stt_engines import (
    FasterWhisperEngine,
    MLXWhisperEngine,
    MockEngine,
    STTEngineLoadError,
)
from voice.coordinator.stt_engines import base as base_module


class _StateFileFixture:
    """Redirect base_module.STT_STATE_FILE to a temp dir for the test."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = base_module.STT_STATE_DIR
        self._orig_file = base_module.STT_STATE_FILE
        base_module.STT_STATE_DIR = Path(self._tmp.name)
        base_module.STT_STATE_FILE = Path(self._tmp.name) / "stt_state.json"

    def close(self) -> None:
        base_module.STT_STATE_DIR = self._orig_dir
        base_module.STT_STATE_FILE = self._orig_file
        self._tmp.cleanup()


class TestWriteState(unittest.TestCase):
    """write_state atomically writes a well-formed JSON state file."""

    def setUp(self) -> None:
        self.fx = _StateFileFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_writes_expected_schema(self) -> None:
        base_module.write_state(
            engine="mlx-whisper",
            fallback_reason="",
            primary_engine="mlx-whisper",
        )
        data = json.loads(base_module.STT_STATE_FILE.read_text())
        self.assertEqual(data["engine"], "mlx-whisper")
        self.assertEqual(data["primary_engine"], "mlx-whisper")
        self.assertEqual(data["fallback_reason"], "")
        self.assertIn("since", data)

    def test_primary_defaults_to_engine_when_omitted(self) -> None:
        base_module.write_state(engine="mock")
        data = json.loads(base_module.STT_STATE_FILE.read_text())
        self.assertEqual(data["primary_engine"], "mock")

    def test_no_leftover_tmp_file(self) -> None:
        base_module.write_state(engine="mock")
        tmps = list(base_module.STT_STATE_DIR.glob("*.tmp"))
        self.assertEqual(tmps, [])


class TestMockEngine(unittest.TestCase):
    def test_deterministic_transcript(self) -> None:
        e = MockEngine(transcript="hello world")
        e.load()
        self.assertEqual(e.transcribe(b"\x00" * 100), "hello world")

    def test_is_available_always_true(self) -> None:
        self.assertTrue(MockEngine.is_available())

    def test_name(self) -> None:
        self.assertEqual(MockEngine.name, "mock")


class TestEngineAvailability(unittest.TestCase):
    """is_available() must not raise on any host."""

    def test_mlx_returns_false_on_non_darwin(self) -> None:
        # The check is platform-gated; it should return a bool
        # without raising regardless of host.  Concrete False on
        # non-darwin/arm64 is asserted via the platform check below.
        result = MLXWhisperEngine.is_available()
        self.assertIsInstance(result, bool)

    def test_mlx_gated_on_platform(self) -> None:
        with mock.patch("sys.platform", "linux"):
            self.assertFalse(MLXWhisperEngine.is_available())

    def test_mlx_gated_on_machine(self) -> None:
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("platform.machine", return_value="x86_64"):
            self.assertFalse(MLXWhisperEngine.is_available())

    def test_faster_whisper_returns_bool(self) -> None:
        self.assertIsInstance(FasterWhisperEngine.is_available(), bool)


class TestFacadeSelection(unittest.TestCase):
    """SpeechToText selection, fallback, and failure behaviour."""

    def setUp(self) -> None:
        self.fx = _StateFileFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def _make_engine(
        self,
        name: str,
        load_raises: bool = False,
        transcribe_returns: str = "ok",
    ):
        """Build a stand-in engine object compliant with the protocol."""
        engine = mock.MagicMock()
        engine.name = name
        if load_raises:
            engine.load.side_effect = STTEngineLoadError(f"{name} down")
        else:
            engine.load.return_value = None
        engine.transcribe.return_value = transcribe_returns
        return engine

    def test_unknown_engine_name_raises(self) -> None:
        from voice.coordinator.stt import SpeechToText
        with self.assertRaises(STTEngineLoadError) as ctx:
            SpeechToText({"engine": "nope", "fallback_engine": "also-nope"})
        self.assertIn("Unknown STT engine", str(ctx.exception))

    def test_primary_ok_both_engines_preloaded(self) -> None:
        from voice.coordinator import stt as stt_mod
        primary = self._make_engine("mlx-whisper")
        fallback = self._make_engine("faster-whisper")
        with mock.patch.object(
            stt_mod, "_build_engine",
            side_effect=[primary, fallback],
        ):
            s = stt_mod.SpeechToText({
                "engine": "mlx-whisper",
                "fallback_engine": "faster-whisper",
            })
        primary.load.assert_called_once()
        fallback.load.assert_called_once()
        self.assertEqual(s.engine_name, "mlx-whisper")
        self.assertEqual(s.primary_name, "mlx-whisper")
        self.assertEqual(s.fallback_reason, "")
        state = json.loads(base_module.STT_STATE_FILE.read_text())
        self.assertEqual(state["engine"], "mlx-whisper")
        self.assertEqual(state["fallback_reason"], "")

    def test_primary_fails_fallback_becomes_active(self) -> None:
        from voice.coordinator import stt as stt_mod
        primary = self._make_engine("mlx-whisper", load_raises=True)
        fallback = self._make_engine("faster-whisper",
                                     transcribe_returns="fell back")
        with mock.patch.object(
            stt_mod, "_build_engine",
            side_effect=[primary, fallback],
        ):
            s = stt_mod.SpeechToText({
                "engine": "mlx-whisper",
                "fallback_engine": "faster-whisper",
            })
        self.assertEqual(s.engine_name, "faster-whisper")
        self.assertEqual(s.primary_name, "mlx-whisper")
        self.assertIn("mlx-whisper", s.fallback_reason)
        self.assertEqual(s.transcribe(b"x"), "fell back")
        state = json.loads(base_module.STT_STATE_FILE.read_text())
        self.assertEqual(state["engine"], "faster-whisper")
        self.assertEqual(state["primary_engine"], "mlx-whisper")
        self.assertTrue(state["fallback_reason"])

    def test_both_engines_fail_raises(self) -> None:
        from voice.coordinator import stt as stt_mod
        primary = self._make_engine("mlx-whisper", load_raises=True)
        fallback = self._make_engine("faster-whisper", load_raises=True)
        with mock.patch.object(
            stt_mod, "_build_engine",
            side_effect=[primary, fallback],
        ):
            with self.assertRaises(STTEngineLoadError):
                stt_mod.SpeechToText({
                    "engine": "mlx-whisper",
                    "fallback_engine": "faster-whisper",
                })
        state = json.loads(base_module.STT_STATE_FILE.read_text())
        self.assertEqual(state["engine"], "none")
        self.assertIn("mlx-whisper", state["fallback_reason"])
        self.assertIn("faster-whisper", state["fallback_reason"])

    def test_same_name_fallback_collapses(self) -> None:
        from voice.coordinator import stt as stt_mod
        primary = self._make_engine("mlx-whisper")
        with mock.patch.object(
            stt_mod, "_build_engine",
            side_effect=[primary],
        ) as build:
            stt_mod.SpeechToText({
                "engine": "mlx-whisper",
                "fallback_engine": "mlx-whisper",
            })
        self.assertEqual(build.call_count, 1)
        primary.load.assert_called_once()

    def test_legacy_model_size_kwarg_accepted(self) -> None:
        from voice.coordinator import stt as stt_mod
        primary = self._make_engine("faster-whisper")
        with mock.patch.object(
            stt_mod, "_build_engine",
            side_effect=[primary],
        ) as build:
            stt_mod.SpeechToText(
                {"engine": "faster-whisper", "fallback_engine": "faster-whisper"},
                model_size="base.en",
                device="cpu",
                compute_type="int8",
            )
        # Verify the legacy model_size flowed into the new 'model' arg.
        call_kwargs = build.call_args
        # Positional signature is (engine_name, model, model_root, ...)
        self.assertEqual(call_kwargs.args[0], "faster-whisper")
        self.assertEqual(call_kwargs.args[1], "base.en")


class TestFacadeModelRootResolution(unittest.TestCase):
    def test_resolve_model_path_returns_none_when_absent(self) -> None:
        from voice.coordinator.stt import _resolve_model_path
        with tempfile.TemporaryDirectory() as root:
            # Nothing under <root>/mlx-whisper/large-v3-turbo/
            path = _resolve_model_path(root, "mlx-whisper", "large-v3-turbo")
            self.assertIsNone(path)

    def test_resolve_model_path_returns_path_when_present(self) -> None:
        from voice.coordinator.stt import _resolve_model_path
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "mlx-whisper", "large-v3-turbo")
            os.makedirs(target)
            path = _resolve_model_path(root, "mlx-whisper", "large-v3-turbo")
            self.assertEqual(path, target)


if __name__ == "__main__":
    unittest.main()
