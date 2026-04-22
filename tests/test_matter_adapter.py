"""Tests for adapters.matter_adapter reconnect and thread-survival behavior.

Two classes of bugs are exercised:

1. CancelledError (BaseException in Python 3.8+, not Exception) silently
   killing the background thread when the WebSocket closes mid-session.
   The old ``except Exception`` in ``_run_loop`` didn't catch it, so the
   thread exited with no log and no retry.

2. ``listen_task.done()`` not being checked in the poll loop, leaving the
   adapter spinning against a dead connection indefinitely instead of
   exiting ``_run_async`` and reconnecting within one poll cycle.

All MatterClient / aiohttp / chip.clusters dependencies are mocked.
No python-matter-server installation is required.

Run::

    ~/venv/bin/python -m unittest tests.test_matter_adapter -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__ = "1.0"

import asyncio
import os
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."),
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_CONFIG: dict = {
    "devices": {
        "Backyard Lights 1": {"node_id": 10},
        "Backyard Lights 2": {"node_id": 11},
    }
}

# Override reconnect delays so tests finish in milliseconds.
_FAST_RECONNECT: float = 0.02   # 20 ms
_FAST_MAX:       float = 0.05   # 50 ms


def _install_matter_stubs() -> None:
    """Inject minimal stubs for aiohttp, matter_server, chip.clusters.

    If adapters.matter_adapter was already imported without these packages
    (e.g. by test_adapter_base.py), _HAS_MATTER=False and MatterClient/
    aiohttp/clusters are unbound.  We force all three into the cached module
    so patch() and direct usage both work regardless of import order.
    """
    for mod in (
        "aiohttp",
        "matter_server",
        "matter_server.client",
        "chip",
        "chip.clusters",
        "chip.clusters.Objects",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

    if "adapters.matter_adapter" in sys.modules:
        m = sys.modules["adapters.matter_adapter"]
        m._HAS_MATTER = True
        # Inject the module-level names that the failed import left unbound.
        m.MatterClient = sys.modules["matter_server.client"].MatterClient
        m.aiohttp = sys.modules["aiohttp"]
        m.clusters = sys.modules["chip.clusters.Objects"]


def _make_adapter() -> "MatterAdapter":  # type: ignore[name-defined]
    """Construct a MatterAdapter with optional deps stubbed out."""
    _install_matter_stubs()
    from adapters.matter_adapter import MatterAdapter
    return MatterAdapter(_CONFIG, bus=None)


def _make_mock_client(
    *,
    start_listening_coro: "asyncio.coroutine | None" = None,  # type: ignore
) -> MagicMock:
    """Return a MatterClient mock with AsyncMock connect/disconnect.

    Args:
        start_listening_coro: optional coroutine for start_listening().
            Defaults to one that runs until cancelled (healthy session).
    """
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_node = MagicMock(return_value=None)  # _sync_state skips None

    if start_listening_coro is None:
        async def _healthy_listen() -> None:
            try:
                while True:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass
        client.start_listening = _healthy_listen
    else:
        client.start_listening = start_listening_coro

    return client


def _make_aiohttp_mock() -> MagicMock:
    """Return a mock aiohttp module with a working async ClientSession CM."""
    mock_session = MagicMock()
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(return_value=session_ctx)
    return mock_aiohttp


# ---------------------------------------------------------------------------
# 1. Thread survival — _run_loop must not die on BaseException subclasses
# ---------------------------------------------------------------------------

class RunLoopSurvivalTests(unittest.TestCase):
    """_run_loop catches BaseException so the thread always retries."""

    def _run_with_flaky_async(
        self, raises_first: BaseException, *, wait_s: float = 1.5,
    ) -> tuple[int, bool]:
        """
        Patch _run_async to raise on the first call, succeed on the second.
        Returns (call_count, thread_exited_cleanly).

        wait_s=1.5 is required because the reconnect sleep in _run_loop
        calls time.sleep(1.0) even when the delay constant is short — the
        inner loop uses 1s granularity.  We must stay running until after
        the sleep completes and the second call starts.
        """
        adapter = _make_adapter()
        adapter._running = True
        call_count = {"n": 0}

        async def flaky() -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise raises_first
            # Subsequent calls: run until the adapter is stopped so the
            # thread's outer while-loop exits cleanly.
            while adapter._running:
                await asyncio.sleep(0.01)

        with (
            patch("adapters.matter_adapter.RECONNECT_DELAY", _FAST_RECONNECT),
            patch("adapters.matter_adapter.MAX_RECONNECT_DELAY", _FAST_MAX),
            patch.object(adapter, "_run_async", flaky),
        ):
            t = threading.Thread(target=adapter._run_loop, daemon=True)
            t.start()
            time.sleep(wait_s)       # wait past the ~1s reconnect sleep
            adapter._running = False
            t.join(timeout=3.0)

        return call_count["n"], not t.is_alive()

    def test_survives_cancelled_error(self) -> None:
        """CancelledError must trigger a retry, not kill the thread."""
        count, exited = self._run_with_flaky_async(
            asyncio.CancelledError("simulated WebSocket drop"),
        )
        self.assertGreaterEqual(
            count, 2,
            "Thread must have retried after CancelledError",
        )
        self.assertTrue(exited, "Thread must exit cleanly when stopped")

    def test_survives_connection_error(self) -> None:
        """Ordinary connection errors also retry (regression guard)."""
        count, exited = self._run_with_flaky_async(
            ConnectionRefusedError("server down"),
        )
        self.assertGreaterEqual(count, 2)
        self.assertTrue(exited)

    def test_exits_cleanly_when_running_cleared(self) -> None:
        """Setting _running=False stops the loop even mid-sleep."""
        adapter = _make_adapter()
        adapter._running = True

        async def checkable_sleep() -> None:
            # Poll _running so the coroutine can return as soon as the
            # flag is cleared — asyncio.sleep(60) would block indefinitely.
            while adapter._running:
                await asyncio.sleep(0.01)

        with patch.object(adapter, "_run_async", checkable_sleep):
            t = threading.Thread(target=adapter._run_loop, daemon=True)
            t.start()
            time.sleep(0.1)
            adapter._running = False
            t.join(timeout=2.0)

        self.assertFalse(t.is_alive(), "Thread must stop when _running cleared")


# ---------------------------------------------------------------------------
# 2. listen_task detection — WebSocket drop detected within one poll cycle
# ---------------------------------------------------------------------------

class ListenTaskDetectionTests(unittest.TestCase):
    """_run_async exits promptly when listen_task completes."""

    def _run_async_sync(self, mock_client: MagicMock) -> "MatterAdapter":  # type: ignore
        """Run _run_async to completion in a fresh event loop.

        Patches adapters.matter_adapter.MatterClient and .aiohttp directly
        (where the names are used, not where they're defined) so the stubs
        are visible to _run_async regardless of import order.
        """
        _install_matter_stubs()
        from adapters.matter_adapter import MatterAdapter

        adapter = MatterAdapter(_CONFIG, bus=None)
        adapter._running = True

        loop = asyncio.new_event_loop()
        try:
            async def run_with_cap() -> None:
                # 5 s cap: _run_async should exit well within that after the
                # WebSocket closes.  Timeout here means listen_task detection
                # didn't fire and the test fails on the assertion below.
                await asyncio.wait_for(adapter._run_async(), timeout=5.0)

            with (
                patch(
                    "adapters.matter_adapter.MatterClient",
                    return_value=mock_client,
                ),
                patch(
                    "adapters.matter_adapter.aiohttp",
                    _make_aiohttp_mock(),
                ),
            ):
                loop.run_until_complete(run_with_cap())
        except asyncio.TimeoutError:
            pass  # Assertion below will catch the failure
        finally:
            loop.close()

        return adapter

    def test_exits_when_listen_task_finishes(self) -> None:
        """When start_listening() returns (WebSocket closed), _run_async exits."""
        async def immediate_close() -> None:
            return  # Returns immediately = WebSocket closed

        mock_client = _make_mock_client(
            start_listening_coro=immediate_close,
        )
        adapter = self._run_async_sync(mock_client)
        # If the listen_task check works, _run_async exited within 5s.
        # Asserting _client is None confirms the finally block ran.
        self.assertIsNone(
            adapter._client,
            "_client must be None after _run_async exits",
        )

    def test_client_none_after_exception_in_listen(self) -> None:
        """Exception in start_listening also causes clean exit + _client=None."""
        async def crashing_listen() -> None:
            raise RuntimeError("WebSocket protocol error")

        mock_client = _make_mock_client(
            start_listening_coro=crashing_listen,
        )
        adapter = self._run_async_sync(mock_client)
        self.assertIsNone(adapter._client)

    def test_disconnect_called_on_exit(self) -> None:
        """disconnect() must be called in the finally block every time."""
        async def immediate_close() -> None:
            return

        mock_client = _make_mock_client(
            start_listening_coro=immediate_close,
        )
        self._run_async_sync(mock_client)
        mock_client.disconnect.assert_awaited()


# ---------------------------------------------------------------------------
# 3. End-to-end reconnect — commands work after a drop+reconnect cycle
# ---------------------------------------------------------------------------

class ReconnectCommandTests(unittest.TestCase):
    """After a WebSocket drop, the adapter reconnects and commands succeed.

    This is the test that the adapter watchdog feedback rule demands:
    assert messages flow AFTER reconnect, not just that reconnect occurred.
    """

    def test_power_off_succeeds_after_reconnect(self) -> None:
        """power_off returns True after the adapter reconnects post-drop."""
        _install_matter_stubs()

        connect_count = {"n": 0}
        command_executed = {"on_second": False}

        async def drop_on_first_connect() -> None:
            connect_count["n"] += 1
            if connect_count["n"] == 1:
                # First session: WebSocket closes immediately.
                return
            # Second session: healthy, receives a command.
            try:
                while True:
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass

        async def _fake_send(node_id, endpoint_id, command) -> None:
            command_executed["on_second"] = True

        mock_client = _make_mock_client(
            start_listening_coro=drop_on_first_connect,
        )
        mock_client.send_device_command = _fake_send

        clusters_mod = sys.modules.get("chip.clusters.Objects", MagicMock())
        off_cmd = MagicMock()
        clusters_mod.OnOff = MagicMock()
        clusters_mod.OnOff.Commands = MagicMock()
        clusters_mod.OnOff.Commands.Off = MagicMock(return_value=off_cmd)

        from adapters.matter_adapter import MatterAdapter

        with (
            patch("adapters.matter_adapter.RECONNECT_DELAY", _FAST_RECONNECT),
            patch("adapters.matter_adapter.MAX_RECONNECT_DELAY", _FAST_MAX),
            patch(
                "adapters.matter_adapter.MatterClient",
                return_value=mock_client,
            ),
            patch(
                "adapters.matter_adapter.aiohttp",
                _make_aiohttp_mock(),
            ),
        ):
            adapter = MatterAdapter(_CONFIG, bus=None)
            adapter.start()

            # _run_async sleeps 2 s after connect before entering the poll
            # loop.  Session 1 drops, then session 2 connects + sleeps 2 s.
            # Allow 6 s total.
            deadline = time.monotonic() + 6.0
            while connect_count["n"] < 2 and time.monotonic() < deadline:
                time.sleep(0.05)

            self.assertGreaterEqual(
                connect_count["n"], 2,
                "Adapter must have reconnected after the first drop",
            )

            # Issue a command — must succeed against the live second session.
            result = adapter.power_off("Backyard Lights 1")

            adapter._running = False
            if adapter._thread:
                adapter._thread.join(timeout=3.0)

        self.assertTrue(
            result,
            "power_off must return True after reconnect",
        )
        self.assertTrue(
            command_executed["on_second"],
            "send_device_command must have been called after reconnect",
        )

    def test_power_off_fails_gracefully_when_not_connected(self) -> None:
        """power_off returns False (not exception) when client is None."""
        adapter = _make_adapter()
        adapter._client = None  # Simulate disconnected state
        result = adapter.power_off("Backyard Lights 1")
        self.assertFalse(result)

    def test_unknown_device_returns_false(self) -> None:
        """power_off for a device not in config returns False cleanly."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        result = adapter.power_off("No Such Device")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
