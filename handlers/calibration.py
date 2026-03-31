"""Device calibration protocol handlers.

Mixin class for GlowUpRequestHandler.  Extracted from server.py.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import math
import os
import socket
import struct
import threading
import time as time_mod
from datetime import datetime, time, timedelta
from typing import Any, Optional
from urllib.parse import unquote

from server_constants import *  # All constants available


class CalibrationHandlerMixin:
    """Device calibration protocol handlers."""

    def _handle_get_calibrate_time_sync(self) -> None:
        """GET /api/calibrate/time_sync — return server monotonic time.

        Used by the CLI to estimate clock offset between client and
        server via Cristian's algorithm.  No auth required.
        """
        self._send_json(200, {"server_time": time_mod.monotonic()})


    def _handle_post_calibrate_start(self, device_id: str) -> None:
        """POST /api/calibrate/start/{device_id} — standalone sonar calibration.

        Opens a temporary TCP socket, sends silence + calibration pulses,
        records emission timestamps, and returns them.  Nothing else is
        running — no music source, no effect, no competition.  The CLI
        connects to the TCP socket, detects pulses, computes the delay.

        This is the "Calibrating for optimal performance" step that
        runs before music starts.
        """
        import socket as _socket
        from media.calibration import PulseGenerator

        CALIBRATION_PORT: int = 8421
        SAMPLE_RATE: int = 44100

        gen: PulseGenerator = PulseGenerator(sample_rate=SAMPLE_RATE)
        sequence = gen.generate_sequence()

        # Open a temporary TCP socket for the calibration stream.
        srv_sock: _socket.socket = _socket.socket(
            _socket.AF_INET, _socket.SOCK_STREAM
        )
        srv_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            srv_sock.bind(("0.0.0.0", CALIBRATION_PORT))
        except OSError:
            # Port in use — try next.
            CALIBRATION_PORT += 1
            srv_sock.bind(("0.0.0.0", CALIBRATION_PORT))
        srv_sock.listen(1)
        srv_sock.settimeout(CALIBRATION_SOCKET_TIMEOUT_SECONDS)

        logging.info(
            "Calibration: listening on tcp port %d", CALIBRATION_PORT,
        )

        # Return the port so the CLI knows where to connect.
        # The CLI will connect, then we send the pulses.
        self._send_json(200, {"port": CALIBRATION_PORT, "status": "ready"})
        self.wfile.flush()

        # Wait for the CLI to connect.
        try:
            client, addr = srv_sock.accept()
        except _socket.timeout:
            srv_sock.close()
            logging.warning("Calibration: no client connected (timeout)")
            return

        client.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        logging.info("Calibration: client connected from %s", addr)

        # Send the calibration sequence.
        emit_times: list[float] = []
        try:
            for tag, chunk in sequence:
                if tag.startswith("pulse:"):
                    t_emit: float = time_mod.monotonic()
                    client.sendall(chunk)
                    emit_times.append(t_emit)
                else:
                    # Silence — send as bulk.
                    client.sendall(chunk)
                # Brief real-time pause so pulses are spaced in time.
                time_mod.sleep(CALIBRATION_PULSE_DELAY_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logging.warning("Calibration: client disconnected: %s", exc)

        # Send the emit timestamps as a JSON line at the end of the
        # TCP stream so the CLI can read them after pulse detection.
        try:
            marker: bytes = b"\n__EMIT_TIMES__:"
            payload: bytes = json.dumps(emit_times).encode()
            client.sendall(marker + payload + b"\n")
        except Exception:
            pass

        client.close()
        srv_sock.close()
        logging.info(
            "Calibration: done, %d pulses emitted", len(emit_times),
        )


    def _handle_post_calibrate_result(self, device_id: str) -> None:
        """POST /api/calibrate/result/{device_id} — apply measured delay.

        Request body::

            {"delay_seconds": 0.423}

        Sets the audio synchronization delay on the device's engine
        so light frames are delayed to match the audio stream.  Also
        stores the value for reuse on subsequent plays.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        delay: Any = body.get("delay_seconds")
        if not isinstance(delay, (int, float)) or delay < 0:
            self._send_json(400, {
                "error": "'delay_seconds' must be a non-negative number"
            })
            return

        # Store for reuse.
        self._calibrated_delay = float(delay)

        dm: DeviceManager = self.device_manager
        ctrl: Optional[Controller] = dm.get_controller(device_id)
        if ctrl is not None:
            ctrl.set_audio_delay(float(delay))

        logging.info(
            "Calibration: audio delay set to %.3fs", delay,
        )
        self._send_json(200, {
            "delay_seconds": delay,
        })

    # -- Fleet handler ---------------------------------------------------------


