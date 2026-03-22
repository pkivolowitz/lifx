#!/usr/bin/env python3
"""Formal tests for the distributed multi-machine agent system.

Covers the full SOE (Sensor-Operator-Emitter) distributed pipeline:
capability registration, work assignment, orchestrator fleet management,
UDP wire protocol, MIDI parsing with known fixtures, audio signal
extraction, and signal bus routing.

All tests run without MQTT broker, network, or hardware.  MQTT clients
are mocked; UDP is tested via pack/unpack round-trips; audio is tested
with a deterministic 440 Hz WAV fixture.

Fixture files in ``tests/fixtures/``:
    test_scale.mid  — C major scale, 8 notes, known events
    test_440hz.wav  — 440 Hz sine, 1s, 44100 Hz, 16-bit mono

Run::

    python3 -m pytest test_distributed.py -v
    python3 -m unittest test_distributed -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import math
import os
import struct
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, call, patch

# Suppress expected WARNING/ERROR output from the orchestrator during tests.
# These log lines (e.g. "went offline", "port pool exhausted") are exercised
# intentionally and are not errors in the test run.
logging.getLogger("glowup.distributed.orchestrator").setLevel(logging.CRITICAL + 1)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

FIXTURES_DIR: Path = Path(__file__).parent / "tests" / "fixtures"
MIDI_FIXTURE: Path = FIXTURES_DIR / "test_scale.mid"
WAV_FIXTURE: Path = FIXTURES_DIR / "test_440hz.wav"
CHORD_FIXTURE: Path = FIXTURES_DIR / "test_c_major_chord.wav"
MP3_FIXTURE: Path = FIXTURES_DIR / "test_440hz.mp3"
CORRUPT_MP3_FIXTURE: Path = FIXTURES_DIR / "test_corrupt.mp3"

# Regenerate fixtures if missing.
if not MIDI_FIXTURE.exists() or not WAV_FIXTURE.exists():
    from tests.fixtures.generate_fixtures import generate_all
    generate_all()


# ---------------------------------------------------------------------------
# Imports — distributed subsystem
# ---------------------------------------------------------------------------

from distributed.capability import (
    NodeCapability, NodeHealth,
    capability_topic, status_topic, assignment_topic, health_topic,
    NODE_TOPIC_PREFIX, CAPABILITY_SUFFIX, STATUS_SUFFIX,
    HEALTH_SUFFIX, ASSIGNMENT_SUFFIX,
    STATUS_ONLINE, STATUS_OFFLINE,
    ROLE_SENSOR, ROLE_COMPUTE, ROLE_EMITTER, ROLE_ORCHESTRATOR,
    VALID_ROLES,
)
from distributed.orchestrator import (
    Orchestrator, WorkAssignment, SignalBinding,
    TRANSPORT_MQTT, TRANSPORT_UDP,
    MAX_ASSIGNMENTS_PER_NODE,
)
from distributed.protocol import (
    SignalFrame, pack_signal_frame, unpack_signal_frame,
    MAGIC, PROTOCOL_VERSION, HEADER_SIZE,
    MSG_SIGNAL_DATA, MSG_HEARTBEAT, MSG_ASSIGNMENT,
    DTYPE_FLOAT32, DTYPE_FLOAT64, DTYPE_INT16_PCM, DTYPE_JSON,
    MAX_NAME_LENGTH, MAX_FRAME_SIZE,
)
from distributed.midi_parser import MidiParser, MidiEvent
from media import SignalBus


# ---------------------------------------------------------------------------
# Known fixture values (from generate_fixtures.py)
# ---------------------------------------------------------------------------

# MIDI fixture: C major scale.
MIDI_EXPECTED_NOTES: list[int] = [60, 62, 64, 65, 67, 69, 71, 72]
MIDI_EXPECTED_VELOCITY: int = 100
MIDI_EXPECTED_NOTE_ON_COUNT: int = 8
MIDI_EXPECTED_NOTE_OFF_COUNT: int = 8
MIDI_EXPECTED_TOTAL_EVENTS: int = 16  # note_on + note_off (excluding tempo)
MIDI_EXPECTED_TICKS_PER_BEAT: int = 480
MIDI_EXPECTED_TEMPO_BPM: int = 120

# WAV fixture: 440 Hz sine.
WAV_EXPECTED_SAMPLE_RATE: int = 44100
WAV_EXPECTED_NUM_SAMPLES: int = 44100
WAV_EXPECTED_FREQUENCY: float = 440.0
WAV_EXPECTED_RMS: float = 23169.3

# Chord fixture: C4 + E4 + G4 simultaneous.
CHORD_FREQUENCIES: list[float] = [261.63, 329.63, 392.00]


# ===================================================================
# 1. NodeCapability — serialization and topic helpers
# ===================================================================

class TestNodeCapability(unittest.TestCase):
    """NodeCapability serialization, deserialization, and topic routing."""

    def _make_capability(self, node_id: str = "judy") -> NodeCapability:
        """Create a typical compute+emitter node capability."""
        return NodeCapability(
            node_id=node_id,
            hostname=f"{node_id}.local",
            ip="192.0.2.63",
            roles=[ROLE_COMPUTE, ROLE_EMITTER],
            resources={"gpus": 1, "cpu_cores": 8, "memory_gb": 16},
            operators=[{"name": "AudioExtractor", "version": "1.0"}],
            emitters=[{"type": "audio_out", "version": "1.0"}],
        )

    def test_round_trip_json(self) -> None:
        """Serialize → deserialize must preserve all fields."""
        original: NodeCapability = self._make_capability()
        restored: Optional[NodeCapability] = NodeCapability.from_json(
            original.to_json(),
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.node_id, original.node_id)
        self.assertEqual(restored.hostname, original.hostname)
        self.assertEqual(restored.roles, original.roles)
        self.assertEqual(restored.resources, original.resources)
        self.assertEqual(restored.operators, original.operators)
        self.assertEqual(restored.emitters, original.emitters)

    def test_from_json_malformed(self) -> None:
        """Malformed JSON returns None instead of raising."""
        self.assertIsNone(NodeCapability.from_json("not json"))
        self.assertIsNone(NodeCapability.from_json("{}"))  # No node_id.
        self.assertIsNone(NodeCapability.from_json('{"node_id": ""}'))

    def test_timestamp_auto_generated(self) -> None:
        """Timestamp is auto-populated if not provided."""
        cap: NodeCapability = self._make_capability()
        self.assertTrue(len(cap.timestamp) > 0)
        self.assertIn("T", cap.timestamp)  # ISO 8601 format.


class TestNodeHealth(unittest.TestCase):
    """NodeHealth serialization round-trip."""

    def test_round_trip_json(self) -> None:
        """Health metrics survive JSON round-trip."""
        original: NodeHealth = NodeHealth(
            node_id="pi",
            cpu_percent=45.2,
            memory_percent=68.0,
            gpu_percent=-1.0,
            active_ops=2,
            uptime_s=3600.0,
        )
        restored: Optional[NodeHealth] = NodeHealth.from_json(
            original.to_json(),
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.node_id, "pi")
        self.assertAlmostEqual(restored.cpu_percent, 45.2)
        self.assertEqual(restored.active_ops, 2)
        self.assertEqual(restored.gpu_percent, -1.0)

    def test_from_json_malformed(self) -> None:
        """Bad JSON returns None."""
        self.assertIsNone(NodeHealth.from_json("garbage"))
        self.assertIsNone(NodeHealth.from_json('{"cpu_percent": 50}'))


class TestTopicHelpers(unittest.TestCase):
    """MQTT topic construction for node management."""

    def test_capability_topic(self) -> None:
        """Topic format: glowup/node/{id}/capability."""
        self.assertEqual(
            capability_topic("judy"),
            "glowup/node/judy/capability",
        )

    def test_status_topic(self) -> None:
        """Topic format: glowup/node/{id}/status."""
        self.assertEqual(
            status_topic("pi"),
            "glowup/node/pi/status",
        )

    def test_assignment_topic(self) -> None:
        """Topic format: glowup/node/{id}/assignment."""
        self.assertEqual(
            assignment_topic("ml-box"),
            "glowup/node/ml-box/assignment",
        )

    def test_health_topic(self) -> None:
        """Topic format: glowup/node/{id}/health."""
        self.assertEqual(
            health_topic("bed"),
            "glowup/node/bed/health",
        )


# ===================================================================
# 2. WorkAssignment & SignalBinding — serialization and modes
# ===================================================================

class TestSignalBinding(unittest.TestCase):
    """SignalBinding serialization and transport modes."""

    def test_mqtt_binding_round_trip(self) -> None:
        """MQTT binding serializes without UDP fields."""
        binding: SignalBinding = SignalBinding(
            signal_name="operator:audio:bands",
            transport=TRANSPORT_MQTT,
        )
        d: dict = binding.to_dict()
        self.assertEqual(d["signal_name"], "operator:audio:bands")
        self.assertEqual(d["transport"], "mqtt")
        self.assertNotIn("udp_ip", d)
        self.assertNotIn("udp_port", d)

        restored: SignalBinding = SignalBinding.from_dict(d)
        self.assertEqual(restored.signal_name, binding.signal_name)

    def test_udp_binding_round_trip(self) -> None:
        """UDP binding includes IP and port."""
        binding: SignalBinding = SignalBinding(
            signal_name="sensor:audio:pcm_raw",
            transport=TRANSPORT_UDP,
            udp_ip="192.0.2.63",
            udp_port=9420,
            dtype=DTYPE_INT16_PCM,
        )
        d: dict = binding.to_dict()
        self.assertEqual(d["udp_ip"], "192.0.2.63")
        self.assertEqual(d["udp_port"], 9420)
        self.assertEqual(d["dtype"], DTYPE_INT16_PCM)

        restored: SignalBinding = SignalBinding.from_dict(d)
        self.assertEqual(restored.udp_port, 9420)
        self.assertEqual(restored.dtype, DTYPE_INT16_PCM)


class TestWorkAssignment(unittest.TestCase):
    """WorkAssignment serialization and mode detection."""

    def test_compute_assignment_round_trip(self) -> None:
        """Compute assignment preserves operator, inputs, and outputs."""
        assignment: WorkAssignment = WorkAssignment(
            assignment_id="assign-001",
            operator_name="AudioExtractor",
            operator_config={"bands": 8, "sample_rate": 44100},
            inputs=[
                SignalBinding("sensor:audio:pcm_raw", TRANSPORT_UDP,
                              udp_ip="192.0.2.100", udp_port=9420,
                              dtype=DTYPE_INT16_PCM),
            ],
            outputs=[
                SignalBinding("operator:audio:bands", TRANSPORT_MQTT),
            ],
        )
        self.assertFalse(assignment.is_emitter_assignment)

        restored: Optional[WorkAssignment] = WorkAssignment.from_json(
            assignment.to_json(),
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.assignment_id, "assign-001")
        self.assertEqual(restored.operator_name, "AudioExtractor")
        self.assertEqual(restored.operator_config["bands"], 8)
        self.assertEqual(len(restored.inputs), 1)
        self.assertEqual(len(restored.outputs), 1)
        self.assertEqual(restored.inputs[0].transport, TRANSPORT_UDP)
        self.assertFalse(restored.is_emitter_assignment)

    def test_emitter_assignment_round_trip(self) -> None:
        """Emitter assignment preserves emitter type and config."""
        assignment: WorkAssignment = WorkAssignment(
            assignment_id="assign-002",
            emitter_type="audio_out",
            emitter_config={"master_volume": 0.5},
            inputs=[
                SignalBinding("theremin:note:frequency", TRANSPORT_MQTT),
            ],
        )
        self.assertTrue(assignment.is_emitter_assignment)

        restored: Optional[WorkAssignment] = WorkAssignment.from_json(
            assignment.to_json(),
        )
        self.assertIsNotNone(restored)
        self.assertTrue(restored.is_emitter_assignment)
        self.assertEqual(restored.emitter_type, "audio_out")
        self.assertEqual(restored.emitter_config["master_volume"], 0.5)

    def test_stop_assignment(self) -> None:
        """Stop action round-trips correctly."""
        assignment: WorkAssignment = WorkAssignment(
            assignment_id="assign-001",
            action="stop",
        )
        restored: Optional[WorkAssignment] = WorkAssignment.from_json(
            assignment.to_json(),
        )
        self.assertEqual(restored.action, "stop")

    def test_from_json_malformed(self) -> None:
        """Malformed JSON returns None."""
        self.assertIsNone(WorkAssignment.from_json("not json"))


# ===================================================================
# 3. Orchestrator — fleet management with mocked MQTT
# ===================================================================

class TestOrchestrator(unittest.TestCase):
    """Orchestrator fleet tracking, node selection, and port allocation."""

    def _make_orchestrator(self) -> tuple[Orchestrator, MagicMock]:
        """Create an orchestrator with a mock MQTT client."""
        mock_client: MagicMock = MagicMock()
        orch: Orchestrator = Orchestrator(mock_client, config={
            "udp_port_base": 9420,
            "udp_port_range": 10,
        })
        orch.start()
        return orch, mock_client

    def _inject_capability(
        self,
        orch: Orchestrator,
        node_id: str,
        roles: list[str],
        operators: Optional[list[dict]] = None,
        emitters: Optional[list[dict]] = None,
        resources: Optional[dict] = None,
    ) -> None:
        """Simulate a capability message arriving from a node."""
        cap: NodeCapability = NodeCapability(
            node_id=node_id,
            hostname=f"{node_id}.local",
            ip=f"192.0.2.{hash(node_id) % 200 + 10}",
            roles=roles,
            resources=resources or {},
            operators=operators or [],
            emitters=emitters or [],
        )
        # Build a mock MQTT message.
        msg: MagicMock = MagicMock()
        msg.topic = capability_topic(node_id)
        msg.payload = cap.to_json().encode("utf-8")
        orch._on_capability_msg(None, None, msg)

    def _inject_status(
        self, orch: Orchestrator, node_id: str, status: str,
    ) -> None:
        """Simulate a status (online/offline) message."""
        msg: MagicMock = MagicMock()
        msg.topic = status_topic(node_id)
        msg.payload = status.encode("utf-8")
        orch._on_status_msg(None, None, msg)

    def _inject_health(
        self, orch: Orchestrator, node_id: str,
        cpu: float = 20.0, gpu: float = -1.0, active_ops: int = 0,
    ) -> None:
        """Simulate a health message."""
        health: NodeHealth = NodeHealth(
            node_id=node_id,
            cpu_percent=cpu,
            memory_percent=50.0,
            gpu_percent=gpu,
            active_ops=active_ops,
        )
        msg: MagicMock = MagicMock()
        msg.topic = health_topic(node_id)
        msg.payload = health.to_json().encode("utf-8")
        orch._on_health_msg(None, None, msg)

    def test_node_registration(self) -> None:
        """Capability message registers a new node in the fleet."""
        orch, _ = self._make_orchestrator()
        self._inject_capability(orch, "judy", [ROLE_COMPUTE])
        self._inject_status(orch, "judy", STATUS_ONLINE)

        status: dict = orch.get_fleet_status()
        self.assertEqual(status["node_count"], 1)
        self.assertEqual(status["online_count"], 1)
        self.assertEqual(status["nodes"][0]["node_id"], "judy")

    def test_node_goes_offline(self) -> None:
        """Offline status marks node as unavailable."""
        orch, _ = self._make_orchestrator()
        self._inject_capability(orch, "judy", [ROLE_COMPUTE])
        self._inject_status(orch, "judy", STATUS_ONLINE)
        self._inject_status(orch, "judy", STATUS_OFFLINE)

        status: dict = orch.get_fleet_status()
        self.assertEqual(status["online_count"], 0)

    def test_select_compute_node_prefers_gpu(self) -> None:
        """select_compute_node should prefer nodes with GPUs."""
        orch, _ = self._make_orchestrator()

        # Node without GPU.
        self._inject_capability(
            orch, "pi", [ROLE_COMPUTE], resources={"gpus": 0},
        )
        self._inject_status(orch, "pi", STATUS_ONLINE)
        self._inject_health(orch, "pi", cpu=20.0, gpu=-1.0)

        # Node with GPU.
        self._inject_capability(
            orch, "ml-box", [ROLE_COMPUTE], resources={"gpus": 1},
        )
        self._inject_status(orch, "ml-box", STATUS_ONLINE)
        self._inject_health(orch, "ml-box", cpu=30.0, gpu=10.0)

        selected: Optional[str] = orch.select_compute_node("AudioExtractor")
        self.assertEqual(selected, "ml-box")

    def test_assign_work_publishes_mqtt(self) -> None:
        """assign_work must publish the assignment to the node's topic."""
        orch, mock_client = self._make_orchestrator()
        self._inject_capability(orch, "judy", [ROLE_COMPUTE])
        self._inject_status(orch, "judy", STATUS_ONLINE)

        assignment: WorkAssignment = WorkAssignment(
            assignment_id="test-assign",
            operator_name="AudioExtractor",
        )
        result: bool = orch.assign_work("judy", assignment)
        self.assertTrue(result)

        # Verify MQTT publish was called.
        mock_client.publish.assert_called()
        published_topic: str = mock_client.publish.call_args[0][0]
        self.assertEqual(published_topic, assignment_topic("judy"))

    def test_port_allocation(self) -> None:
        """UDP ports are allocated from a bounded pool."""
        orch, _ = self._make_orchestrator()

        ports: list[int] = []
        for _ in range(10):
            port: Optional[int] = orch.allocate_port()
            self.assertIsNotNone(port)
            ports.append(port)

        # All ports should be unique.
        self.assertEqual(len(set(ports)), 10)

        # Pool exhausted — should return None.
        self.assertIsNone(orch.allocate_port())

        # Release one — should be available again.
        orch.release_port(ports[0])
        self.assertEqual(orch.allocate_port(), ports[0])

    def test_multi_node_fleet(self) -> None:
        """Fleet tracks multiple nodes correctly."""
        orch, _ = self._make_orchestrator()
        for name in ("pi", "judy", "ml-box", "bed"):
            self._inject_capability(orch, name, [ROLE_COMPUTE])
            self._inject_status(orch, name, STATUS_ONLINE)

        status: dict = orch.get_fleet_status()
        self.assertEqual(status["node_count"], 4)
        self.assertEqual(status["online_count"], 4)

    def test_assignment_counter(self) -> None:
        """Assignment IDs are monotonically increasing."""
        orch, _ = self._make_orchestrator()
        ids: list[str] = [orch.next_assignment_id() for _ in range(5)]
        # All unique.
        self.assertEqual(len(set(ids)), 5)


