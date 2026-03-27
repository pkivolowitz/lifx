"""N-body particle simulation operator — GPU stress test.

Subscribes to ``sensor:midi:events`` on the MQTT bus.  Each note_on
spawns a cluster of particles with gravitational and electrostatic
interactions.  The simulation runs continuously, publishing particle
frames to a separate signal for the WebGL emitter (or any subscriber).

The particle count per note is the difficulty knob:

* ``--particles-per-note 100`` — a Pi can handle this.
* ``--particles-per-note 10000`` — needs a Jetson.
* ``--particles-per-note 100000`` — needs the ML box.

O(n²) naive force calculation — 10x more particles = 100x more
compute.  This is intentional: it's a stress test for the
orchestrator's capability-based routing.

The numpy backend is the CPU baseline.  A cupy backend (identical
API) is a drop-in swap for GPU compute.

Usage::

    python3 -m distributed.nbody_operator --particles-per-note 500

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import json
import logging
import signal
import sys
import threading
import time
from typing import Any, Optional

import numpy as np

from network_config import net

logger: logging.Logger = logging.getLogger("glowup.nbody_operator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (from centralized network config).
DEFAULT_BROKER: str = net.broker

# Default MQTT port.
DEFAULT_MQTT_PORT: int = 1883

# MQTT topic prefix.
MQTT_SIGNAL_PREFIX: str = "glowup/signals/"

# Input signal (MIDI events from the bus).
DEFAULT_INPUT_SIGNAL: str = "sensor:midi:events"

# Output signal (particle frames for the WebGL emitter).
DEFAULT_OUTPUT_SIGNAL: str = "operator:nbody:frame"

# MQTT QoS.
MQTT_QOS: int = 0

# Default particles spawned per MIDI note_on event.
DEFAULT_PARTICLES_PER_NOTE: int = 500

# Maximum total particles before oldest are recycled.
DEFAULT_MAX_PARTICLES: int = 50000

# Simulation time step (seconds).
DEFAULT_DT: float = 0.016  # ~60 Hz physics.

# Simulation frame rate (Hz) — how often frames are published.
DEFAULT_SIM_FPS: int = 30

# Gravitational constant (arbitrary units — tuned for visual appeal).
G_CONSTANT: float = 0.5

# Electrostatic constant (same-charge repulsion).
K_ELECTRO: float = 0.2

# Softening factor to prevent singularities at close range.
SOFTENING: float = 0.01

# Damping factor per step (1.0 = no damping, 0.99 = slow decay).
VELOCITY_DAMPING: float = 0.998

# Simulation domain (particles are clamped to this range).
DOMAIN_MIN: float = -1.0
DOMAIN_MAX: float = 1.0

# MIDI note range for position mapping.
MIDI_NOTE_LOW: int = 24
MIDI_NOTE_HIGH: int = 108

# Number of MIDI channels.
NUM_CHANNELS: int = 16

# Particle spawn velocity spread (random scatter around initial velocity).
SPAWN_VELOCITY_SPREAD: float = 0.3

# Mass range for spawned particles.
MASS_MIN: float = 0.5
MASS_MAX: float = 2.0

# Particle lifetime in simulation steps.  After this many steps a
# particle is killed.  Prevents unbounded growth.
# At 10 fps, 150 steps = 15 seconds of life.
DEFAULT_PARTICLE_LIFETIME: int = 150

# Downsampling — publish every Nth particle to keep WebSocket payloads sane.
# At 50K particles, sending all of them at 30fps would be ~60MB/s.
DEFAULT_PUBLISH_MAX_PARTICLES: int = 500


# ---------------------------------------------------------------------------
# NBodySimulation
# ---------------------------------------------------------------------------

class NBodySimulation:
    """N-body particle simulation with O(n²) force calculation.

    All state is stored in numpy arrays for vectorized computation.
    The same code works with cupy by swapping the import.

    Attributes:
        positions:  (N, 2) array of particle positions.
        velocities: (N, 2) array of particle velocities.
        masses:     (N,) array of particle masses.
        charges:    (N,) array of particle charges (channel-derived).
        colors:     (N,) array of color indices (MIDI channel).
        ages:       (N,) array of age in simulation steps.
        alive:      (N,) boolean mask of active particles.
    """

    def __init__(self, max_particles: int = DEFAULT_MAX_PARTICLES,
                 xp: Any = np, forces: bool = False) -> None:
        """Initialize the simulation with pre-allocated arrays.

        Args:
            max_particles: Maximum particle count (pre-allocated).
            xp:            Array module (numpy or cupy).
            forces:        Enable O(n²) pairwise forces (GPU stress test).
                           When False, particles are independent (O(n)).
        """
        self._forces: bool = forces
        self._xp: Any = xp
        self._max: int = max_particles
        self._count: int = 0  # Number of active particles.
        self._next_slot: int = 0  # Ring buffer index for recycling.

        # Pre-allocate all arrays.
        self.positions: Any = xp.zeros((max_particles, 2), dtype=xp.float32)
        self.velocities: Any = xp.zeros((max_particles, 2), dtype=xp.float32)
        self.masses: Any = xp.ones(max_particles, dtype=xp.float32)
        self.charges: Any = xp.zeros(max_particles, dtype=xp.float32)
        self.colors: Any = xp.zeros(max_particles, dtype=xp.int32)
        self.ages: Any = xp.zeros(max_particles, dtype=xp.int32)
        self.alive: Any = xp.zeros(max_particles, dtype=bool)

        # Stats.
        self.step_count: int = 0
        self.last_step_ms: float = 0.0

    @property
    def active_count(self) -> int:
        """Number of currently active particles."""
        return int(self._xp.sum(self.alive))

    def spawn(self, count: int, x: float, y: float,
              vx: float, vy: float, mass: float,
              charge: float, color: int) -> None:
        """Spawn a cluster of particles at a position.

        Particles are placed with random scatter around (x, y) and
        random velocity scatter around (vx, vy).

        Args:
            count:  Number of particles to spawn.
            x, y:   Center position.
            vx, vy: Center velocity.
            mass:   Particle mass.
            charge: Particle charge (for electrostatic force).
            color:  Color index (MIDI channel).
        """
        xp = self._xp

        for i in range(count):
            # Find a dead slot to recycle.  Skip alive particles to
            # prevent overwriting active simulation state.
            slot: int = self._next_slot % self._max
            attempts: int = 0
            while self.alive[slot] and attempts < self._max:
                self._next_slot += 1
                slot = self._next_slot % self._max
                attempts += 1
            if attempts >= self._max:
                break  # All slots occupied — drop the spawn.
            self._next_slot += 1

            self.positions[slot, 0] = x + xp.float32(
                np.random.uniform(-0.05, 0.05))
            self.positions[slot, 1] = y + xp.float32(
                np.random.uniform(-0.05, 0.05))
            self.velocities[slot, 0] = vx + xp.float32(
                np.random.uniform(-SPAWN_VELOCITY_SPREAD,
                                  SPAWN_VELOCITY_SPREAD))
            self.velocities[slot, 1] = vy + xp.float32(
                np.random.uniform(-SPAWN_VELOCITY_SPREAD,
                                  SPAWN_VELOCITY_SPREAD))
            self.masses[slot] = xp.float32(mass)
            self.charges[slot] = xp.float32(charge)
            self.colors[slot] = color
            self.ages[slot] = 0
            self.alive[slot] = True

        self._count = int(xp.sum(self.alive))

    def step(self, dt: float = DEFAULT_DT) -> None:
        """Advance the simulation by one time step.

        Computes all pairwise forces (O(n²)), integrates velocities
        and positions, applies damping and domain clamping.

        Args:
            dt: Time step in seconds.
        """
        xp = self._xp
        t0: float = time.monotonic()

        # Get indices of alive particles.
        alive_idx = xp.where(self.alive)[0]
        n: int = len(alive_idx)

        if n < 2:
            self.last_step_ms = (time.monotonic() - t0) * 1000.0
            self.step_count += 1
            return

        # Extract active particle data.
        pos: Any = self.positions[alive_idx]     # (n, 2)
        vel: Any = self.velocities[alive_idx]    # (n, 2)

        if self._forces and n >= 2:
            # O(n²) pairwise force calculation — GPU stress test mode.
            mass: Any = self.masses[alive_idx]       # (n,)
            charge: Any = self.charges[alive_idx]    # (n,)

            # Compute pairwise displacement vectors.
            dx: Any = pos[xp.newaxis, :, :] - pos[:, xp.newaxis, :]
            dist_sq: Any = xp.sum(dx ** 2, axis=2) + SOFTENING
            dist: Any = xp.sqrt(dist_sq)

            # Gravitational attraction.
            grav_mag: Any = (
                G_CONSTANT * mass[:, xp.newaxis] * mass[xp.newaxis, :]
                / dist_sq
            )

            # Electrostatic repulsion (same-sign charges).
            electro_mag: Any = (
                K_ELECTRO * charge[:, xp.newaxis] * charge[xp.newaxis, :]
                / dist_sq
            )

            net_mag: Any = grav_mag - electro_mag
            unit: Any = dx / dist[:, :, xp.newaxis]
            forces: Any = net_mag[:, :, xp.newaxis] * unit

            # Zero self-interaction.
            eye_mask: Any = xp.eye(n, dtype=bool)
            forces[eye_mask] = 0.0

            total_force: Any = xp.sum(forces, axis=1)
            accel: Any = total_force / mass[:, xp.newaxis]
            vel += accel * dt
        else:
            # O(n) independent mode — simple downward gravity only.
            vel[:, 1] -= 0.1 * dt  # Gentle downward pull.

        vel *= VELOCITY_DAMPING
        pos += vel * dt

        # Clamp to domain (bounce off walls).
        for axis in range(2):
            below: Any = pos[:, axis] < DOMAIN_MIN
            above: Any = pos[:, axis] > DOMAIN_MAX
            pos[below, axis] = DOMAIN_MIN
            vel[below, axis] *= -0.5  # Inelastic bounce.
            pos[above, axis] = DOMAIN_MAX
            vel[above, axis] *= -0.5

        # Write back to main arrays.
        self.positions[alive_idx] = pos
        self.velocities[alive_idx] = vel
        self.ages[alive_idx] += 1

        # Kill old particles to prevent unbounded growth.
        expired: Any = self.ages > DEFAULT_PARTICLE_LIFETIME
        self.alive[expired] = False

        self.step_count += 1
        self.last_step_ms = (time.monotonic() - t0) * 1000.0

    def get_frame(self, max_particles: int = DEFAULT_PUBLISH_MAX_PARTICLES
                  ) -> dict:
        """Extract a frame for publishing to the emitter.

        Downsamples if there are more active particles than
        max_particles to keep WebSocket payloads manageable.

        Args:
            max_particles: Maximum particles to include in the frame.

        Returns:
            Dict with x, y, color arrays and metadata.
        """
        xp = self._xp
        alive_idx = xp.where(self.alive)[0]
        n: int = len(alive_idx)

        if n == 0:
            return {
                "type": "nbody_frame",
                "particles": 0,
                "step": self.step_count,
                "step_ms": round(self.last_step_ms, 2),
            }

        # Downsample if needed.
        if n > max_particles:
            indices = np.random.choice(n, max_particles, replace=False)
            alive_idx = alive_idx[indices]
            n = max_particles

        pos = self.positions[alive_idx]
        cols = self.colors[alive_idx]

        # Convert to Python lists for JSON serialization.
        if hasattr(pos, 'get'):
            # cupy → numpy transfer.
            pos = pos.get()
            cols = cols.get()

        return {
            "type": "nbody_frame",
            "particles": n,
            "total_active": self.active_count,
            "step": self.step_count,
            "step_ms": round(self.last_step_ms, 2),
            "x": [round(v, 3) for v in pos[:, 0].tolist()],
            "y": [round(v, 3) for v in pos[:, 1].tolist()],
            "color": cols.tolist(),
        }


# ---------------------------------------------------------------------------
# NBodyOperator — bus-connected operator
# ---------------------------------------------------------------------------

class NBodyOperator:
    """MQTT-connected N-body operator.

    Subscribes to MIDI events, spawns particles, runs the simulation,
    and publishes particle frames to the output signal.

    Args:
        broker:             MQTT broker host.
        port:               MQTT broker port.
        input_signal:       Signal to subscribe to (MIDI events).
        output_signal:      Signal to publish frames to.
        particles_per_note: Particles spawned per note_on event.
        max_particles:      Maximum total particles.
        sim_fps:            Simulation/publish rate in Hz.
    """

    def __init__(self, broker: str = DEFAULT_BROKER,
                 port: int = DEFAULT_MQTT_PORT,
                 input_signal: str = DEFAULT_INPUT_SIGNAL,
                 output_signal: str = DEFAULT_OUTPUT_SIGNAL,
                 particles_per_note: int = DEFAULT_PARTICLES_PER_NOTE,
                 max_particles: int = DEFAULT_MAX_PARTICLES,
                 sim_fps: int = DEFAULT_SIM_FPS,
                 forces: bool = False) -> None:
        """Initialize the N-body operator.

        Args:
            broker:             MQTT broker host.
            port:               MQTT broker port.
            input_signal:       Input signal name.
            output_signal:      Output signal name.
            particles_per_note: Difficulty knob.
            max_particles:      Particle cap.
            sim_fps:            Physics/publish rate.
            forces:             Enable O(n²) pairwise forces.
        """
        self._forces: bool = forces
        self._broker: str = broker
        self._port: int = port
        self._input_signal: str = input_signal
        self._output_signal: str = output_signal
        self._particles_per_note: int = particles_per_note
        self._sim_fps: int = sim_fps

        # Try cupy first, fall back to numpy.
        try:
            import cupy
            self._xp = cupy
            self._backend_name: str = "cupy (GPU)"
            logger.info("Using cupy GPU backend")
        except ImportError:
            self._xp = np
            self._backend_name = "numpy (CPU)"
            logger.info("cupy not available — using numpy CPU backend")

        self._sim: NBodySimulation = NBodySimulation(
            max_particles=max_particles, xp=self._xp, forces=self._forces,
        )

        self._client: Optional[Any] = None
        self._connected: bool = False
        self._stop_event: threading.Event = threading.Event()
        self._sim_thread: Optional[threading.Thread] = None
        self._lock: threading.Lock = threading.Lock()

        # Stats.
        self._notes_received: int = 0
        self._frames_published: int = 0
        self._start_time: float = 0.0

    def start(self) -> None:
        """Connect to MQTT, start the simulation loop, and block."""
        self._connect_mqtt()
        if not self._connected:
            logger.error("Failed to connect to MQTT broker")
            return

        self._start_time = time.monotonic()
        self._stop_event.clear()

        # Start the simulation thread.
        self._sim_thread = threading.Thread(
            target=self._sim_loop,
            daemon=True,
            name="nbody-sim",
        )
        self._sim_thread.start()

        logger.info(
            "NBodyOperator running — %s, %d particles/note, %d fps",
            self._backend_name, self._particles_per_note, self._sim_fps,
        )

        # Block until stopped.
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(1.0)
                # Periodic status.
                if self._sim.step_count > 0 and self._sim.step_count % 300 == 0:
                    logger.info(
                        "Step %d: %d active particles, %.1f ms/step",
                        self._sim.step_count,
                        self._sim.active_count,
                        self._sim.last_step_ms,
                    )
        except KeyboardInterrupt:
            pass

        self._shutdown()

    def stop(self) -> None:
        """Signal the operator to stop."""
        self._stop_event.set()

    def _shutdown(self) -> None:
        """Clean shutdown."""
        self._stop_event.set()
        if self._sim_thread:
            self._sim_thread.join(timeout=5)
        self._disconnect_mqtt()

        elapsed: float = time.monotonic() - self._start_time
        logger.info(
            "NBodyOperator stopped — %d notes, %d frames, %d steps in %.1f s",
            self._notes_received, self._frames_published,
            self._sim.step_count, elapsed,
        )

    # -------------------------------------------------------------------
    # Simulation loop
    # -------------------------------------------------------------------

    def _sim_loop(self) -> None:
        """Run the physics simulation and publish frames."""
        frame_interval: float = 1.0 / self._sim_fps
        output_topic: str = MQTT_SIGNAL_PREFIX + self._output_signal

        while not self._stop_event.is_set():
            t0: float = time.monotonic()

            # Step the simulation.
            with self._lock:
                self._sim.step(DEFAULT_DT)
                frame: dict = self._sim.get_frame()

            # Publish the frame.
            if self._client and self._connected:
                payload: str = json.dumps(frame, separators=(",", ":"))
                self._client.publish(output_topic, payload, qos=MQTT_QOS)
                self._frames_published += 1

            # Sleep for remainder of frame interval.
            elapsed: float = time.monotonic() - t0
            sleep_time: float = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    # -------------------------------------------------------------------
    # MIDI event handling
    # -------------------------------------------------------------------

    def _on_midi_event(self, event: dict) -> None:
        """Spawn particles from a MIDI event.

        Args:
            event: Parsed JSON MIDI event from the bus.
        """
        event_type: str = event.get("event_type", "")

        if event_type != "note_on":
            return

        self._notes_received += 1

        channel: int = event.get("channel", 0)
        note: int = event.get("note", 60)
        velocity: int = event.get("velocity", 100)

        # Map note to position (0-1 → domain range).
        note_frac: float = (note - MIDI_NOTE_LOW) / max(
            MIDI_NOTE_HIGH - MIDI_NOTE_LOW, 1,
        )
        note_frac = max(0.0, min(1.0, note_frac))
        x: float = DOMAIN_MIN + note_frac * (DOMAIN_MAX - DOMAIN_MIN)
        y: float = 0.0  # Spawn at center height.

        # Velocity from MIDI velocity (upward burst).
        vy: float = (velocity / 127.0) * 0.5
        vx: float = 0.0

        # Mass from velocity (louder = heavier).
        mass: float = MASS_MIN + (velocity / 127.0) * (MASS_MAX - MASS_MIN)

        # Charge from channel (odd channels positive, even negative).
        charge: float = 1.0 if channel % 2 == 0 else -1.0

        with self._lock:
            self._sim.spawn(
                count=self._particles_per_note,
                x=x, y=y, vx=vx, vy=vy,
                mass=mass, charge=charge, color=channel,
            )

    # -------------------------------------------------------------------
    # MQTT connection
    # -------------------------------------------------------------------

    def _connect_mqtt(self) -> None:
        """Connect to the MQTT broker."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError(
                "paho-mqtt is required.  Install with: pip install paho-mqtt"
            )

        client_id: str = f"glowup-nbody-{int(time.time())}"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        try:
            self._client.connect(self._broker, self._port)
            self._client.loop_start()
            self._connected = True
        except Exception as exc:
            logger.error("MQTT connect failed: %s", exc)
            self._connected = False

    def _disconnect_mqtt(self) -> None:
        """Disconnect from the MQTT broker."""
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._connected = False

    def _on_connect(self, client: Any, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        """Subscribe to MIDI events on connect."""
        if reason_code == 0:
            topic: str = MQTT_SIGNAL_PREFIX + self._input_signal
            client.subscribe(topic, qos=MQTT_QOS)
            logger.info("Subscribed to %s", topic)
        else:
            logger.error("MQTT connect refused: %s", reason_code)

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any,
                       reason_code: Any, properties: Any = None) -> None:
        """Handle disconnect."""
        if reason_code != 0:
            logger.warning("MQTT disconnected unexpectedly (rc=%s)",
                           reason_code)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Parse incoming MIDI event and spawn particles."""
        try:
            event: dict = json.loads(msg.payload.decode("utf-8"))
            self._on_midi_event(event)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.debug("Bad event payload: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for the N-body operator."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "GlowUp N-body Operator — particle simulation stress test. "
            "Subscribes to MIDI events, spawns particles, publishes frames."
        ),
    )
    parser.add_argument(
        "--particles-per-note", dest="particles_per_note",
        type=int, default=DEFAULT_PARTICLES_PER_NOTE,
        help=f"Particles spawned per note (default: {DEFAULT_PARTICLES_PER_NOTE})",
    )
    parser.add_argument(
        "--max-particles", dest="max_particles",
        type=int, default=DEFAULT_MAX_PARTICLES,
        help=f"Maximum total particles (default: {DEFAULT_MAX_PARTICLES})",
    )
    parser.add_argument(
        "--fps", type=int, default=DEFAULT_SIM_FPS,
        help=f"Simulation/publish rate in Hz (default: {DEFAULT_SIM_FPS})",
    )
    parser.add_argument(
        "--broker", default=DEFAULT_BROKER,
        help=f"MQTT broker host (default: {DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_MQTT_PORT,
        help=f"MQTT broker port (default: {DEFAULT_MQTT_PORT})",
    )
    parser.add_argument(
        "--input-signal", dest="input_signal",
        default=DEFAULT_INPUT_SIGNAL,
        help=f"Input signal name (default: '{DEFAULT_INPUT_SIGNAL}')",
    )
    parser.add_argument(
        "--output-signal", dest="output_signal",
        default=DEFAULT_OUTPUT_SIGNAL,
        help=f"Output signal name (default: '{DEFAULT_OUTPUT_SIGNAL}')",
    )
    parser.add_argument(
        "--forces", action="store_true",
        help="Enable O(n²) pairwise forces (GPU stress test mode). "
             "Without this flag, particles are independent (O(n), smooth).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args: argparse.Namespace = parser.parse_args()

    # Configure logging.
    level: int = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    operator: NBodyOperator = NBodyOperator(
        broker=args.broker,
        port=args.port,
        input_signal=args.input_signal,
        output_signal=args.output_signal,
        particles_per_note=args.particles_per_note,
        max_particles=args.max_particles,
        sim_fps=args.fps,
        forces=args.forces,
    )

    # Handle Ctrl+C.
    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        operator.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    operator.start()


if __name__ == "__main__":
    main()
