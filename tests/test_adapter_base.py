"""Exhaustive tests for adapter base classes.

Tests every code path in AdapterBase, MqttAdapterBase,
PollingAdapterBase, and AsyncPollingAdapterBase.  These base classes
underpin every adapter in the system — correctness here is load-bearing.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__ = "1.0"

import asyncio
import logging
import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch, call

from adapters.adapter_base import (
    AdapterBase,
    AsyncPollingAdapterBase,
    MqttAdapterBase,
    PollingAdapterBase,
    SLEEP_CHUNK,
    THREAD_JOIN_TIMEOUT,
    _HAS_PAHO,
)


# =========================================================================
# Concrete test subclasses — minimal implementations for testing
# =========================================================================

class StubAdapter(AdapterBase):
    """Minimal concrete AdapterBase for instantiation tests."""

    def __init__(self) -> None:
        super().__init__()
        self.started: bool = False
        self.stopped: bool = False

    def start(self) -> None:
        """Mark as started."""
        self._running = True
        self.started = True

    def stop(self) -> None:
        """Mark as stopped."""
        self._running = False
        self.stopped = True


class IncompleteAdapter(AdapterBase):
    """AdapterBase subclass missing stop() — should not instantiate."""

    def start(self) -> None:
        pass


class StubMqttAdapter(MqttAdapterBase):
    """Concrete MqttAdapterBase for testing message dispatch."""

    def __init__(
        self,
        broker: str = "localhost",
        port: int = 1883,
        subscribe_prefix: str = "test/topic",
        client_id_prefix: str = "test-mqtt",
    ) -> None:
        super().__init__(broker, port, subscribe_prefix, client_id_prefix)
        self.messages: list[tuple[str, bytes]] = []
        self.started_hook_called: bool = False
        self.stopped_hook_called: bool = False

    def _handle_message(self, topic: str, payload: bytes) -> None:
        """Record received messages."""
        self.messages.append((topic, payload))

    def _on_started(self) -> None:
        self.started_hook_called = True
        super()._on_started()

    def _on_stopped(self) -> None:
        self.stopped_hook_called = True
        super()._on_stopped()


class ExplodingMqttAdapter(MqttAdapterBase):
    """MqttAdapterBase whose _handle_message always raises."""

    def _handle_message(self, topic: str, payload: bytes) -> None:
        """Always explode."""
        raise ValueError(f"boom on {topic}")


class StubPollingAdapter(PollingAdapterBase):
    """Concrete PollingAdapterBase for testing poll lifecycle."""

    def __init__(
        self,
        poll_interval: float = 1.0,
        thread_name: str = "test-poller",
        fail_prerequisites: bool = False,
    ) -> None:
        super().__init__(poll_interval, thread_name)
        self.poll_count: int = 0
        self.poll_timestamps: list[float] = []
        self._fail_prerequisites: bool = fail_prerequisites
        self.started_hook_called: bool = False
        self.stopped_hook_called: bool = False

    def _do_poll(self) -> None:
        """Record poll invocations."""
        self.poll_count += 1
        self.poll_timestamps.append(time.time())

    def _check_prerequisites(self) -> bool:
        return not self._fail_prerequisites

    def _on_started(self) -> None:
        self.started_hook_called = True
        super()._on_started()

    def _on_stopped(self) -> None:
        self.stopped_hook_called = True
        super()._on_stopped()


class StubAsyncAdapter(AsyncPollingAdapterBase):
    """Concrete AsyncPollingAdapterBase for testing async lifecycle."""

    def __init__(
        self,
        thread_name: str = "test-async",
        reconnect_delay: float = 0.05,
        max_reconnect_delay: float = 0.2,
        fail_prerequisites: bool = False,
        connect_error_count: int = 0,
    ) -> None:
        super().__init__(thread_name, reconnect_delay, max_reconnect_delay)
        self._fail_prerequisites: bool = fail_prerequisites
        self._connect_error_count: int = connect_error_count
        self._connect_attempts: int = 0
        self.connected: bool = False
        self.disconnected: bool = False
        self.cycle_count: int = 0
        self.started_hook_called: bool = False
        self.stopped_hook_called: bool = False

    async def _connect(self) -> None:
        """Connect, optionally failing N times first."""
        self._connect_attempts += 1
        if self._connect_attempts <= self._connect_error_count:
            raise ConnectionError(
                f"deliberate failure #{self._connect_attempts}"
            )
        self.connected = True

    async def _disconnect(self) -> None:
        """Mark as disconnected."""
        self.disconnected = True

    async def _run_cycle(self) -> None:
        """Run one cycle then stop."""
        self.cycle_count += 1
        # Run briefly, then exit to let test observe.
        self._running = False

    def _check_prerequisites(self) -> bool:
        return not self._fail_prerequisites

    def _on_started(self) -> None:
        self.started_hook_called = True
        super()._on_started()

    def _on_stopped(self) -> None:
        self.stopped_hook_called = True
        super()._on_stopped()


class LongRunningAsyncAdapter(AsyncPollingAdapterBase):
    """Async adapter that runs until stopped — tests stop() responsiveness."""

    def __init__(self) -> None:
        super().__init__(
            thread_name="test-long-async",
            reconnect_delay=0.05,
            max_reconnect_delay=0.2,
        )
        self.connected: bool = False
        self.disconnected: bool = False

    async def _connect(self) -> None:
        self.connected = True

    async def _disconnect(self) -> None:
        self.disconnected = True

    async def _run_cycle(self) -> None:
        """Spin until stopped."""
        while self._running:
            await asyncio.sleep(0.01)


# =========================================================================
# AdapterBase tests
# =========================================================================

class TestAdapterBase(unittest.TestCase):
    """Tests for the AdapterBase abstract class."""

    def test_cannot_instantiate_directly(self) -> None:
        """AdapterBase is abstract — direct instantiation must raise."""
        with self.assertRaises(TypeError):
            AdapterBase()  # type: ignore[abstract]

    def test_subclass_missing_stop_cannot_instantiate(self) -> None:
        """Subclass that omits stop() is still abstract."""
        with self.assertRaises(TypeError):
            IncompleteAdapter()  # type: ignore[abstract]

    def test_concrete_subclass_instantiates(self) -> None:
        """Fully concrete subclass instantiates without error."""
        adapter = StubAdapter()
        self.assertIsInstance(adapter, AdapterBase)

    def test_running_defaults_false(self) -> None:
        """Newly created adapter is not running."""
        adapter = StubAdapter()
        self.assertFalse(adapter.running)

    def test_running_property_reflects_flag(self) -> None:
        """The running property tracks the _running flag."""
        adapter = StubAdapter()
        adapter._running = True
        self.assertTrue(adapter.running)
        adapter._running = False
        self.assertFalse(adapter.running)

    def test_start_sets_running(self) -> None:
        """StubAdapter.start() sets running to True."""
        adapter = StubAdapter()
        adapter.start()
        self.assertTrue(adapter.running)
        self.assertTrue(adapter.started)

    def test_stop_clears_running(self) -> None:
        """StubAdapter.stop() sets running to False."""
        adapter = StubAdapter()
        adapter.start()
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertTrue(adapter.stopped)

    def test_stop_before_start(self) -> None:
        """Stopping a never-started adapter does not crash."""
        adapter = StubAdapter()
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertTrue(adapter.stopped)

    def test_double_start(self) -> None:
        """Starting twice does not crash."""
        adapter = StubAdapter()
        adapter.start()
        adapter.start()
        self.assertTrue(adapter.running)

    def test_double_stop(self) -> None:
        """Stopping twice does not crash."""
        adapter = StubAdapter()
        adapter.start()
        adapter.stop()
        adapter.stop()
        self.assertFalse(adapter.running)


# =========================================================================
# MqttAdapterBase tests
# =========================================================================

class TestMqttAdapterBaseConstruction(unittest.TestCase):
    """Construction and attribute storage for MqttAdapterBase."""

    def test_stores_broker(self) -> None:
        adapter = StubMqttAdapter(broker="10.0.0.1")
        self.assertEqual(adapter._broker, "10.0.0.1")

    def test_stores_port(self) -> None:
        adapter = StubMqttAdapter(port=1884)
        self.assertEqual(adapter._port, 1884)

    def test_stores_subscribe_prefix(self) -> None:
        adapter = StubMqttAdapter(subscribe_prefix="zigbee2mqtt")
        self.assertEqual(adapter._subscribe_prefix, "zigbee2mqtt")

    def test_stores_client_id_prefix(self) -> None:
        adapter = StubMqttAdapter(client_id_prefix="my-adapter")
        self.assertEqual(adapter._client_id_prefix, "my-adapter")

    def test_running_initially_false(self) -> None:
        adapter = StubMqttAdapter()
        self.assertFalse(adapter.running)

    def test_client_initially_none(self) -> None:
        adapter = StubMqttAdapter()
        self.assertIsNone(adapter._client)

    def test_is_adapter_base(self) -> None:
        """MqttAdapterBase is a proper AdapterBase subclass."""
        adapter = StubMqttAdapter()
        self.assertIsInstance(adapter, AdapterBase)


class TestMqttAdapterBaseStart(unittest.TestCase):
    """Tests for MqttAdapterBase.start()."""

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_creates_client(self, mock_mqtt: MagicMock) -> None:
        """start() creates a paho MQTT client."""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        # Simulate paho v1 (no CallbackAPIVersion).
        mock_mqtt.CallbackAPIVersion = None
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        mock_mqtt.Client.assert_called_once()
        self.assertIs(adapter._client, mock_client)

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_sets_running(self, mock_mqtt: MagicMock) -> None:
        """start() sets running to True."""
        mock_mqtt.Client.return_value = MagicMock()
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        self.assertTrue(adapter.running)

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_client_id_has_prefix(self, mock_mqtt: MagicMock) -> None:
        """Client ID starts with the configured prefix."""
        mock_mqtt.Client.return_value = MagicMock()
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter(client_id_prefix="glowup-ble")
            adapter.start()
        call_kwargs = mock_mqtt.Client.call_args
        # paho v1: Client(client_id=...)
        client_id: str = call_kwargs[1]["client_id"]
        self.assertTrue(client_id.startswith("glowup-ble-"))

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_client_id_has_timestamp_and_counter(
        self, mock_mqtt: MagicMock,
    ) -> None:
        """Client ID has the form ``{prefix}-{epoch}-{counter}``.

        The counter component was added in the post-7679713 fix so
        every recovery rebuild gets a brand-new client_id even if
        two rebuilds happen within the same wall-clock second.
        """
        mock_mqtt.Client.return_value = MagicMock()
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter(client_id_prefix="test")
            adapter.start()
        call_kwargs = mock_mqtt.Client.call_args
        client_id: str = call_kwargs[1]["client_id"]
        # Format: test-<epoch>-<counter>
        parts: list[str] = client_id.split("-")
        self.assertEqual(parts[0], "test")
        self.assertTrue(
            parts[-2].isdigit(),
            f"epoch component is not digits: {parts[-2]!r}",
        )
        self.assertTrue(
            parts[-1].isdigit(),
            f"counter component is not digits: {parts[-1]!r}",
        )
        # First start() call must produce counter == 1.
        self.assertEqual(parts[-1], "1")

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_wires_on_connect(self, mock_mqtt: MagicMock) -> None:
        """start() wires _on_connect to the client."""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter(subscribe_prefix="wire/test")
            adapter.start()
        # Verify the wired callback works — call it and check subscription.
        on_connect = mock_client.on_connect
        sub_client = MagicMock()
        on_connect(sub_client, None, None, 0)
        sub_client.subscribe.assert_called_once_with("wire/test/#")

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_wires_on_message(self, mock_mqtt: MagicMock) -> None:
        """start() wires _on_message_dispatch to the client."""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        # Verify the wired callback dispatches to _handle_message.
        on_message = mock_client.on_message
        msg = MagicMock()
        msg.topic = "test/wire"
        msg.payload = b"hello"
        on_message(None, None, msg)
        self.assertEqual(len(adapter.messages), 1)
        self.assertEqual(adapter.messages[0][0], "test/wire")

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_calls_connect_async(self, mock_mqtt: MagicMock) -> None:
        """start() calls connect_async with broker, port, and keepalive."""
        from adapters.adapter_base import MQTT_KEEPALIVE
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter(broker="10.0.0.5", port=1884)
            adapter.start()
        mock_client.connect_async.assert_called_once_with(
            "10.0.0.5", 1884, keepalive=MQTT_KEEPALIVE,
        )

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_calls_loop_start(self, mock_mqtt: MagicMock) -> None:
        """start() calls loop_start on the client."""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        mock_client.loop_start.assert_called_once()

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_calls_on_started_hook(self, mock_mqtt: MagicMock) -> None:
        """start() calls the _on_started hook."""
        mock_mqtt.Client.return_value = MagicMock()
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        self.assertTrue(adapter.started_hook_called)

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base._PAHO_V2", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_paho_v2_uses_callback_api(
        self, mock_mqtt: MagicMock,
    ) -> None:
        """With paho v2, Client receives CallbackAPIVersion.VERSION2."""
        mock_mqtt.Client.return_value = MagicMock()
        mock_mqtt.CallbackAPIVersion.VERSION2 = "V2"
        adapter = StubMqttAdapter()
        adapter.start()
        mock_mqtt.Client.assert_called_once_with(
            "V2", client_id=unittest.mock.ANY,
        )

    @patch("adapters.adapter_base._HAS_PAHO", False)
    def test_start_without_paho_is_noop(self) -> None:
        """start() without paho-mqtt logs warning, stays stopped."""
        adapter = StubMqttAdapter()
        adapter.start()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter._client)

    @patch("adapters.adapter_base._HAS_PAHO", False)
    def test_start_without_paho_does_not_call_hook(self) -> None:
        """start() without paho does not call _on_started."""
        adapter = StubMqttAdapter()
        adapter.start()
        self.assertFalse(adapter.started_hook_called)


class TestMqttAdapterBaseStop(unittest.TestCase):
    """Tests for MqttAdapterBase.stop()."""

    def test_stop_sets_running_false(self) -> None:
        """stop() clears the running flag."""
        adapter = StubMqttAdapter()
        adapter._running = True
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_calls_loop_stop(self) -> None:
        """stop() calls loop_stop on the client."""
        adapter = StubMqttAdapter()
        adapter._client = MagicMock()
        adapter._running = True
        adapter.stop()
        adapter._client.loop_stop.assert_called_once()

    def test_stop_calls_disconnect(self) -> None:
        """stop() calls disconnect on the client."""
        adapter = StubMqttAdapter()
        adapter._client = MagicMock()
        adapter._running = True
        adapter.stop()
        adapter._client.disconnect.assert_called_once()

    def test_stop_calls_on_stopped_hook(self) -> None:
        """stop() calls the _on_stopped hook."""
        adapter = StubMqttAdapter()
        adapter.stop()
        self.assertTrue(adapter.stopped_hook_called)

    def test_stop_with_no_client(self) -> None:
        """stop() with no client does not crash."""
        adapter = StubMqttAdapter()
        adapter._client = None
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_before_start(self) -> None:
        """stop() before start() is a clean no-op."""
        adapter = StubMqttAdapter()
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertTrue(adapter.stopped_hook_called)

    def test_double_stop(self) -> None:
        """Stopping twice does not crash."""
        adapter = StubMqttAdapter()
        adapter._client = MagicMock()
        adapter.stop()
        adapter.stop()
        self.assertFalse(adapter.running)


class TestMqttAdapterBaseOnConnect(unittest.TestCase):
    """Tests for _on_connect callback."""

    def test_successful_connect_subscribes(self) -> None:
        """rc=0 triggers subscription to {prefix}/#.

        Uses ``glowup/example`` as a generic stand-in prefix.  No
        production adapter currently subscribes there; this test
        exists to verify the base-class wiring, not any specific
        adapter.  (Earlier revisions used ``glowup/ble`` as the
        sample prefix, which became misleading after the BLE
        pivot to the service pattern — see
        docs/35-service-vs-adapter.md.)
        """
        adapter = StubMqttAdapter(subscribe_prefix="glowup/example")
        mock_client = MagicMock()
        adapter._on_connect(mock_client, None, None, 0)
        mock_client.subscribe.assert_called_once_with("glowup/example/#")

    def test_failed_connect_does_not_subscribe(self) -> None:
        """rc != 0 does not subscribe."""
        adapter = StubMqttAdapter()
        mock_client = MagicMock()
        adapter._on_connect(mock_client, None, None, 5)
        mock_client.subscribe.assert_not_called()

    def test_connect_with_various_error_codes(self) -> None:
        """All non-zero rc values prevent subscription."""
        adapter = StubMqttAdapter()
        for rc in [1, 2, 3, 4, 5, 128, 255]:
            mock_client = MagicMock()
            adapter._on_connect(mock_client, None, None, rc)
            mock_client.subscribe.assert_not_called()

    def test_connect_with_paho_v2_properties(self) -> None:
        """v2-style callback with properties arg works."""
        adapter = StubMqttAdapter(subscribe_prefix="z2m")
        mock_client = MagicMock()
        adapter._on_connect(
            mock_client, None, None, 0, properties={"foo": "bar"},
        )
        mock_client.subscribe.assert_called_once_with("z2m/#")

    def test_connect_custom_prefix(self) -> None:
        """Subscription uses the configured prefix."""
        adapter = StubMqttAdapter(subscribe_prefix="custom/deep/topic")
        mock_client = MagicMock()
        adapter._on_connect(mock_client, None, None, 0)
        mock_client.subscribe.assert_called_once_with(
            "custom/deep/topic/#",
        )


class TestMqttAdapterBaseMessageDispatch(unittest.TestCase):
    """Tests for _on_message_dispatch and _handle_message."""

    def test_message_dispatched_to_handler(self) -> None:
        """Incoming message is forwarded to _handle_message."""
        adapter = StubMqttAdapter()
        msg = MagicMock()
        msg.topic = "test/topic/sensor1"
        msg.payload = b'{"temp": 22.5}'
        adapter._on_message_dispatch(None, None, msg)
        self.assertEqual(len(adapter.messages), 1)
        self.assertEqual(adapter.messages[0][0], "test/topic/sensor1")
        self.assertEqual(adapter.messages[0][1], b'{"temp": 22.5}')

    def test_multiple_messages(self) -> None:
        """Multiple messages are all dispatched."""
        adapter = StubMqttAdapter()
        for i in range(5):
            msg = MagicMock()
            msg.topic = f"test/{i}"
            msg.payload = f"payload-{i}".encode()
            adapter._on_message_dispatch(None, None, msg)
        self.assertEqual(len(adapter.messages), 5)
        for i in range(5):
            self.assertEqual(adapter.messages[i][0], f"test/{i}")

    def test_handler_exception_caught(self) -> None:
        """Exception in _handle_message is caught, not propagated."""
        adapter = ExplodingMqttAdapter(
            broker="localhost", port=1883,
            subscribe_prefix="boom", client_id_prefix="boom",
        )
        msg = MagicMock()
        msg.topic = "boom/thing"
        msg.payload = b"data"
        # Must not raise.
        adapter._on_message_dispatch(None, None, msg)

    def test_handler_exception_logged(self) -> None:
        """Exception in _handle_message is logged at WARNING."""
        adapter = ExplodingMqttAdapter(
            broker="localhost", port=1883,
            subscribe_prefix="boom", client_id_prefix="boom",
        )
        msg = MagicMock()
        msg.topic = "boom/thing"
        msg.payload = b"data"
        with self.assertLogs("glowup.adapter_base", level="WARNING") as cm:
            adapter._on_message_dispatch(None, None, msg)
        self.assertTrue(any("boom/thing" in line for line in cm.output))

    def test_message_with_empty_payload(self) -> None:
        """Empty payload is forwarded without error."""
        adapter = StubMqttAdapter()
        msg = MagicMock()
        msg.topic = "test/empty"
        msg.payload = b""
        adapter._on_message_dispatch(None, None, msg)
        self.assertEqual(adapter.messages[0][1], b"")

    def test_message_with_binary_payload(self) -> None:
        """Binary payload passes through unchanged."""
        adapter = StubMqttAdapter()
        msg = MagicMock()
        msg.topic = "test/bin"
        msg.payload = bytes(range(256))
        adapter._on_message_dispatch(None, None, msg)
        self.assertEqual(adapter.messages[0][1], bytes(range(256)))


class TestMqttAdapterBaseOnDisconnect(unittest.TestCase):
    """Tests for _on_disconnect callback."""

    def test_clean_disconnect_sets_connected_false(self) -> None:
        """rc=0 (clean disconnect) clears _connected."""
        adapter = StubMqttAdapter()
        adapter._connected = True
        adapter._on_disconnect(None, None, 0)
        self.assertFalse(adapter._connected)

    def test_unexpected_disconnect_sets_connected_false(self) -> None:
        """rc!=0 (unexpected disconnect) clears _connected."""
        adapter = StubMqttAdapter()
        adapter._connected = True
        adapter._on_disconnect(None, None, 7)
        self.assertFalse(adapter._connected)

    def test_clean_disconnect_logs_info(self) -> None:
        """Clean disconnect logs at INFO, not WARNING."""
        adapter = StubMqttAdapter(client_id_prefix="test-disc")
        adapter._connected = True
        with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
            adapter._on_disconnect(None, None, 0)
        self.assertTrue(
            any("disconnected (clean)" in line for line in cm.output),
        )

    def test_unexpected_disconnect_logs_warning(self) -> None:
        """Unexpected disconnect logs at WARNING with rc."""
        adapter = StubMqttAdapter(client_id_prefix="test-disc")
        adapter._connected = True
        with self.assertLogs("glowup.adapter_base", level="WARNING") as cm:
            adapter._on_disconnect(None, None, 7)
        self.assertTrue(
            any("rc=7" in line for line in cm.output),
        )

    def test_disconnect_with_paho_v2_signature(self) -> None:
        """v2-style callback with (client, userdata, flags, rc, properties)."""
        adapter = StubMqttAdapter()
        adapter._connected = True
        # v2: flags_or_rc=flags_obj, rc=7, properties=props
        adapter._on_disconnect(None, None, {}, 7, {"foo": "bar"})
        self.assertFalse(adapter._connected)

    def test_disconnect_v1_style_clean(self) -> None:
        """v1-style callback with (client, userdata, rc=0)."""
        adapter = StubMqttAdapter()
        adapter._connected = True
        adapter._on_disconnect(None, None, 0)
        self.assertFalse(adapter._connected)

    def test_disconnect_v2_style_clean(self) -> None:
        """v2-style callback with rc=0 is clean disconnect."""
        adapter = StubMqttAdapter(client_id_prefix="v2-clean")
        adapter._connected = True
        with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
            adapter._on_disconnect(None, None, {}, 0, None)
        self.assertTrue(
            any("disconnected (clean)" in line for line in cm.output),
        )


class TestMqttAdapterBaseConnectionState(unittest.TestCase):
    """Tests for _connected state tracking through connect/disconnect."""

    def test_connected_initially_false(self) -> None:
        """_connected starts False before any connection."""
        adapter = StubMqttAdapter()
        self.assertFalse(adapter._connected)

    def test_on_connect_success_sets_connected(self) -> None:
        """Successful _on_connect sets _connected True."""
        adapter = StubMqttAdapter()
        adapter._on_connect(MagicMock(), None, None, 0)
        self.assertTrue(adapter._connected)

    def test_on_connect_failure_leaves_disconnected(self) -> None:
        """Failed _on_connect keeps _connected False."""
        adapter = StubMqttAdapter()
        adapter._on_connect(MagicMock(), None, None, 5)
        self.assertFalse(adapter._connected)

    def test_connect_then_disconnect_cycle(self) -> None:
        """Full connect → disconnect cycle tracks state correctly."""
        adapter = StubMqttAdapter()
        adapter._on_connect(MagicMock(), None, None, 0)
        self.assertTrue(adapter._connected)
        adapter._on_disconnect(None, None, 7)
        self.assertFalse(adapter._connected)

    def test_stop_clears_connected(self) -> None:
        """stop() clears _connected even without paho callbacks."""
        adapter = StubMqttAdapter()
        adapter._connected = True
        adapter._client = MagicMock()
        adapter.stop()
        self.assertFalse(adapter._connected)


class TestMqttAdapterBaseMessageTimestamp(unittest.TestCase):
    """Tests for _last_message_time tracking in _on_message_dispatch."""

    def test_last_message_time_initially_none(self) -> None:
        """_last_message_time is None before any message."""
        adapter = StubMqttAdapter()
        self.assertIsNone(adapter._last_message_time)

    def test_message_updates_timestamp(self) -> None:
        """Receiving a message sets _last_message_time."""
        adapter = StubMqttAdapter()
        msg = MagicMock()
        msg.topic = "test/ts"
        msg.payload = b"data"
        before: float = time.monotonic()
        adapter._on_message_dispatch(None, None, msg)
        after: float = time.monotonic()
        self.assertIsNotNone(adapter._last_message_time)
        self.assertGreaterEqual(adapter._last_message_time, before)
        self.assertLessEqual(adapter._last_message_time, after)

    def test_timestamp_updates_even_on_handler_error(self) -> None:
        """Timestamp is set BEFORE handler runs, so errors don't prevent it."""
        adapter = ExplodingMqttAdapter(
            broker="localhost", port=1883,
            subscribe_prefix="boom", client_id_prefix="boom",
        )
        msg = MagicMock()
        msg.topic = "boom/ts"
        msg.payload = b"data"
        with self.assertLogs("glowup.adapter_base", level="WARNING"):
            adapter._on_message_dispatch(None, None, msg)
        self.assertIsNotNone(adapter._last_message_time)

    def test_successive_messages_advance_timestamp(self) -> None:
        """Each message advances the timestamp."""
        adapter = StubMqttAdapter()
        msg = MagicMock()
        msg.topic = "test/adv"
        msg.payload = b"1"
        adapter._on_message_dispatch(None, None, msg)
        t1: float = adapter._last_message_time
        time.sleep(0.01)
        adapter._on_message_dispatch(None, None, msg)
        t2: float = adapter._last_message_time
        self.assertGreater(t2, t1)