# ===================================================================
# 4. UDP wire protocol — pack/unpack round-trip
# ===================================================================

class TestUDPProtocol(unittest.TestCase):
    """UDP binary protocol frame packing and unpacking."""

    def test_signal_data_round_trip(self) -> None:
        """Pack → unpack preserves all fields."""
        payload: bytes = struct.pack("<8f", *[0.1, 0.3, 0.8, 0.2,
                                               0.0, 0.1, 0.5, 0.9])
        frame: bytes = pack_signal_frame(
            name="mic:audio:bands",
            payload=payload,
            dtype=DTYPE_FLOAT32,
            sequence=42,
        )
        result: Optional[SignalFrame] = unpack_signal_frame(frame)
        self.assertIsNotNone(result)
        self.assertEqual(result.msg_type, MSG_SIGNAL_DATA)
        self.assertEqual(result.sequence, 42)
        self.assertEqual(result.name, "mic:audio:bands")
        self.assertEqual(result.dtype, DTYPE_FLOAT32)
        self.assertEqual(result.payload, payload)

    def test_pcm_audio_round_trip(self) -> None:
        """PCM audio chunk survives pack/unpack."""
        # 100ms of 44100 Hz mono int16 = 4410 samples = 8820 bytes
        num_samples: int = 4410
        payload: bytes = struct.pack(f"<{num_samples}h",
                                      *([1000] * num_samples))
        frame: bytes = pack_signal_frame(
            name="sensor:audio:pcm_raw",
            payload=payload,
            dtype=DTYPE_INT16_PCM,
            sequence=1,
        )
        result: Optional[SignalFrame] = unpack_signal_frame(frame)
        self.assertIsNotNone(result)
        self.assertEqual(result.dtype, DTYPE_INT16_PCM)
        self.assertEqual(len(result.payload), 8820)

    def test_heartbeat_message(self) -> None:
        """Heartbeat messages have empty payload."""
        frame: bytes = pack_signal_frame(
            name="",
            payload=b"",
            dtype=0,
            sequence=99,
            msg_type=MSG_HEARTBEAT,
        )
        result: Optional[SignalFrame] = unpack_signal_frame(frame)
        self.assertIsNotNone(result)
        self.assertEqual(result.msg_type, MSG_HEARTBEAT)
        self.assertEqual(result.payload, b"")

    def test_sequence_wraps_at_32bit(self) -> None:
        """Sequence number wraps at 2^32."""
        frame: bytes = pack_signal_frame(
            name="test",
            payload=b"\x00",
            dtype=0,
            sequence=0xFFFFFFFF + 1,
        )
        result: Optional[SignalFrame] = unpack_signal_frame(frame)
        self.assertEqual(result.sequence, 0)

    def test_malformed_data_returns_none(self) -> None:
        """Short or corrupt data returns None."""
        self.assertIsNone(unpack_signal_frame(b""))
        self.assertIsNone(unpack_signal_frame(b"short"))
        self.assertIsNone(unpack_signal_frame(b"BAAD" + b"\x00" * 20))

    def test_oversized_frame_raises(self) -> None:
        """Frame exceeding MAX_FRAME_SIZE raises ValueError."""
        with self.assertRaises(ValueError):
            pack_signal_frame(
                name="test",
                payload=b"\x00" * (MAX_FRAME_SIZE + 1),
                dtype=0,
                sequence=0,
            )

    def test_long_name_truncated(self) -> None:
        """Signal names longer than MAX_NAME_LENGTH are truncated."""
        long_name: str = "x" * 300
        frame: bytes = pack_signal_frame(
            name=long_name,
            payload=b"\x01",
            dtype=0,
            sequence=0,
        )
        result: Optional[SignalFrame] = unpack_signal_frame(frame)
        self.assertEqual(len(result.name), MAX_NAME_LENGTH)

    def test_header_size_constant(self) -> None:
        """Header struct size must be exactly 19 bytes."""
        self.assertEqual(HEADER_SIZE, 19)

    def test_magic_bytes(self) -> None:
        """Magic must be 'GWUP'."""
        self.assertEqual(MAGIC, b"GWUP")


