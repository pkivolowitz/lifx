"""Media source and signal handlers.

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
from media import MediaManager


class MediaHandlerMixin:
    """Media source and signal handlers."""

    def _handle_get_media_sources(self) -> None:
        """GET /api/media/sources — list media sources with status.

        Returns source names, types, and alive status.  Never exposes
        RTSP URLs or credentials.
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(200, {"sources": []})
            return
        self._send_json(200, {"sources": mm.get_status()})


    def _handle_get_media_signals(self) -> None:
        """GET /api/media/signals — list available signal names.

        Returns signal metadata for the iOS signal picker UI.
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(200, {"signals": []})
            return
        self._send_json(200, {"signals": mm.bus.list_signals()})


    def _handle_get_media_stream(self, source_name: str) -> None:
        """GET /api/media/stream/{source_name} — raw PCM audio stream.

        Streams raw 16-bit signed little-endian mono PCM at the source's
        sample rate.  Designed for piping to ffplay::

            ffplay -f s16le -ar 44100 -ac 1 -nodisp http://server:8420/api/media/stream/name

        The connection stays open as long as the source is alive.  No
        authentication required — audio streams are not sensitive.
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(404, {"error": "No media manager"})
            return

        with mm._lock:
            source = mm._sources.get(source_name)
        if source is None or not source.is_alive():
            self._send_json(404, {
                "error": f"Source '{source_name}' not found or not running"
            })
            return

        # Send chunked raw PCM.
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Sample-Rate", str(source.sample_rate))
        self.send_header("X-Channels", str(source.channels))
        self.send_header("X-Format", "s16le")
        self.end_headers()

        # Thread-safe queue bridges the source callback to this thread.
        import queue as _queue
        audio_q: _queue.Queue[Optional[bytes]] = _queue.Queue(maxsize=512)

        def _on_chunk(chunk: bytes) -> None:
            try:
                audio_q.put_nowait(chunk)
            except _queue.Full:
                pass  # Drop frames rather than blocking the source.

        # Aggregate small PCM chunks into larger HTTP writes to reduce
        # per-chunk overhead.  At 44100 Hz mono 16-bit, 3200 bytes is
        # only ~36ms.  We batch up to ~200ms before flushing.
        BATCH_BYTES: int = 17640  # ~200ms at 44100 Hz mono 16-bit

        source.add_extractor(_on_chunk)
        try:
            buf: bytearray = bytearray()
            while source.is_alive():
                try:
                    chunk: Optional[bytes] = audio_q.get(timeout=AUDIO_QUEUE_TIMEOUT_SECONDS)
                except _queue.Empty:
                    # Flush whatever we have on timeout.
                    if buf:
                        self.wfile.write(f"{len(buf):x}\r\n".encode())
                        self.wfile.write(buf)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                        buf = bytearray()
                    continue
                if chunk is None:
                    break
                buf.extend(chunk)
                if len(buf) >= BATCH_BYTES:
                    self.wfile.write(f"{len(buf):x}\r\n".encode())
                    self.wfile.write(buf)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    buf = bytearray()
            # Flush remainder.
            if buf:
                self.wfile.write(f"{len(buf):x}\r\n".encode())
                self.wfile.write(buf)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            # Chunked terminator.
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client disconnected.
        finally:
            source.remove_extractor(_on_chunk)

    # -- Calibration handlers -------------------------------------------------


    def _handle_post_media_source_start(self, name: str) -> None:
        """POST /api/media/sources/{name}/start — manually start a source.

        Starts the named media source (e.g. an ffmpeg audio capture
        pipeline).  Returns 503 if the media pipeline is not configured,
        404 if the source name is unknown.

        Args:
            name: Media source name (URL-decoded by dispatch).
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(503, {
                "error": "Media pipeline not configured",
            })
            return
        if mm.start_source(name):
            logging.info("API: started media source '%s'", name)
            self._send_json(200, {"source": name, "started": True})
        else:
            self._send_json(404, {
                "error": f"Unknown media source: {name}",
            })


    def _handle_post_media_source_stop(self, name: str) -> None:
        """POST /api/media/sources/{name}/stop — manually stop a source.

        Stops the named media source.  Returns 503 if the media
        pipeline is not configured, 404 if the source name is unknown.

        Args:
            name: Media source name (URL-decoded by dispatch).
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(503, {
                "error": "Media pipeline not configured",
            })
            return
        try:
            mm.stop_source(name)
            logging.info("API: stopped media source '%s'", name)
            self._send_json(200, {"source": name, "stopped": True})
        except KeyError:
            self._send_json(404, {
                "error": f"Unknown media source: {name}",
            })


    def _handle_post_signal_ingest(self) -> None:
        """POST /api/media/signals/ingest — write signals from an external source.

        Accepts a JSON body with a ``source`` name and a ``signals`` dict
        mapping signal suffixes to values (scalar float or float array).
        Each signal is written to the bus as ``{source}:audio:{name}``.

        This endpoint enables any device (iPhone, ESP32, browser) to act
        as a media source by posting computed signal values directly to
        the signal bus, bypassing the ffmpeg/extractor pipeline.

        Request body::

            {
                "source": "iphone",
                "signals": {
                    "bands": [0.1, 0.3, 0.8, 0.2, 0.0, 0.1, 0.5, 0.9],
                    "rms": 0.42,
                    "beat": 1.0,
                    "bass": 0.2,
                    "mid": 0.5,
                    "treble": 0.7,
                    "energy": 0.45,
                    "centroid": 0.6
                }
            }
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(503, {
                "error": "Media pipeline not configured",
            })
            return

        body: dict = self._read_json_body()
        if body is None:
            return

        source: str = body.get("source", "")
        if not source:
            self._send_json(400, {"error": "'source' is required"})
            return

        signals: dict = body.get("signals", {})
        if not isinstance(signals, dict):
            self._send_json(400, {"error": "'signals' must be an object"})
            return

        bus = mm.bus
        written: int = 0
        for name, value in signals.items():
            signal_name: str = f"{source}:audio:{name}"
            if isinstance(value, (int, float)):
                bus.write(signal_name, float(value))
                written += 1
            elif isinstance(value, list):
                bus.write(signal_name, [float(v) for v in value])
                written += 1

        self._send_json(200, {"written": written})

    # -- Diagnostics endpoints ----------------------------------------------