class TestMqttAdapterBaseWatchdog(unittest.TestCase):
    """Tests for the silence watchdog."""

    # NOTE on test history: the original three tests in this class
    # (committed in 7679713 alongside the watchdog itself) were
    # tautologies that set state, did the watchdog's work manually,
    # then asserted the manual work happened.  They never invoked
    # _watchdog_loop or any real watchdog code, and would have
    # passed even if _watchdog_loop were entirely deleted.  They
    # gave false confidence that let the production zombie bug
    # recur on 2026-04-07 (see project_zigbee_adapter_zombie and
    # feedback_adapter_watchdog_test_gap in project memory).  The
    # tests below replace them with real exercises of the actual
    # _watchdog_check and _recover_from_silence methods.

    def test_watchdog_check_returns_false_before_first_message(self) -> None:
        """_watchdog_check skips when no message has ever arrived."""
        adapter = StubMqttAdapter()
        adapter._connected = True
        adapter._last_message_time = None
        adapter._recover_from_silence = MagicMock()  # type: ignore[method-assign]

        result: bool = adapter._watchdog_check()

        self.assertFalse(result)
        adapter._recover_from_silence.assert_not_called()

    def test_watchdog_check_returns_false_within_silence_threshold(self) -> None:
        """_watchdog_check skips when silence is shorter than threshold."""
        from adapters.adapter_base import WATCHDOG_SILENCE_THRESHOLD
        adapter = StubMqttAdapter()
        adapter._connected = True
        # Recent message — much less than the threshold.
        adapter._last_message_time = time.monotonic() - (
            WATCHDOG_SILENCE_THRESHOLD / 2.0
        )
        adapter._recover_from_silence = MagicMock()  # type: ignore[method-assign]

        result: bool = adapter._watchdog_check()

        self.assertFalse(result)
        adapter._recover_from_silence.assert_not_called()

    def test_watchdog_check_returns_false_when_not_connected(self) -> None:
        """_watchdog_check skips when _connected is False (recovery in flight)."""
        from adapters.adapter_base import WATCHDOG_SILENCE_THRESHOLD
        adapter = StubMqttAdapter()
        adapter._connected = False
        adapter._last_message_time = time.monotonic() - (
            WATCHDOG_SILENCE_THRESHOLD + 10
        )
        adapter._recover_from_silence = MagicMock()  # type: ignore[method-assign]

        result: bool = adapter._watchdog_check()

        self.assertFalse(result)
        adapter._recover_from_silence.assert_not_called()

    def test_watchdog_check_triggers_recovery_on_silence(self) -> None:
        """_watchdog_check calls _recover_from_silence when silence exceeds threshold."""
        from adapters.adapter_base import WATCHDOG_SILENCE_THRESHOLD
        adapter = StubMqttAdapter()
        adapter._connected = True
        adapter._last_message_time = time.monotonic() - (
            WATCHDOG_SILENCE_THRESHOLD + 10
        )
        adapter._recover_from_silence = MagicMock()  # type: ignore[method-assign]

        with self.assertLogs("glowup.adapter_base", level="WARNING") as cm:
            result: bool = adapter._watchdog_check()

        self.assertTrue(result)
        adapter._recover_from_silence.assert_called_once()
        self.assertTrue(
            any("forcing reconnect" in line for line in cm.output),
            "Watchdog did not log the forcing-reconnect WARNING",
        )

    def test_recover_from_silence_tears_down_old_client_and_rebuilds(self) -> None:
        """_recover_from_silence calls loop_stop+disconnect on old client, then _create_and_start_client."""
        adapter = StubMqttAdapter()
        old_client: MagicMock = MagicMock()
        adapter._client = old_client
        adapter._connected = True
        adapter._last_message_time = time.monotonic()
        adapter._create_and_start_client = MagicMock()  # type: ignore[method-assign]

        adapter._recover_from_silence()

        # Old client must have been torn down — both calls,
        # in order, on the SAME client object.
        old_client.loop_stop.assert_called_once()
        old_client.disconnect.assert_called_once()
        # State must have been reset to the pre-connection condition
        # so a re-firing watchdog skips until the new client connects.
        self.assertIsNone(adapter._last_message_time)
        self.assertFalse(adapter._connected)
        # And the rebuild must have been invoked.
        adapter._create_and_start_client.assert_called_once()

    def test_recover_from_silence_handles_old_client_already_dead(self) -> None:
        """_recover_from_silence still rebuilds when old client teardown raises."""
        adapter = StubMqttAdapter()
        old_client: MagicMock = MagicMock()
        old_client.loop_stop.side_effect = OSError("socket already dead")
        old_client.disconnect.side_effect = OSError("socket already dead")
        adapter._client = old_client
        adapter._connected = True
        adapter._last_message_time = time.monotonic()
        adapter._create_and_start_client = MagicMock()  # type: ignore[method-assign]

        # Must not raise.
        adapter._recover_from_silence()

        # Rebuild must still happen even when teardown best-effort calls
        # raise — that's the whole point: old socket is already dead,
        # which is exactly the condition that triggered recovery.
        adapter._create_and_start_client.assert_called_once()
        self.assertIsNone(adapter._last_message_time)
        self.assertFalse(adapter._connected)

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_create_and_start_client_uses_unique_client_id_per_call(
        self, mock_mqtt: MagicMock,
    ) -> None:
        """Every call to _create_and_start_client increments _reconnect_count and produces a fresh client_id.

        This is the load-bearing assertion of the post-7679713 fix:
        the production zombie bug came from reusing the same client_id
        across recovery cycles, which let broker-2's mosquitto get
        stuck in a session-takeover state.  Each rebuild MUST get a
        new id.
        """
        # Make every Client() call return a fresh MagicMock so we
        # can inspect them independently.
        clients_created: list[MagicMock] = []
        def _make_client(*args: Any, **kwargs: Any) -> MagicMock:
            c = MagicMock()
            clients_created.append(c)
            return c
        mock_mqtt.Client.side_effect = _make_client

        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter._reconnect_count = 0
            # Call three times in a row — exactly what the watchdog
            # would do across three recovery cycles in the same epoch
            # second.  Counter component must keep them unique.
            adapter._create_and_start_client()
            adapter._create_and_start_client()
            adapter._create_and_start_client()

        self.assertEqual(adapter._reconnect_count, 3)
        self.assertEqual(len(clients_created), 3)

        # Extract the client_id passed to each Client() call.
        ids: list[str] = []
        for call_args in mock_mqtt.Client.call_args_list:
            ids.append(call_args.kwargs["client_id"])
        # All three must be distinct strings — this is the assertion
        # that would have caught the production bug at test time.
        self.assertEqual(len(set(ids)), 3, f"Client IDs collided: {ids}")
        # And each must end with the matching counter suffix.
        self.assertTrue(ids[0].endswith("-1"))
        self.assertTrue(ids[1].endswith("-2"))
        self.assertTrue(ids[2].endswith("-3"))

        # Each client must have had its callbacks wired and loop started.
        for c in clients_created:
            c.connect_async.assert_called_once()
            c.loop_start.assert_called_once()

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_launches_watchdog_thread(
        self, mock_mqtt: MagicMock,
    ) -> None:
        """start() creates and starts the watchdog thread."""
        mock_mqtt.Client.return_value = MagicMock()
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        self.assertIsNotNone(adapter._watchdog_thread)
        self.assertTrue(adapter._watchdog_thread.is_alive())
        # Clean up.
        adapter.stop()

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_watchdog_thread_is_daemon(
        self, mock_mqtt: MagicMock,
    ) -> None:
        """Watchdog thread is a daemon so it dies with the process."""
        mock_mqtt.Client.return_value = MagicMock()
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        self.assertTrue(adapter._watchdog_thread.daemon)
        adapter.stop()

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_watchdog_exits_on_stop(
        self, mock_mqtt: MagicMock,
    ) -> None:
        """Watchdog thread exits promptly when stop() is called."""
        mock_mqtt.Client.return_value = MagicMock()
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        wt = adapter._watchdog_thread
        adapter.stop()
        # Watchdog should exit within a couple seconds (it checks
        # _running every 1s sleep chunk).
        wt.join(timeout=5.0)
        self.assertFalse(wt.is_alive())


