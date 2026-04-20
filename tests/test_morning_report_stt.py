"""Tests for morning_report's STT-state collector and rendering.

Verifies that:
    - collect_stt_state parses the JSON state file from Daedalus
      correctly, classifies degraded vs healthy, and handles missing
      files / malformed JSON as reachable=False with an error message
      rather than raising.
    - The rendered HTML flags degraded state with the "fail" CSS
      class (red) and lists the fallback reason so the operator can
      see at a glance what broke.

Runs entirely in-process with _ssh mocked — no live SSH to Daedalus.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import json
import unittest
from datetime import datetime
from unittest import mock

# The morning_report module lives in services/ and is not a package.
# Load it by path so this test does not depend on the PYTHONPATH
# tweaks required on the hub's deployment.  The module is not deployed
# to Daedalus (the coordinator host) — it only lives on the hub.  Skip
# the whole file when the source is not present so the test suite
# stays clean on every deployed host.
import importlib.util
import os

_MR_PATH: str = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "services", "morning_report.py",
))

if not os.path.isfile(_MR_PATH):
    import pytest
    pytest.skip(
        f"services/morning_report.py not present at {_MR_PATH} "
        "(expected when running tests off the hub)",
        allow_module_level=True,
    )

_spec = importlib.util.spec_from_file_location(
    "morning_report_under_test", _MR_PATH,
)
assert _spec is not None and _spec.loader is not None
morning_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(morning_report)


class TestCollectSttState(unittest.TestCase):
    def test_healthy_primary(self) -> None:
        payload = {
            "engine": "mlx-whisper",
            "primary_engine": "mlx-whisper",
            "fallback_reason": "",
            "since": "2026-04-20T10:00:00+00:00",
        }
        with mock.patch.object(
            morning_report, "_ssh",
            return_value=(True, json.dumps(payload)),
        ):
            out = morning_report.collect_stt_state("10.0.0.191", "user")
        self.assertTrue(out["reachable"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["engine"], "mlx-whisper")

    def test_degraded_fallback_active(self) -> None:
        payload = {
            "engine": "faster-whisper",
            "primary_engine": "mlx-whisper",
            "fallback_reason": "primary mlx-whisper failed: out of memory",
            "since": "2026-04-20T10:00:00+00:00",
        }
        with mock.patch.object(
            morning_report, "_ssh",
            return_value=(True, json.dumps(payload)),
        ):
            out = morning_report.collect_stt_state("10.0.0.191", "user")
        self.assertTrue(out["reachable"])
        self.assertTrue(out["degraded"])
        self.assertIn("out of memory", out["fallback_reason"])

    def test_degraded_reason_without_engine_swap(self) -> None:
        """A warning-only degradation still flips degraded to True."""
        payload = {
            "engine": "mlx-whisper",
            "primary_engine": "mlx-whisper",
            "fallback_reason": "transcription errored 3 times in a row",
            "since": "2026-04-20T10:00:00+00:00",
        }
        with mock.patch.object(
            morning_report, "_ssh",
            return_value=(True, json.dumps(payload)),
        ):
            out = morning_report.collect_stt_state("10.0.0.191", "user")
        self.assertTrue(out["degraded"])

    def test_missing_file_is_warn_not_fail(self) -> None:
        with mock.patch.object(
            morning_report, "_ssh",
            return_value=(True, ""),
        ):
            out = morning_report.collect_stt_state("10.0.0.191", "user")
        self.assertFalse(out["reachable"])
        self.assertIn("state file", out["error"])

    def test_ssh_failure_is_warn_not_fail(self) -> None:
        with mock.patch.object(
            morning_report, "_ssh",
            return_value=(False, "connection refused"),
        ):
            out = morning_report.collect_stt_state("10.0.0.191", "user")
        self.assertFalse(out["reachable"])

    def test_malformed_json_is_handled(self) -> None:
        with mock.patch.object(
            morning_report, "_ssh",
            return_value=(True, "this is not json"),
        ):
            out = morning_report.collect_stt_state("10.0.0.191", "user")
        self.assertFalse(out["reachable"])
        self.assertIn("not valid JSON", out["error"])


class TestRenderDegradedInRed(unittest.TestCase):
    """Render-level smoke test — degraded state must produce the
    'fail' CSS class so the email shows red."""

    def _render_with_stt(self, stt_state: dict) -> str:
        # Minimal dummy data for the other sections.  API reachable
        # with ready status so unrelated sections do not emit red
        # that would pollute the assertions.
        hosts = []
        api_status = {"reachable": True, "data": {"status": "ready"}}
        batteries = []
        mqtt_rates = {}
        git = {"reachable": True, "log": "", "branches": ""}
        tests = {"reachable": True, "summary": ""}
        now = datetime(2026, 4, 20, 6, 0, 0)
        return morning_report.render_html(
            hosts, api_status, batteries, mqtt_rates,
            git, tests, stt_state, now,
        )

    @staticmethod
    def _extract_voice_section(html: str) -> str:
        """Return just the Voice/STT section, so assertions do not
        accidentally pick up CSS classes in unrelated sections."""
        marker: str = 'Voice &middot; STT</h2>'
        start: int = html.find(marker)
        assert start >= 0, "Voice section missing from rendered HTML"
        end: int = html.find('<div class="section"', start + len(marker))
        return html[start:end] if end >= 0 else html[start:]

    def test_degraded_renders_red(self) -> None:
        html = self._render_with_stt({
            "reachable": True, "degraded": True,
            "engine": "faster-whisper",
            "primary_engine": "mlx-whisper",
            "fallback_reason": "mlx-whisper out of memory",
            "since": "2026-04-20T10:00:00+00:00",
        })
        section = self._extract_voice_section(html)
        self.assertIn('class="fail"', section)
        self.assertIn("DEGRADED", section)
        self.assertIn("mlx-whisper out of memory", section)

    def test_healthy_does_not_render_red(self) -> None:
        html = self._render_with_stt({
            "reachable": True, "degraded": False,
            "engine": "mlx-whisper",
            "primary_engine": "mlx-whisper",
            "fallback_reason": "",
            "since": "2026-04-20T10:00:00+00:00",
        })
        section = self._extract_voice_section(html)
        self.assertNotIn('class="fail"', section)
        self.assertIn("primary", section)

    def test_unavailable_state_file_renders_warn(self) -> None:
        html = self._render_with_stt({
            "reachable": False,
            "error": "state file missing at ~/.glowup/stt_state.json",
        })
        section = self._extract_voice_section(html)
        self.assertNotIn('class="fail"', section)
        self.assertIn('class="warn"', section)


if __name__ == "__main__":
    unittest.main()