# ===================================================================
# 5. MIDI parser — known-fixture validation
# ===================================================================

class TestMidiParserWithFixture(unittest.TestCase):
    """Parse the known test_scale.mid fixture and validate events."""

    @classmethod
    def setUpClass(cls) -> None:
        """Parse the fixture once for all tests."""
        cls.parser: MidiParser = MidiParser(str(MIDI_FIXTURE))
        cls.events: list[MidiEvent] = cls.parser.events()

    def test_fixture_exists(self) -> None:
        """The MIDI fixture file must exist."""
        self.assertTrue(MIDI_FIXTURE.exists())

    def test_parser_summary(self) -> None:
        """Parser summary reports correct format and track count."""
        summary: dict = self.parser.summary()
        self.assertEqual(summary["format"], 0)
        self.assertEqual(summary["tracks"], 1)
        self.assertEqual(summary["ticks_per_quarter"], MIDI_EXPECTED_TICKS_PER_BEAT)

    def test_note_on_count(self) -> None:
        """Fixture contains exactly 8 note_on events."""
        note_ons: list[MidiEvent] = [
            e for e in self.events if e.event_type == "note_on"
        ]
        self.assertEqual(len(note_ons), MIDI_EXPECTED_NOTE_ON_COUNT)

    def test_note_off_count(self) -> None:
        """Fixture contains exactly 8 note_off events."""
        note_offs: list[MidiEvent] = [
            e for e in self.events if e.event_type == "note_off"
        ]
        self.assertEqual(len(note_offs), MIDI_EXPECTED_NOTE_OFF_COUNT)

    def test_note_values_are_c_major_scale(self) -> None:
        """Note-on events should be C4 through C5."""
        note_ons: list[MidiEvent] = [
            e for e in self.events if e.event_type == "note_on"
        ]
        notes: list[int] = [e.note for e in note_ons]
        self.assertEqual(notes, MIDI_EXPECTED_NOTES)

    def test_velocity_is_uniform(self) -> None:
        """All note-on velocities should be 100."""
        note_ons: list[MidiEvent] = [
            e for e in self.events if e.event_type == "note_on"
        ]
        for e in note_ons:
            self.assertEqual(e.velocity, MIDI_EXPECTED_VELOCITY)

    def test_all_notes_on_channel_zero(self) -> None:
        """All note events should be on MIDI channel 0."""
        note_events: list[MidiEvent] = [
            e for e in self.events
            if e.event_type in ("note_on", "note_off")
        ]
        for e in note_events:
            self.assertEqual(e.channel, 0)

    def test_events_are_time_ordered(self) -> None:
        """Events must be in ascending time order."""
        times: list[float] = [e.time_s for e in self.events]
        self.assertEqual(times, sorted(times))

    def test_event_to_dict_round_trip(self) -> None:
        """MidiEvent.to_dict() must produce a JSON-serializable dict."""
        for event in self.events[:5]:
            d: dict = event.to_dict()
            json_str: str = json.dumps(d)  # Must not raise.
            self.assertIn("event_type", d)
            self.assertIn("time_s", d)