class TestMqttAdapterBaseStartWiresDisconnect(unittest.TestCase):
    """Tests that start() wires the on_disconnect callback."""

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_start_wires_on_disconnect(self, mock_mqtt: MagicMock) -> None:
        """start() wires _on_disconnect to the client."""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter()
            adapter.start()
        # Verify on_disconnect was set — invoke it with v1 args.
        on_disconnect = mock_client.on_disconnect
        adapter._connected = True
        on_disconnect(mock_client, None, 7)  # v1: (client, userdata, rc)
        self.assertFalse(adapter._connected)
        adapter.stop()


# =========================================================================
# PollingAdapterBase tests
# =========================================================================

class TestPollingAdapterBaseConstruction(unittest.TestCase):
    """Construction and attribute storage for PollingAdapterBase."""

    def test_stores_poll_interval(self) -> None:
        adapter = StubPollingAdapter(poll_interval=30.0)
        self.assertEqual(adapter._poll_interval, 30.0)

    def test_stores_thread_name(self) -> None:
        adapter = StubPollingAdapter(thread_name="my-poller")
        self.assertEqual(adapter._thread_name, "my-poller")

    def test_running_initially_false(self) -> None:
        adapter = StubPollingAdapter()
        self.assertFalse(adapter.running)

    def test_thread_initially_none(self) -> None:
        adapter = StubPollingAdapter()
        self.assertIsNone(adapter._thread)

    def test_is_adapter_base(self) -> None:
        adapter = StubPollingAdapter()
        self.assertIsInstance(adapter, AdapterBase)


