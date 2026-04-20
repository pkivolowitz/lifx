"""Tests for the resilient MQTT client helper.

Exhaustive behavior tests for
``infrastructure.mqtt_resilient_client.MqttResilientClient``.  The
helper is the consolidated lifecycle that the voice coordinator and
voice satellite use to avoid the silent-death bug that killed the
coordinator 2026-04-18 (half-open TCP, no on_disconnect callback, no
watchdog, paho's internal autoreconnect never fired).

These tests cover:

    - ``is_available`` truth table
    - ``start`` creates client only when paho is present
    - client_id format (``{prefix}-{epoch}-{counter}``) and counter
      increments across rebuilds
    - ``publish`` forwards to the underlying client and drops
      gracefully when the client is None (watchdog rebuild window)
    - ``on_connect`` applies every configured subscription
    - ``on_connect`` invokes the optional ``on_connected`` hook
    - ``on_disconnect`` flips connection state and logs
    - ``on_message`` updates the watchdog timestamp and dispatches
      to the user callback
    - Watchdog skip conditions (no messages yet, under threshold,
      not connected)
    - Watchdog trigger rebuilds with a fresh client_id
    - ``stop`` tears down cleanly (idempotent)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from infrastructure.mqtt_resilient_client import (
    MqttResilientClient,
    MQTT_KEEPALIVE,
    WATCHDOG_POLL_INTERVAL,
    WATCHDOG_SILENCE_THRESHOLD,
)


def _make_client(
    **overrides: Any,
) -> tuple[MqttResilientClient, MagicMock]:
    """Construct a helper with a mock on_message; return both.

    Defaults: one subscription, trivial on_message that records calls.
    Override any constructor kwarg via ``overrides``.
    """
    on_message = MagicMock()
    kwargs: dict[str, Any] = dict(
        broker="broker.test",
        port=1883,
        client_id_prefix="test",
        subscriptions=[("glowup/test/topic", 1)],
        on_message=on_message,
    )
    kwargs.update(overrides)
    return MqttResilientClient(**kwargs), on_message


# =========================================================================
# Availability and startup gating
# =========================================================================

class TestAvailability(unittest.TestCase):
    """``is_available`` reflects whether paho is importable."""

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    def test_available_when_paho_present(self) -> None:
        client, _ = _make_client()
        self.assertTrue(client.is_available)

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", False)
    def test_unavailable_when_paho_missing(self) -> None:
        client, _ = _make_client()
        self.assertFalse(client.is_available)


class TestStartGating(unittest.TestCase):
    """``start`` is a no-op when paho is missing; idempotent otherwise."""

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", False)
    def test_start_noop_without_paho(self) -> None:
        client, _ = _make_client()
        client.start()
        self.assertIsNone(client._client)
        self.assertFalse(client._running)

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_start_idempotent(self, mock_mqtt: MagicMock) -> None:
        mock_mqtt.Client.return_value = MagicMock()
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client()
            client.start()
            mock_mqtt.Client.reset_mock()
            client.start()  # second call is a no-op
        mock_mqtt.Client.assert_not_called()


# =========================================================================
# Client construction
# =========================================================================

class TestClientConstruction(unittest.TestCase):
    """Behavior of ``_create_and_start_client``."""

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_client_id_has_prefix_epoch_counter(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_mqtt.Client.return_value = MagicMock()
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client(client_id_prefix="coordinator")
            client.start()
        client_id: str = mock_mqtt.Client.call_args[1]["client_id"]
        parts = client_id.split("-")
        self.assertEqual(parts[0], "coordinator")
        self.assertTrue(parts[-2].isdigit())
        self.assertTrue(parts[-1].isdigit())
        self.assertEqual(parts[-1], "1")  # first client has counter == 1

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_paho_v2_uses_callback_api_version(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_mqtt.Client.return_value = MagicMock()
        mock_mqtt.CallbackAPIVersion = MagicMock()
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", True):
            client, _ = _make_client()
            client.start()
        # v2 signature: Client(api_version, client_id=...)
        positional = mock_mqtt.Client.call_args[0]
        self.assertEqual(positional[0], mock_mqtt.CallbackAPIVersion.VERSION2)

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_connect_called_with_keepalive(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client(keepalive=7)
            client.start()
        mock_client.connect.assert_called_once_with(
            "broker.test", 1883, keepalive=7,
        )

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_loop_start_called(self, mock_mqtt: MagicMock) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client()
            client.start()
        mock_client.loop_start.assert_called_once()


# =========================================================================
# on_connect behavior
# =========================================================================

class TestOnConnect(unittest.TestCase):
    """``on_connect`` subscribes to every configured topic on success."""

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_successful_connect_subscribes_all(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client(subscriptions=[
                ("a/topic", 0),
                ("b/topic", 1),
                ("c/topic", 2),
            ])
            client.start()
        # Drive the on_connect callback paho would invoke.
        sub_client = MagicMock()
        mock_client.on_connect(sub_client, None, None, 0)
        self.assertEqual(sub_client.subscribe.call_count, 3)
        calls = sub_client.subscribe.call_args_list
        self.assertEqual(calls[0], ((("a/topic",)), {"qos": 0}))
        self.assertEqual(calls[1], ((("b/topic",)), {"qos": 1}))
        self.assertEqual(calls[2], ((("c/topic",)), {"qos": 2}))

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_failed_connect_leaves_disconnected(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client()
            client.start()
        sub_client = MagicMock()
        mock_client.on_connect(sub_client, None, None, 5)  # rc != 0
        self.assertFalse(client.is_connected)
        sub_client.subscribe.assert_not_called()

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_on_connected_hook_fires_after_subscribe(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        hook = MagicMock()
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client(on_connected=hook)
            client.start()
        mock_client.on_connect(MagicMock(), None, None, 0)
        hook.assert_called_once_with()

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_hook_exception_is_swallowed(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        hook = MagicMock(side_effect=RuntimeError("boom"))
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client(on_connected=hook)
            client.start()
        # Must not raise — a buggy hook cannot crash paho's thread.
        mock_client.on_connect(MagicMock(), None, None, 0)
        self.assertTrue(client.is_connected)


# =========================================================================
# on_disconnect behavior
# =========================================================================

class TestOnDisconnect(unittest.TestCase):
    """``on_disconnect`` flips state and accepts both paho v1 and v2."""

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_disconnect_clears_connected(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client()
            client.start()
        mock_client.on_connect(MagicMock(), None, None, 0)
        self.assertTrue(client.is_connected)
        # paho v1 signature: (client, userdata, rc)
        mock_client.on_disconnect(mock_client, None, 0)
        self.assertFalse(client.is_connected)

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_disconnect_v2_signature(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client()
            client.start()
        # paho v2: (client, userdata, flags, rc, properties)
        mock_client.on_disconnect(mock_client, None, None, 7, None)
        self.assertFalse(client.is_connected)


# =========================================================================
# on_message dispatch + watchdog timestamp update
# =========================================================================

class TestOnMessage(unittest.TestCase):
    """``on_message`` updates the watchdog timestamp and calls user cb."""

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_on_message_updates_timestamp(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, on_msg = _make_client()
            client.start()
        self.assertIsNone(client._last_message_time)
        msg = MagicMock(topic="t", payload=b"p")
        mock_client.on_message(mock_client, None, msg)
        self.assertIsNotNone(client._last_message_time)
        on_msg.assert_called_once_with("t", b"p")

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_user_callback_exception_isolated(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        bad_cb = MagicMock(side_effect=ValueError("boom"))
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client = MqttResilientClient(
                broker="b", port=1, client_id_prefix="x",
                subscriptions=[], on_message=bad_cb,
            )
            client.start()
        msg = MagicMock(topic="t", payload=b"p")
        # Must not raise — protects paho's internal thread.
        mock_client.on_message(mock_client, None, msg)


# =========================================================================
# publish
# =========================================================================

class TestPublish(unittest.TestCase):
    """``publish`` forwards to the underlying client, drops when disconnected."""

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_publish_forwards(self, mock_mqtt: MagicMock) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client()
            client.start()
        client.publish("t", b"p", qos=1, retain=True)
        mock_client.publish.assert_called_once_with(
            "t", b"p", qos=1, retain=True,
        )

    def test_publish_before_start_returns_none(self) -> None:
        client, _ = _make_client()
        # Never started — _client is None.
        self.assertIsNone(client.publish("t", b"p"))

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_publish_during_rebuild_window_returns_none(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_mqtt.Client.return_value = MagicMock()
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client()
            client.start()
        # Simulate the brief window inside _recover_from_silence where
        # the old client has been torn down but the new one has not
        # yet been created.
        client._client = None
        self.assertIsNone(client.publish("t", b"p"))


# =========================================================================
# Silence watchdog
# =========================================================================

class TestWatchdog(unittest.TestCase):
    """Watchdog skip conditions and recovery trigger."""

    def _make_started(
        self, mock_mqtt: MagicMock,
    ) -> tuple[MqttResilientClient, MagicMock]:
        """Return a started client and its first mock paho client."""
        first_client = MagicMock()
        mock_mqtt.Client.return_value = first_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client(silence_threshold=1.0)
            client.start()
        return client, first_client

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_no_recover_without_first_message(
        self, mock_mqtt: MagicMock,
    ) -> None:
        client, first = self._make_started(mock_mqtt)
        first.on_connect(MagicMock(), None, None, 0)
        # No message yet — watchdog must never trigger.
        self.assertFalse(client._watchdog_check())

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_no_recover_under_threshold(
        self, mock_mqtt: MagicMock,
    ) -> None:
        client, first = self._make_started(mock_mqtt)
        first.on_connect(MagicMock(), None, None, 0)
        client._last_message_time = time.monotonic()  # just now
        self.assertFalse(client._watchdog_check())

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_no_recover_while_disconnected(
        self, mock_mqtt: MagicMock,
    ) -> None:
        client, _ = self._make_started(mock_mqtt)
        # Simulate old silence but currently disconnected.
        client._last_message_time = time.monotonic() - 60
        client._connected = False
        self.assertFalse(client._watchdog_check())

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_recover_triggers_rebuild_with_new_client_id(
        self, mock_mqtt: MagicMock,
    ) -> None:
        first_client = MagicMock()
        second_client = MagicMock()
        mock_mqtt.Client.side_effect = [first_client, second_client]
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client(silence_threshold=1.0)
            client.start()
        first_client.on_connect(MagicMock(), None, None, 0)
        # Force the watchdog to trigger: last message long ago.
        client._last_message_time = time.monotonic() - 10
        client._connected = True
        triggered = client._watchdog_check()
        self.assertTrue(triggered)
        # Old client torn down.
        first_client.loop_stop.assert_called_once()
        first_client.disconnect.assert_called_once()
        # New client constructed with counter == 2.
        second_call_id = mock_mqtt.Client.call_args_list[1][1]["client_id"]
        self.assertTrue(second_call_id.endswith("-2"))
        # New client is the one the helper now holds.
        self.assertIs(client._client, second_client)
        # Recovery resets watchdog state until the next message arrives.
        self.assertIsNone(client._last_message_time)
        self.assertFalse(client._connected)


# =========================================================================
# stop
# =========================================================================

class TestStop(unittest.TestCase):
    """``stop`` shuts down cleanly and is idempotent."""

    @patch("infrastructure.mqtt_resilient_client._HAS_PAHO", True)
    @patch("infrastructure.mqtt_resilient_client.mqtt")
    def test_stop_calls_loop_stop_and_disconnect(
        self, mock_mqtt: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("infrastructure.mqtt_resilient_client._PAHO_V2", False):
            client, _ = _make_client()
            client.start()
        client.stop()
        mock_client.loop_stop.assert_called_once()
        mock_client.disconnect.assert_called_once()
        self.assertFalse(client.is_connected)

    def test_stop_without_start_is_noop(self) -> None:
        client, _ = _make_client()
        client.stop()  # must not raise


if __name__ == "__main__":
    unittest.main()