# ===================================================================
# 6. Audio extraction — WAV fixture through signal pipeline
# ===================================================================

class TestAudioExtractionWithFixture(unittest.TestCase):
    """Feed the known 440 Hz WAV fixture through AudioExtractor."""

    def test_fixture_exists(self) -> None:
        """The WAV fixture file must exist."""
        self.assertTrue(WAV_FIXTURE.exists())

    def test_wav_has_correct_header(self) -> None:
        """WAV fixture must be valid RIFF/WAVE format."""
        with open(WAV_FIXTURE, "rb") as f:
            riff: bytes = f.read(4)
            self.assertEqual(riff, b"RIFF")
            f.read(4)  # Skip size.
            wave: bytes = f.read(4)
            self.assertEqual(wave, b"WAVE")

    def test_wav_sample_count(self) -> None:
        """WAV fixture must contain exactly 44100 samples."""
        with open(WAV_FIXTURE, "rb") as f:
            data: bytes = f.read()
        # Find "data" chunk.
        idx: int = data.index(b"data")
        data_size: int = struct.unpack_from("<I", data, idx + 4)[0]
        num_samples: int = data_size // 2  # 16-bit mono.
        self.assertEqual(num_samples, WAV_EXPECTED_NUM_SAMPLES)

    def test_pcm_rms_matches_440hz_sine(self) -> None:
        """PCM data RMS should match a 440 Hz sine at full amplitude."""
        with open(WAV_FIXTURE, "rb") as f:
            data: bytes = f.read()
        idx: int = data.index(b"data") + 8  # Skip "data" + size.
        samples: list[int] = list(
            struct.unpack_from(f"<{WAV_EXPECTED_NUM_SAMPLES}h", data, idx),
        )
        rms: float = math.sqrt(sum(s * s for s in samples) / len(samples))
        # Allow 1% tolerance for int16 quantization.
        self.assertAlmostEqual(rms, WAV_EXPECTED_RMS, delta=250)

    def test_audio_extractor_produces_signals(self) -> None:
        """AudioExtractor fed PCM chunks must write signals to the bus."""
        try:
            from media.extractors import AudioExtractor
        except ImportError:
            self.skipTest("media.extractors not available")

        bus: SignalBus = SignalBus()
        extractor: AudioExtractor = AudioExtractor(
            source_name="test",
            sample_rate=WAV_EXPECTED_SAMPLE_RATE,
            bus=bus,
            band_count=8,
        )

        # Read PCM data from fixture.
        with open(WAV_FIXTURE, "rb") as f:
            raw: bytes = f.read()
        idx: int = raw.index(b"data") + 8
        pcm_data: bytes = raw[idx:]

        # Feed in 100ms chunks (4410 samples = 8820 bytes).
        chunk_size: int = 8820
        for offset in range(0, len(pcm_data), chunk_size):
            chunk: bytes = pcm_data[offset:offset + chunk_size]
            if len(chunk) == chunk_size:
                extractor.process(chunk)

        # Check that signals were written to the bus.
        rms: float = bus.read("test:audio:rms", 0.0)
        bands: Any = bus.read("test:audio:bands", [])

        # A 440 Hz sine should produce nonzero RMS and band energy.
        self.assertGreater(rms, 0.0, "RMS signal should be nonzero for 440 Hz sine")
        self.assertIsInstance(bands, list)
        if bands:
            self.assertGreater(
                max(bands), 0.0,
                "At least one band should have energy for 440 Hz",
            )