class TestPollingAdapterBaseStart(unittest.TestCase):
    """Tests for PollingAdapterBase.start()."""

    def test_start_sets_running(self) -> None:
        """start() sets running to True."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        self.assertTrue(adapter.running)
        adapter.stop()

    def test_start_creates_daemon_thread(self) -> None:
        """start() creates a daemon thread."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        self.assertIsNotNone(adapter._thread)
        self.assertTrue(adapter._thread.daemon)
        adapter.stop()

    def test_start_thread_has_correct_name(self) -> None:
        """Thread name matches the configured value."""
        adapter = StubPollingAdapter(
            poll_interval=999, thread_name="custom-name",
        )
        adapter.start()
        self.assertEqual(adapter._thread.name, "custom-name")
        adapter.stop()

    def test_start_thread_is_alive(self) -> None:
        """Thread is alive after start."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        self.assertTrue(adapter._thread.is_alive())
        adapter.stop()

    def test_start_calls_on_started_hook(self) -> None:
        """start() calls the _on_started hook."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        self.assertTrue(adapter.started_hook_called)
        adapter.stop()

    def test_start_with_failed_prerequisites(self) -> None:
        """start() with failed prerequisites does not start."""
        adapter = StubPollingAdapter(fail_prerequisites=True)
        adapter.start()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.started_hook_called)

    def test_default_prerequisites_returns_true(self) -> None:
        """Default _check_prerequisites returns True."""
        adapter = StubPollingAdapter()
        self.assertTrue(adapter._check_prerequisites())


