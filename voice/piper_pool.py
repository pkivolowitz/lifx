"""Pre-warmed pool of single-use piper TTS processes.

Each piper process loads the ONNX model at spawn time and blocks on
stdin, ready to synthesize.  When acquired, the caller writes one
text line, closes stdin, and reads raw PCM from stdout until EOF.
EOF is the *only* end-of-stream signal — no select() timeouts.

After use (or on cancel) the process is dead and the pool spawns a
replacement in the background so one is always warm.

Pool exhaustion (all slots busy) is logged so we can track whether
the pool size needs to increase.

Usage::

    pool = PiperPool(model="/home/a/models/en_US-ryan-low.onnx",
                     piper_bin="/home/a/venv/bin/piper",
                     size=2)
    pool.start()

    proc, rate = pool.acquire()      # blocks if none ready
    proc.stdin.write(b"Hello world\\n")
    proc.stdin.close()               # signals EOF to piper
    while chunk := os.read(proc.stdout.fileno(), 65536):
        aplay_stdin.write(chunk)     # deterministic EOF termination
    proc.wait()

    # On cancel: just kill the process
    proc.kill()
    proc.wait()
    # stdout EOF fires naturally — read loop exits
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import os
import subprocess
import threading
import time
from queue import Queue, Empty
from typing import Optional

logger: logging.Logger = logging.getLogger("glowup.voice.piper_pool")

# How long acquire() blocks before logging a pool-exhaustion warning
# and continuing to wait.  This is NOT a timeout — it always waits.
# The log entry is diagnostic evidence for needing a larger pool.
_EXHAUSTION_LOG_INTERVAL_S: float = 2.0


class PiperPool:
    """Pool of pre-warmed single-use piper processes.

    Args:
        model:     Path to piper ONNX model file.
        piper_bin: Path to piper binary.
        size:      Number of warm processes to maintain.
    """

    def __init__(
        self,
        model: str,
        piper_bin: str,
        size: int = 2,
    ) -> None:
        """Initialize the pool (does not spawn processes yet)."""
        self._model: str = os.path.expanduser(model)
        self._piper_bin: str = os.path.expanduser(piper_bin)
        self._size: int = size
        self._rate: int = 22050
        self._ready: Queue[subprocess.Popen] = Queue()
        self._running: bool = False
        self._exhaustion_count: int = 0
        self._lock: threading.Lock = threading.Lock()

        # Read sample rate from the model's JSON config.
        model_json: str = self._model + ".json"
        if os.path.exists(model_json):
            with open(model_json, "r") as f:
                mcfg: dict = json.load(f)
            self._rate = mcfg.get("audio", {}).get("sample_rate", 22050)

    @property
    def sample_rate(self) -> int:
        """Audio sample rate for PCM output from this model."""
        return self._rate

    def start(self) -> bool:
        """Validate paths and pre-spawn the pool.

        Returns:
            True if at least one piper process started successfully.
        """
        if not os.path.exists(self._piper_bin):
            logger.error("Piper binary not found: %s", self._piper_bin)
            return False
        if not os.path.exists(self._model):
            logger.error("Piper model not found: %s", self._model)
            return False

        self._running = True

        # Spawn initial pool in background threads so start()
        # returns quickly.  The first acquire() may block until
        # at least one process is warm.
        for _ in range(self._size):
            threading.Thread(
                target=self._spawn_one, daemon=True,
            ).start()

        logger.info(
            "PiperPool started: model=%s rate=%d size=%d",
            os.path.basename(self._model), self._rate, self._size,
        )
        return True

    def acquire(self) -> subprocess.Popen:
        """Get a warm piper process, blocking if none are ready.

        Logs a warning every ``_EXHAUSTION_LOG_INTERVAL_S`` while
        waiting, as evidence that the pool size may need to increase.

        A replacement process is spawned in the background immediately.

        Returns:
            A piper subprocess with stdin/stdout pipes.  The caller
            MUST eventually close stdin or kill the process.
        """
        proc: Optional[subprocess.Popen] = None
        while proc is None:
            try:
                proc = self._ready.get(timeout=_EXHAUSTION_LOG_INTERVAL_S)
            except Empty:
                with self._lock:
                    self._exhaustion_count += 1
                logger.warning(
                    "PiperPool exhausted — all %d slots busy "
                    "(exhaustion count: %d)",
                    self._size, self._exhaustion_count,
                )
                continue

            # Verify the process is still alive (it could have crashed
            # while sitting in the queue).
            if proc.poll() is not None:
                logger.warning("Discarding dead piper from pool (rc=%d)", proc.returncode)
                proc = None
                threading.Thread(
                    target=self._spawn_one, daemon=True,
                ).start()
                continue

        # Spawn replacement in background.
        threading.Thread(target=self._spawn_one, daemon=True).start()

        return proc

    def stop(self) -> None:
        """Kill all pooled processes and stop spawning."""
        self._running = False
        while not self._ready.empty():
            try:
                proc: subprocess.Popen = self._ready.get_nowait()
                proc.kill()
                proc.wait()
            except Empty:
                break
        logger.info(
            "PiperPool stopped (exhaustion events: %d)",
            self._exhaustion_count,
        )

    def _spawn_one(self) -> None:
        """Spawn a single piper process and add it to the ready queue.

        Runs in a background thread.  Logs timing so we know how long
        model load takes on this hardware.
        """
        if not self._running:
            return

        t0: float = time.monotonic()
        try:
            proc: subprocess.Popen = subprocess.Popen(
                [self._piper_bin, "--model", self._model, "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            elapsed: float = time.monotonic() - t0
            self._ready.put(proc)
            logger.info(
                "Piper process spawned in %.1fs (pool: %d ready)",
                elapsed, self._ready.qsize(),
            )
        except Exception as exc:
            logger.error("Failed to spawn piper: %s", exc)