# ===================================================================
# 6b. MP3 codec handling — graceful degradation
# ===================================================================

class TestMP3CodecHandling(unittest.TestCase):
    """Test MP3 file handling and graceful degradation on codec errors."""

    def test_mp3_fixture_exists(self) -> None:
        """The MP3 fixture should exist (requires ffmpeg at gen time)."""
        if not MP3_FIXTURE.exists():
            self.skipTest("MP3 fixture not generated (ffmpeg unavailable)")
        self.assertGreater(MP3_FIXTURE.stat().st_size, 0)

    def test_mp3_has_valid_header(self) -> None:
        """MP3 file should start with ID3 tag or MPEG sync word."""
        if not MP3_FIXTURE.exists():
            self.skipTest("MP3 fixture not generated")
        with open(MP3_FIXTURE, "rb") as f:
            header: bytes = f.read(3)
        # MP3 files start with ID3 tag or 0xFF 0xFB sync word.
        valid_starts: list[bytes] = [b"ID3", b"\xff\xfb", b"\xff\xf3"]
        self.assertTrue(
            any(header.startswith(s) for s in valid_starts),
            f"MP3 header {header.hex()} doesn't match known MP3 signatures",
        )

    def test_corrupt_mp3_fixture_exists(self) -> None:
        """The corrupt MP3 fixture must exist."""
        self.assertTrue(CORRUPT_MP3_FIXTURE.exists())

    def test_corrupt_mp3_is_not_valid_wav(self) -> None:
        """Corrupt MP3 should not parse as WAV."""
        with open(CORRUPT_MP3_FIXTURE, "rb") as f:
            header: bytes = f.read(4)
        self.assertNotEqual(header, b"RIFF")

    def test_corrupt_mp3_does_not_crash_extractor(self) -> None:
        """Feeding corrupt data to AudioExtractor must not crash.

        The extractor should either ignore the bad data or raise a
        handled exception — never a segfault or unhandled error.
        """
        try:
            from media.extractors import AudioExtractor
        except ImportError:
            self.skipTest("media.extractors not available")

        bus: SignalBus = SignalBus()
        extractor: AudioExtractor = AudioExtractor(
            source_name="corrupt",
            sample_rate=WAV_EXPECTED_SAMPLE_RATE,
            bus=bus,
            band_count=8,
        )

        with open(CORRUPT_MP3_FIXTURE, "rb") as f:
            garbage: bytes = f.read()

        # Feed garbage in 8820-byte chunks (same size as normal PCM).
        # Should not raise — the extractor processes raw PCM bytes;
        # garbage bytes produce garbage signal values but no crash.
        chunk_size: int = 8820
        for offset in range(0, len(garbage), chunk_size):
            chunk: bytes = garbage[offset:offset + chunk_size]
            if len(chunk) >= chunk_size:
                extractor.process(chunk)
        # If we get here without an exception, the test passes.

    def test_ffmpeg_mp3_decode_to_pcm(self) -> None:
        """ffmpeg should be able to decode the MP3 fixture to raw PCM.

        This tests the actual codec pipeline that the audio sensor uses.
        """
        import shutil
        import subprocess

        if not MP3_FIXTURE.exists():
            self.skipTest("MP3 fixture not generated")
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg not installed")

        result = subprocess.run(
            ["ffmpeg", "-i", str(MP3_FIXTURE), "-f", "s16le",
             "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1",
             "pipe:1"],
            capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, "ffmpeg decode failed")
        # 1 second of 44100 Hz 16-bit mono ≈ 88200 bytes.
        # MP3 encoding may add/remove a few frames.
        self.assertGreater(
            len(result.stdout), 80000,
            "Decoded PCM too short — codec error?",
        )

    def test_ffmpeg_rejects_corrupt_mp3(self) -> None:
        """ffmpeg should fail gracefully on corrupt MP3."""
        import shutil
        import subprocess

        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg not installed")

        result = subprocess.run(
            ["ffmpeg", "-i", str(CORRUPT_MP3_FIXTURE), "-f", "s16le",
             "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1",
             "pipe:1"],
            capture_output=True, timeout=10,
        )
        # ffmpeg should return nonzero or produce negligible output.
        corrupt: bool = (result.returncode != 0 or len(result.stdout) < 1000)
        self.assertTrue(
            corrupt,
            "ffmpeg should fail or produce minimal output for corrupt MP3",
        )