class TestPollingAdapterBaseStop(unittest.TestCase):
    """Tests for PollingAdapterBase.stop()."""

    def test_stop_sets_running_false(self) -> None:
        """stop() clears the running flag."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_joins_thread(self) -> None:
        """stop() waits for the thread to finish."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        adapter.stop()
        self.assertFalse(adapter._thread.is_alive())

    def test_stop_calls_on_stopped_hook(self) -> None:
        """stop() calls the _on_stopped hook."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        adapter.stop()
        self.assertTrue(adapter.stopped_hook_called)

    def test_stop_with_no_thread(self) -> None:
        """stop() with no thread does not crash."""
        adapter = StubPollingAdapter()
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_before_start(self) -> None:
        """stop() before start() is a clean no-op."""
        adapter = StubPollingAdapter()
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertTrue(adapter.stopped_hook_called)


class TestPollingAdapterBasePollLoop(unittest.TestCase):
    """Tests for the _poll_loop behavior."""

    def test_immediate_first_poll(self) -> None:
        """First poll happens immediately on start."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        # Give the thread a moment to run.
        time.sleep(0.1)
        self.assertGreaterEqual(adapter.poll_count, 1)
        adapter.stop()

    def test_poll_interval_respected(self) -> None:
        """Polls occur at approximately the configured interval."""
        # Short interval for fast test.
        adapter = StubPollingAdapter(poll_interval=0.15)
        adapter.start()
        # Wait for initial poll + at least one interval poll.
        time.sleep(0.5)
        adapter.stop()
        # Should have at least 2 polls (immediate + 1-2 interval).
        self.assertGreaterEqual(adapter.poll_count, 2)

    def test_stop_interrupts_sleep(self) -> None:
        """stop() wakes the sleeping poll loop quickly."""
        adapter = StubPollingAdapter(poll_interval=60.0)
        adapter.start()
        # Wait for the first poll.
        time.sleep(0.1)
        # Now it's sleeping for 60s — stop should interrupt quickly.
        t0: float = time.time()
        adapter.stop()
        elapsed: float = time.time() - t0
        # Should complete well under SLEEP_CHUNK + margin.
        self.assertLess(elapsed, SLEEP_CHUNK + 1.0)

    def test_stop_prevents_further_polls(self) -> None:
        """After stop(), no more _do_poll calls occur."""
        adapter = StubPollingAdapter(poll_interval=0.05)
        adapter.start()
        time.sleep(0.2)
        adapter.stop()
        count_at_stop: int = adapter.poll_count
        time.sleep(0.2)
        # No additional polls after stop.
        self.assertEqual(adapter.poll_count, count_at_stop)


# =========================================================================
# AsyncPollingAdapterBase tests
# =========================================================================

class TestAsyncPollingAdapterBaseConstruction(unittest.TestCase):
    """Construction and attribute storage for AsyncPollingAdapterBase."""

    def test_stores_thread_name(self) -> None:
        adapter = StubAsyncAdapter(thread_name="my-async")
        self.assertEqual(adapter._thread_name, "my-async")

    def test_stores_reconnect_delay(self) -> None:
        adapter = StubAsyncAdapter(reconnect_delay=15.0)
        self.assertEqual(adapter._initial_reconnect_delay, 15.0)

    def test_stores_max_reconnect_delay(self) -> None:
        adapter = StubAsyncAdapter(max_reconnect_delay=600.0)
        self.assertEqual(adapter._max_reconnect_delay, 600.0)

    def test_running_initially_false(self) -> None:
        adapter = StubAsyncAdapter()
        self.assertFalse(adapter.running)

    def test_thread_initially_none(self) -> None:
        adapter = StubAsyncAdapter()
        self.assertIsNone(adapter._thread)

    def test_loop_initially_none(self) -> None:
        adapter = StubAsyncAdapter()
        self.assertIsNone(adapter._loop)

    def test_is_adapter_base(self) -> None:
        adapter = StubAsyncAdapter()
        self.assertIsInstance(adapter, AdapterBase)


class TestAsyncPollingAdapterBaseStart(unittest.TestCase):
    """Tests for AsyncPollingAdapterBase.start()."""

    def test_start_sets_running(self) -> None:
        adapter = StubAsyncAdapter()
        adapter.start()
        self.assertTrue(adapter.running)
        adapter.stop()

    def test_start_creates_daemon_thread(self) -> None:
        adapter = StubAsyncAdapter()
        adapter.start()
        self.assertIsNotNone(adapter._thread)
        self.assertTrue(adapter._thread.daemon)
        adapter.stop()

    def test_start_thread_has_correct_name(self) -> None:
        adapter = StubAsyncAdapter(thread_name="my-name")
        adapter.start()
        self.assertEqual(adapter._thread.name, "my-name")
        adapter.stop()

    def test_start_calls_on_started_hook(self) -> None:
        adapter = StubAsyncAdapter()
        adapter.start()
        self.assertTrue(adapter.started_hook_called)
        adapter.stop()

    def test_start_with_failed_prerequisites(self) -> None:
        adapter = StubAsyncAdapter(fail_prerequisites=True)
        adapter.start()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.started_hook_called)

    def test_default_prerequisites_returns_true(self) -> None:
        adapter = StubAsyncAdapter()
        self.assertTrue(adapter._check_prerequisites())