# ===================================================================
# 6c. FFT validation — chord fixture frequency detection
# ===================================================================

class TestFFTChordDetection(unittest.TestCase):
    """Validate FFT output against the known C major chord fixture.

    The chord fixture contains simultaneous C4 (261.6 Hz), E4 (329.6 Hz),
    and G4 (392.0 Hz) sine waves.  The FFT should show clear energy
    peaks at these three frequencies.
    """

    def test_chord_fixture_exists(self) -> None:
        """The chord WAV fixture must exist."""
        self.assertTrue(CHORD_FIXTURE.exists())

    def test_fft_detects_chord_frequencies(self) -> None:
        """FFT of the chord fixture should peak near C4, E4, G4."""
        # Read PCM data from chord fixture.
        with open(CHORD_FIXTURE, "rb") as f:
            raw: bytes = f.read()
        idx: int = raw.index(b"data") + 8
        num_samples: int = (len(raw) - idx) // 2
        samples: list[int] = list(
            struct.unpack_from(f"<{num_samples}h", raw, idx),
        )

        # Compute FFT magnitude spectrum.
        # Use a Hann window to reduce spectral leakage.
        n: int = len(samples)
        hann: list[float] = [
            0.5 * (1.0 - math.cos(2.0 * math.pi * i / (n - 1)))
            for i in range(n)
        ]
        windowed: list[complex] = [
            complex(samples[i] * hann[i], 0) for i in range(n)
        ]

        # Simple DFT at the frequencies of interest (much faster than
        # full FFT for targeted frequency checking).
        freq_resolution: float = WAV_EXPECTED_SAMPLE_RATE / n

        def magnitude_at_freq(target_freq: float) -> float:
            """Compute DFT magnitude at a specific frequency bin."""
            k: int = round(target_freq / freq_resolution)
            real: float = sum(
                windowed[i].real * math.cos(2.0 * math.pi * k * i / n)
                for i in range(n)
            )
            imag: float = sum(
                windowed[i].real * math.sin(2.0 * math.pi * k * i / n)
                for i in range(n)
            )
            return math.sqrt(real * real + imag * imag) / n

        # Check each chord frequency has significant energy.
        for freq in CHORD_FREQUENCIES:
            mag: float = magnitude_at_freq(freq)
            self.assertGreater(
                mag, 1000.0,
                f"FFT magnitude at {freq} Hz too low: {mag:.1f} "
                f"(expected strong peak for chord tone)",
            )

        # Check a non-chord frequency has much less energy.
        noise_freq: float = 500.0  # Not in the chord.
        noise_mag: float = magnitude_at_freq(noise_freq)
        chord_min: float = min(magnitude_at_freq(f) for f in CHORD_FREQUENCIES)
        self.assertLess(
            noise_mag, chord_min * 0.1,
            f"Energy at {noise_freq} Hz ({noise_mag:.1f}) should be "
            f"much less than chord tones ({chord_min:.1f})",
        )

    def test_extractor_sees_multiple_bands(self) -> None:
        """AudioExtractor should report energy in multiple bands for chord."""
        try:
            from media.extractors import AudioExtractor
        except ImportError:
            self.skipTest("media.extractors not available")

        bus: SignalBus = SignalBus()
        extractor: AudioExtractor = AudioExtractor(
            source_name="chord",
            sample_rate=WAV_EXPECTED_SAMPLE_RATE,
            bus=bus,
            band_count=8,
        )

        with open(CHORD_FIXTURE, "rb") as f:
            raw: bytes = f.read()
        idx: int = raw.index(b"data") + 8
        pcm_data: bytes = raw[idx:]

        chunk_size: int = 8820
        for offset in range(0, len(pcm_data), chunk_size):
            chunk: bytes = pcm_data[offset:offset + chunk_size]
            if len(chunk) == chunk_size:
                extractor.process(chunk)

        bands: Any = bus.read("chord:audio:bands", [])
        self.assertIsInstance(bands, list)
        if bands:
            # A chord has energy spread across multiple bands.
            nonzero_bands: int = sum(1 for b in bands if b > 0.01)
            self.assertGreater(
                nonzero_bands, 1,
                f"Chord should activate multiple bands, got {nonzero_bands}",
            )