class TestAsyncPollingAdapterBaseStop(unittest.TestCase):
    """Tests for AsyncPollingAdapterBase.stop()."""

    def test_stop_sets_running_false(self) -> None:
        adapter = StubAsyncAdapter()
        adapter.start()
        # Wait for cycle to complete (StubAsyncAdapter stops itself).
        time.sleep(0.2)
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_joins_thread(self) -> None:
        adapter = StubAsyncAdapter()
        adapter.start()
        time.sleep(0.2)
        adapter.stop()
        self.assertFalse(adapter._thread.is_alive())

    def test_stop_calls_on_stopped_hook(self) -> None:
        adapter = StubAsyncAdapter()
        adapter.start()
        time.sleep(0.2)
        adapter.stop()
        self.assertTrue(adapter.stopped_hook_called)

    def test_stop_with_no_thread(self) -> None:
        adapter = StubAsyncAdapter()
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_with_no_loop(self) -> None:
        """stop() with no event loop does not crash."""
        adapter = StubAsyncAdapter()
        adapter._loop = None
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_before_start(self) -> None:
        adapter = StubAsyncAdapter()
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertTrue(adapter.stopped_hook_called)


class TestAsyncPollingAdapterBaseLifecycle(unittest.TestCase):
    """Tests for the async connect/cycle/reconnect lifecycle."""

    def test_connect_and_cycle_called(self) -> None:
        """_connect and _run_cycle are called on start."""
        adapter = StubAsyncAdapter()
        adapter.start()
        # StubAsyncAdapter._run_cycle sets _running=False, so it exits.
        time.sleep(0.3)
        adapter.stop()
        self.assertTrue(adapter.connected)
        self.assertEqual(adapter.cycle_count, 1)

    def test_disconnect_called_on_stop(self) -> None:
        """_disconnect is called when stop() is invoked."""
        adapter = LongRunningAsyncAdapter()
        adapter.start()
        time.sleep(0.1)
        adapter.stop()
        time.sleep(0.2)
        self.assertTrue(adapter.disconnected)

    def _wait_for(
        self,
        predicate: "callable[[], bool]",
        timeout: float = 5.0,
        poll: float = 0.01,
    ) -> bool:
        """Poll ``predicate`` until it returns True or ``timeout`` elapses.

        Returns True if the predicate became True, False on timeout.
        Used by the async retry tests to replace fixed time.sleep()
        budgets that flake under load.  See feedback memory entry
        about why fixed-sleep test budgets are an antipattern.
        """
        deadline: float = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(poll)
        return False

    def test_reconnect_on_connect_failure(self) -> None:
        """Connection failures trigger retry with backoff."""
        adapter = StubAsyncAdapter(
            connect_error_count=2,
            reconnect_delay=0.02,
            max_reconnect_delay=0.1,
        )
        adapter.start()
        # Expected sequence: fail, wait 0.02, fail, wait 0.04, succeed,
        # then run one cycle.  Poll until the success-and-cycle state
        # is reached rather than guessing a sleep budget — under load
        # a fixed sleep can wake before the asyncio scheduler has run
        # the 3rd attempt, producing a flake.
        reached: bool = self._wait_for(
            lambda: (
                adapter.connected
                and adapter._connect_attempts == 3
                and adapter.cycle_count == 1
            ),
            timeout=5.0,
        )
        adapter.stop()
        self.assertTrue(
            reached,
            f"State not reached: connected={adapter.connected}, "
            f"attempts={adapter._connect_attempts}, "
            f"cycles={adapter.cycle_count}",
        )

    def test_backoff_resets_after_success(self) -> None:
        """Delay resets to initial value after successful connect."""
        # This is verified by the reconnect test above — if backoff
        # didn't reset, the 3rd attempt would wait too long.
        adapter = StubAsyncAdapter(
            connect_error_count=1,
            reconnect_delay=0.02,
            max_reconnect_delay=0.5,
        )
        adapter.start()
        # Poll for cycle_count == 1 instead of fixed sleep.
        reached: bool = self._wait_for(
            lambda: adapter.cycle_count == 1,
            timeout=5.0,
        )
        adapter.stop()
        self.assertTrue(
            reached,
            f"cycle_count never reached 1: {adapter.cycle_count}",
        )

    def test_backoff_capped_at_max(self) -> None:
        """Reconnect delay does not exceed max_reconnect_delay.

        With initial=0.02, max=0.05: delays would be 0.02, 0.04, 0.05
        (capped at 0.05, not 0.08).  Three failures then success →
        4 total attempts, 1 successful cycle.

        Originally written with a fixed ``time.sleep(0.5)`` budget;
        flaked under load when the asyncio scheduler did not run all
        4 attempts within 500ms.  Now polls for the actual end state
        with a generous upper bound.
        """
        adapter = StubAsyncAdapter(
            connect_error_count=3,
            reconnect_delay=0.02,
            max_reconnect_delay=0.05,
        )
        adapter.start()
        reached: bool = self._wait_for(
            lambda: (
                adapter.connected
                and adapter._connect_attempts == 4
            ),
            timeout=5.0,
        )
        adapter.stop()
        self.assertTrue(
            reached,
            f"State not reached: connected={adapter.connected}, "
            f"attempts={adapter._connect_attempts}",
        )

    def test_stop_interrupts_reconnect_sleep(self) -> None:
        """stop() during reconnect sleep exits quickly."""
        # Adapter that always fails to connect.
        class AlwaysFailAdapter(AsyncPollingAdapterBase):
            async def _connect(self) -> None:
                raise ConnectionError("always fails")
            async def _disconnect(self) -> None:
                pass
            async def _run_cycle(self) -> None:
                pass

        adapter = AlwaysFailAdapter(
            thread_name="always-fail",
            reconnect_delay=60.0,  # Long delay.
            max_reconnect_delay=60.0,
        )
        adapter.start()
        time.sleep(0.1)  # Let it fail and start sleeping.
        t0: float = time.time()
        adapter.stop()
        elapsed: float = time.time() - t0
        # Should exit within THREAD_JOIN_TIMEOUT, not wait 60s.
        self.assertLess(elapsed, THREAD_JOIN_TIMEOUT + 1.0)

    def test_event_loop_created_and_closed(self) -> None:
        """_run_loop creates an event loop and closes it on exit."""
        adapter = StubAsyncAdapter()
        adapter.start()
        time.sleep(0.3)
        adapter.stop()
        # Loop should have been created.
        self.assertIsNotNone(adapter._loop)
        # Loop should be closed after thread exits.
        self.assertTrue(adapter._loop.is_closed())


# =========================================================================
# Cross-cutting concerns
# =========================================================================

class TestConstants(unittest.TestCase):
    """Tests for module-level constants."""

    def test_thread_join_timeout_positive(self) -> None:
        self.assertGreater(THREAD_JOIN_TIMEOUT, 0)

    def test_sleep_chunk_positive(self) -> None:
        self.assertGreater(SLEEP_CHUNK, 0)

    def test_sleep_chunk_less_than_join_timeout(self) -> None:
        """Sleep chunk should be smaller than join timeout."""
        self.assertLess(SLEEP_CHUNK, THREAD_JOIN_TIMEOUT)


class TestPahoDetection(unittest.TestCase):
    """Tests that paho availability is correctly detected."""

    def test_has_paho_is_bool(self) -> None:
        self.assertIsInstance(_HAS_PAHO, bool)

    @unittest.skipUnless(_HAS_PAHO, "paho-mqtt not installed")
    def test_paho_available_means_mqtt_not_none(self) -> None:
        """When paho is available, the mqtt module is importable."""
        import adapters.adapter_base as adapter_base
        self.assertIsNotNone(adapter_base.mqtt)


class TestLogging(unittest.TestCase):
    """Verify that lifecycle events produce log output."""

    @patch("adapters.adapter_base._HAS_PAHO", True)
    @patch("adapters.adapter_base.mqtt")
    def test_mqtt_start_logs(self, mock_mqtt: MagicMock) -> None:
        """MqttAdapterBase start logs at INFO."""
        mock_mqtt.Client.return_value = MagicMock()
        with patch("adapters.adapter_base._PAHO_V2", False):
            adapter = StubMqttAdapter(
                subscribe_prefix="test/log",
                client_id_prefix="log-test",
            )
            with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
                adapter.start()
        self.assertTrue(
            any("log-test" in line and "test/log" in line for line in cm.output),
        )

    @patch("adapters.adapter_base._HAS_PAHO", False)
    def test_mqtt_start_without_paho_logs_warning(self) -> None:
        """MqttAdapterBase start without paho logs WARNING."""
        adapter = StubMqttAdapter(client_id_prefix="no-paho-test")
        with self.assertLogs("glowup.adapter_base", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(
            any("paho-mqtt not installed" in line for line in cm.output),
        )

    def test_mqtt_stop_logs(self) -> None:
        """MqttAdapterBase stop logs at INFO."""
        adapter = StubMqttAdapter(client_id_prefix="stop-log-test")
        with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
            adapter.stop()
        self.assertTrue(
            any("stop-log-test" in line and "stopped" in line for line in cm.output),
        )

    def test_mqtt_connect_failure_logs_warning(self) -> None:
        """_on_connect with rc != 0 logs WARNING."""
        adapter = StubMqttAdapter(client_id_prefix="connect-fail")
        mock_client = MagicMock()
        with self.assertLogs("glowup.adapter_base", level="WARNING") as cm:
            adapter._on_connect(mock_client, None, None, 5)
        self.assertTrue(
            any("connect failed" in line and "rc=5" in line for line in cm.output),
        )

    def test_mqtt_connect_success_logs_info(self) -> None:
        """_on_connect with rc=0 logs subscription at INFO."""
        adapter = StubMqttAdapter(
            subscribe_prefix="log/sub",
            client_id_prefix="connect-ok",
        )
        mock_client = MagicMock()
        with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
            adapter._on_connect(mock_client, None, None, 0)
        self.assertTrue(
            any("subscribed" in line and "log/sub" in line for line in cm.output),
        )

    def test_polling_start_logs(self) -> None:
        """PollingAdapterBase start logs at INFO."""
        adapter = StubPollingAdapter(
            poll_interval=999, thread_name="poll-log-test",
        )
        with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
            adapter.start()
        adapter.stop()
        self.assertTrue(
            any("poll-log-test" in line and "started" in line for line in cm.output),
        )

    def test_polling_stop_logs(self) -> None:
        """PollingAdapterBase stop logs at INFO."""
        adapter = StubPollingAdapter(thread_name="poll-stop-log")
        with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
            adapter.stop()
        self.assertTrue(
            any("poll-stop-log" in line and "stopped" in line for line in cm.output),
        )

    def test_async_start_logs(self) -> None:
        """AsyncPollingAdapterBase start logs at INFO."""
        adapter = StubAsyncAdapter(thread_name="async-log-test")
        with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
            adapter.start()
        time.sleep(0.2)
        adapter.stop()
        self.assertTrue(
            any("async-log-test" in line and "started" in line for line in cm.output),
        )

    def test_async_stop_logs(self) -> None:
        """AsyncPollingAdapterBase stop logs at INFO."""
        adapter = StubAsyncAdapter(thread_name="async-stop-log")
        with self.assertLogs("glowup.adapter_base", level="INFO") as cm:
            adapter.stop()
        self.assertTrue(
            any("async-stop-log" in line and "stopped" in line for line in cm.output),
        )


class TestAbstractMethodEnforcement(unittest.TestCase):
    """Verify that abstract methods are enforced on all base classes."""

    def test_mqtt_without_handle_message(self) -> None:
        """MqttAdapterBase without _handle_message cannot instantiate."""
        with self.assertRaises(TypeError):
            MqttAdapterBase(  # type: ignore[abstract]
                broker="localhost", port=1883,
                subscribe_prefix="test", client_id_prefix="test",
            )

    def test_polling_without_do_poll(self) -> None:
        """PollingAdapterBase without _do_poll cannot instantiate."""
        with self.assertRaises(TypeError):
            PollingAdapterBase(  # type: ignore[abstract]
                poll_interval=1.0, thread_name="test",
            )

    def test_async_without_connect(self) -> None:
        """AsyncPollingAdapterBase without _connect cannot instantiate."""
        # Missing all three abstract methods.
        with self.assertRaises(TypeError):
            AsyncPollingAdapterBase(  # type: ignore[abstract]
                thread_name="test",
            )

    def test_async_partial_implementation(self) -> None:
        """AsyncPollingAdapterBase with only _connect still fails."""
        class Partial(AsyncPollingAdapterBase):
            async def _connect(self) -> None:
                pass

        with self.assertRaises(TypeError):
            Partial(thread_name="test")  # type: ignore[abstract]

    def test_async_two_of_three(self) -> None:
        """AsyncPollingAdapterBase with 2 of 3 methods still fails."""
        class TwoOfThree(AsyncPollingAdapterBase):
            async def _connect(self) -> None:
                pass
            async def _disconnect(self) -> None:
                pass

        with self.assertRaises(TypeError):
            TwoOfThree(thread_name="test")  # type: ignore[abstract]


class TestConcurrencyAndThreadSafety(unittest.TestCase):
    """Tests that verify thread behavior and responsiveness."""

    def test_polling_thread_is_daemon(self) -> None:
        """Polling threads are daemons — won't prevent process exit."""
        adapter = StubPollingAdapter(poll_interval=999)
        adapter.start()
        self.assertTrue(adapter._thread.daemon)
        adapter.stop()

    def test_async_thread_is_daemon(self) -> None:
        """Async threads are daemons."""
        adapter = StubAsyncAdapter()
        adapter.start()
        self.assertTrue(adapter._thread.daemon)
        adapter.stop()

    def test_rapid_start_stop(self) -> None:
        """Rapid start/stop cycles do not crash or leak threads."""
        for _ in range(10):
            adapter = StubPollingAdapter(poll_interval=999)
            adapter.start()
            adapter.stop()
            self.assertFalse(adapter._thread.is_alive())

    def test_rapid_async_start_stop(self) -> None:
        """Rapid async start/stop cycles do not crash."""
        for _ in range(5):
            adapter = StubAsyncAdapter()
            adapter.start()
            time.sleep(0.05)
            adapter.stop()
            self.assertFalse(adapter.running)


if __name__ == "__main__":
    unittest.main()