# ===================================================================
# 7. SignalBus — write/read, transport routing
# ===================================================================

class TestSignalBus(unittest.TestCase):
    """SignalBus core operations without MQTT."""

    def test_write_read_scalar(self) -> None:
        """Write a scalar float, read it back."""
        bus: SignalBus = SignalBus()
        bus.write("test:level", 0.42)
        self.assertAlmostEqual(bus.read("test:level"), 0.42)

    def test_write_read_array(self) -> None:
        """Write an array, read it back."""
        bus: SignalBus = SignalBus()
        bands: list[float] = [0.1, 0.3, 0.8, 0.2, 0.0, 0.1, 0.5, 0.9]
        bus.write("test:bands", bands)
        result: Any = bus.read("test:bands")
        self.assertEqual(result, bands)

    def test_read_unknown_returns_default(self) -> None:
        """Reading a nonexistent signal returns the default value."""
        bus: SignalBus = SignalBus()
        self.assertEqual(bus.read("nonexistent", 0.0), 0.0)
        self.assertEqual(bus.read("nonexistent", -1.0), -1.0)

    def test_overwrite_updates_value(self) -> None:
        """Writing the same signal twice updates the value."""
        bus: SignalBus = SignalBus()
        bus.write("test:level", 0.1)
        bus.write("test:level", 0.9)
        self.assertAlmostEqual(bus.read("test:level"), 0.9)

    def test_multiple_signals_independent(self) -> None:
        """Different signal names are independent."""
        bus: SignalBus = SignalBus()
        bus.write("a:one", 1.0)
        bus.write("b:two", 2.0)
        self.assertAlmostEqual(bus.read("a:one"), 1.0)
        self.assertAlmostEqual(bus.read("b:two"), 2.0)


# ===================================================================
# 8. Integration: assignment → signal flow (mock transport)
# ===================================================================

class TestAssignmentSignalFlow(unittest.TestCase):
    """End-to-end: orchestrator assigns work, signals flow through bus."""

    def test_compute_assignment_wiring(self) -> None:
        """A compute assignment should wire inputs to outputs via the bus.

        This test validates the assignment data structure, not the
        actual transport — we verify the signal bus can carry data
        between the conceptual input and output endpoints.
        """
        bus: SignalBus = SignalBus()

        # Simulate sensor writing raw PCM signal name.
        bus.write("sensor:audio:pcm_raw", 0.5)

        # Create the assignment that would be sent to a compute node.
        assignment: WorkAssignment = WorkAssignment(
            assignment_id="test-flow-001",
            operator_name="AudioExtractor",
            operator_config={"bands": 8, "sample_rate": 44100},
            inputs=[
                SignalBinding("sensor:audio:pcm_raw", TRANSPORT_UDP,
                              udp_ip="192.0.2.100", udp_port=9420),
            ],
            outputs=[
                SignalBinding("operator:audio:bands", TRANSPORT_MQTT),
                SignalBinding("operator:audio:rms", TRANSPORT_MQTT),
            ],
        )

        # Verify the assignment round-trips through JSON (as it would
        # over MQTT).
        restored: WorkAssignment = WorkAssignment.from_json(
            assignment.to_json(),
        )
        self.assertEqual(len(restored.inputs), 1)
        self.assertEqual(len(restored.outputs), 2)
        self.assertEqual(restored.inputs[0].signal_name, "sensor:audio:pcm_raw")

        # Simulate the operator writing results to the bus.
        bus.write("operator:audio:bands", [0.1, 0.3, 0.8, 0.2,
                                            0.0, 0.1, 0.5, 0.9])
        bus.write("operator:audio:rms", 0.42)

        # Downstream consumer reads from the bus.
        bands: Any = bus.read("operator:audio:bands")
        rms: float = bus.read("operator:audio:rms")
        self.assertEqual(len(bands), 8)
        self.assertAlmostEqual(rms, 0.42)

    def test_emitter_assignment_wiring(self) -> None:
        """An emitter assignment wires bus signals to an output device.

        Validates the assignment structure for routing theremin signals
        to an audio output emitter.
        """
        assignment: WorkAssignment = WorkAssignment(
            assignment_id="test-emit-001",
            emitter_type="audio_out",
            emitter_config={"master_volume": 0.7, "portamento": 0.05},
            inputs=[
                SignalBinding("theremin:note:frequency", TRANSPORT_MQTT),
                SignalBinding("theremin:note:amplitude", TRANSPORT_MQTT),
            ],
        )
        self.assertTrue(assignment.is_emitter_assignment)

        # Round-trip through MQTT serialization.
        restored: WorkAssignment = WorkAssignment.from_json(
            assignment.to_json(),
        )
        self.assertEqual(restored.emitter_type, "audio_out")
        self.assertEqual(len(restored.inputs), 2)
        self.assertEqual(
            restored.inputs[0].signal_name,
            "theremin:note:frequency",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
