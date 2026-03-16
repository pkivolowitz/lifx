"""N-body particle visualizer — operator + WebGL emitter in one process.

Subscribes to ``sensor:midi:events`` on the MQTT bus, runs an N-body
particle simulation, and serves a WebGL page that polls for frames
via HTTP.  No WebSocket, no asyncio — just a shared variable and a
standard threaded HTTP server.

Usage::

    python3 -m distributed.nbody_visualizer --particles-per-note 50
    # Open http://localhost:8421 in a browser

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import argparse
import json
import logging
import signal
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Optional

import numpy as np

logger: logging.Logger = logging.getLogger("glowup.nbody_visualizer")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BROKER: str = "10.0.0.48"
DEFAULT_MQTT_PORT: int = 1883
MQTT_SIGNAL_PREFIX: str = "glowup/signals/"
DEFAULT_INPUT_SIGNAL: str = "sensor:midi:events"
MQTT_QOS: int = 0

DEFAULT_HTTP_PORT: int = 8421
DEFAULT_PARTICLES_PER_NOTE: int = 50
DEFAULT_MAX_PARTICLES: int = 5000
DEFAULT_SIM_FPS: int = 20
DEFAULT_DT: float = 0.016
DEFAULT_PARTICLE_LIFETIME: int = 200
VELOCITY_DAMPING: float = 0.998
DOMAIN_MIN: float = -1.0
DOMAIN_MAX: float = 1.0
SPAWN_VELOCITY_SPREAD: float = 0.3
MIDI_NOTE_LOW: int = 24
MIDI_NOTE_HIGH: int = 108

CHANNEL_COLORS: list[str] = [
    "#FF0000", "#0055FF", "#00AA00", "#CC00DD",
    "#88CC00", "#00CCCC", "#FF6600", "#8800CC",
    "#CCCC00", "#009999", "#CC8800", "#44BB44",
    "#6600AA", "#BBAA00", "#4488CC", "#DD0066",
]


# ---------------------------------------------------------------------------
# Particle simulation
# ---------------------------------------------------------------------------

class ParticleSystem:
    """O(n) independent particle system.

    Args:
        max_particles: Pre-allocated capacity.
        lifetime:      Steps before a particle dies.
    """

    def __init__(self, max_particles: int = DEFAULT_MAX_PARTICLES,
                 lifetime: int = DEFAULT_PARTICLE_LIFETIME) -> None:
        self._max: int = max_particles
        self._lifetime: int = lifetime
        self._next: int = 0

        self.px: np.ndarray = np.zeros(max_particles, dtype=np.float32)
        self.py: np.ndarray = np.zeros(max_particles, dtype=np.float32)
        self.vx: np.ndarray = np.zeros(max_particles, dtype=np.float32)
        self.vy: np.ndarray = np.zeros(max_particles, dtype=np.float32)
        self.color: np.ndarray = np.zeros(max_particles, dtype=np.int32)
        self.age: np.ndarray = np.zeros(max_particles, dtype=np.int32)
        self.alive: np.ndarray = np.zeros(max_particles, dtype=bool)
        self.step_count: int = 0
        self.step_ms: float = 0.0

    @property
    def active_count(self) -> int:
        return int(np.sum(self.alive))

    def spawn(self, count: int, x: float, y: float,
              vx: float, vy: float, color: int) -> None:
        for _ in range(count):
            s: int = self._next % self._max
            self._next += 1
            self.px[s] = x + np.random.uniform(-0.03, 0.03)
            self.py[s] = y + np.random.uniform(-0.03, 0.03)
            self.vx[s] = vx + np.random.uniform(
                -SPAWN_VELOCITY_SPREAD, SPAWN_VELOCITY_SPREAD)
            self.vy[s] = vy + np.random.uniform(
                -SPAWN_VELOCITY_SPREAD, SPAWN_VELOCITY_SPREAD)
            self.color[s] = color
            self.age[s] = 0
            self.alive[s] = True

    def step(self, dt: float = DEFAULT_DT) -> None:
        t0: float = time.monotonic()
        idx = np.where(self.alive)[0]
        if len(idx) == 0:
            self.step_ms = 0.0
            self.step_count += 1
            return

        self.vy[idx] -= 0.15 * dt
        self.vx[idx] *= VELOCITY_DAMPING
        self.vy[idx] *= VELOCITY_DAMPING
        self.px[idx] += self.vx[idx] * dt
        self.py[idx] += self.vy[idx] * dt

        for arr, varr in [(self.px, self.vx), (self.py, self.vy)]:
            below = idx[arr[idx] < DOMAIN_MIN]
            above = idx[arr[idx] > DOMAIN_MAX]
            arr[below] = DOMAIN_MIN
            varr[below] *= -0.5
            arr[above] = DOMAIN_MAX
            varr[above] *= -0.5

        self.age[idx] += 1
        expired = idx[self.age[idx] > self._lifetime]
        self.alive[expired] = False
        self.step_count += 1
        self.step_ms = (time.monotonic() - t0) * 1000.0

    def get_frame_json(self) -> str:
        """Build compact JSON of alive particles."""
        idx = np.where(self.alive)[0]
        n: int = len(idx)
        if n == 0:
            return '{"p":0}'
        return json.dumps({
            "p": n,
            "x": [round(float(v), 3) for v in self.px[idx]],
            "y": [round(float(v), 3) for v in self.py[idx]],
            "c": self.color[idx].tolist(),
        }, separators=(",", ":"))


# ---------------------------------------------------------------------------
# HTML page — polls /frame at animation rate
# ---------------------------------------------------------------------------

def _build_html() -> str:
    colors_js: str = json.dumps(CHANNEL_COLORS)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>GlowUp N-body</title>
<style>
  body {{ margin: 0; background: #000; overflow: hidden; }}
  canvas {{ display: block; width: 100vw; height: 100vh; }}
  #hud {{
    position: fixed; top: 10px; left: 10px;
    color: #666; font: 13px monospace;
    pointer-events: none;
  }}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="hud"></div>
<script>
const COLORS = {colors_js};
const RGB = COLORS.map(h => [
  parseInt(h.slice(1,3),16)/255,
  parseInt(h.slice(3,5),16)/255,
  parseInt(h.slice(5,7),16)/255
]);

const canvas = document.getElementById('c');
const gl = canvas.getContext('webgl');
const hud = document.getElementById('hud');

function resize() {{
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
  gl.viewport(0, 0, canvas.width, canvas.height);
}}
window.addEventListener('resize', resize);
resize();

const vs = `
  attribute vec2 aP;
  attribute vec3 aC;
  varying vec3 vC;
  void main() {{
    gl_Position = vec4(aP, 0.0, 1.0);
    gl_PointSize = 4.0;
    vC = aC;
  }}
`;
const fs = `
  precision mediump float;
  varying vec3 vC;
  void main() {{
    float d = length(gl_PointCoord - vec2(0.5));
    if (d > 0.5) discard;
    float a = 1.0 - smoothstep(0.2, 0.5, d);
    gl_FragColor = vec4(vC, a);
  }}
`;

function mkShader(src, type) {{
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  return s;
}}

const prog = gl.createProgram();
gl.attachShader(prog, mkShader(vs, gl.VERTEX_SHADER));
gl.attachShader(prog, mkShader(fs, gl.FRAGMENT_SHADER));
gl.linkProgram(prog);
gl.useProgram(prog);

const aP = gl.getAttribLocation(prog, 'aP');
const aC = gl.getAttribLocation(prog, 'aC');
const pBuf = gl.createBuffer();
const cBuf = gl.createBuffer();

gl.enable(gl.BLEND);
gl.blendFunc(gl.SRC_ALPHA, gl.ONE);

let frame = null;
let polls = 0;

function render() {{
  gl.clearColor(0, 0, 0, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  if (frame && frame.p > 0) {{
    const n = frame.p;
    const pd = new Float32Array(n * 2);
    const cd = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {{
      pd[i*2] = frame.x[i];
      pd[i*2+1] = frame.y[i];
      const rgb = RGB[frame.c[i] % RGB.length];
      cd[i*3] = rgb[0];
      cd[i*3+1] = rgb[1];
      cd[i*3+2] = rgb[2];
    }}
    gl.bindBuffer(gl.ARRAY_BUFFER, pBuf);
    gl.bufferData(gl.ARRAY_BUFFER, pd, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(aP);
    gl.vertexAttribPointer(aP, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, cBuf);
    gl.bufferData(gl.ARRAY_BUFFER, cd, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(aC);
    gl.vertexAttribPointer(aC, 3, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.POINTS, 0, n);
  }}
  requestAnimationFrame(render);
}}
requestAnimationFrame(render);

// Poll for frames via HTTP — no WebSocket needed.
async function poll() {{
  try {{
    const r = await fetch('/frame');
    if (r.ok) {{
      frame = await r.json();
      polls++;
      if (polls % 20 === 0)
        hud.textContent = (frame.p || 0) + ' particles';
    }}
  }} catch(e) {{}}
  setTimeout(poll, 50);  // ~20 fps polling.
}}
poll();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Visualizer
# ---------------------------------------------------------------------------

class NBodyVisualizer:
    """N-body operator + HTTP/WebGL emitter in one process.

    MIDI events arrive via MQTT.  Physics runs in a thread.
    HTTP serves the page and the latest frame.  No WebSocket.
    """

    def __init__(self, broker: str = DEFAULT_BROKER,
                 mqtt_port: int = DEFAULT_MQTT_PORT,
                 input_signal: str = DEFAULT_INPUT_SIGNAL,
                 http_port: int = DEFAULT_HTTP_PORT,
                 particles_per_note: int = DEFAULT_PARTICLES_PER_NOTE,
                 max_particles: int = DEFAULT_MAX_PARTICLES,
                 sim_fps: int = DEFAULT_SIM_FPS) -> None:
        self._broker: str = broker
        self._mqtt_port: int = mqtt_port
        self._input_signal: str = input_signal
        self._http_port: int = http_port
        self._particles_per_note: int = particles_per_note
        self._sim_fps: int = sim_fps

        self._sim: ParticleSystem = ParticleSystem(
            max_particles=max_particles,
        )

        self._mqtt_client: Optional[Any] = None
        self._mqtt_connected: bool = False
        self._stop_event: threading.Event = threading.Event()
        self._sim_lock: threading.Lock = threading.Lock()

        # Latest frame — written by sim thread, read by HTTP handler.
        self._latest_frame: str = '{"p":0}'
        self._frame_lock: threading.Lock = threading.Lock()

        self._html: bytes = _build_html().encode("utf-8")

    def start(self) -> None:
        """Start HTTP server, sim loop, MQTT, and block."""
        viz = self

        class Handler(BaseHTTPRequestHandler):
            """Serve the page at / and frames at /frame."""

            def do_GET(self) -> None:
                if self.path == "/frame":
                    with viz._frame_lock:
                        data: bytes = viz._latest_frame.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(viz._html)))
                    self.end_headers()
                    self.wfile.write(viz._html)

            def log_message(self, fmt: str, *args: Any) -> None:
                pass  # Suppress request logging.

        # Threaded HTTP server so multiple poll requests don't block.
        from socketserver import ThreadingMixIn

        class ThreadedHTTP(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        http_thread = threading.Thread(
            target=lambda: ThreadedHTTP(
                ("", self._http_port), Handler,
            ).serve_forever(),
            daemon=True,
        )
        http_thread.start()
        logger.info("HTTP: http://localhost:%d", self._http_port)

        # MQTT.
        self._connect_mqtt()
        if not self._mqtt_connected:
            logger.error("MQTT connection failed")
            return

        # Sim loop.
        sim_thread = threading.Thread(
            target=self._sim_loop, daemon=True,
        )
        sim_thread.start()

        logger.info(
            "Visualizer running — %d particles/note, %d fps",
            self._particles_per_note, self._sim_fps,
        )

        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(0.5)
        except KeyboardInterrupt:
            pass

        self._stop_event.set()
        self._disconnect_mqtt()
        logger.info("Visualizer stopped")

    def stop(self) -> None:
        self._stop_event.set()

    # -------------------------------------------------------------------
    # Sim loop
    # -------------------------------------------------------------------

    def _sim_loop(self) -> None:
        """Run physics and update the shared frame."""
        interval: float = 1.0 / self._sim_fps

        while not self._stop_event.is_set():
            t0: float = time.monotonic()

            with self._sim_lock:
                self._sim.step(DEFAULT_DT)
                frame_json: str = self._sim.get_frame_json()

            with self._frame_lock:
                self._latest_frame = frame_json

            elapsed: float = time.monotonic() - t0
            sleep: float = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    # -------------------------------------------------------------------
    # MIDI events
    # -------------------------------------------------------------------

    def _on_midi_event(self, event: dict) -> None:
        if event.get("event_type") != "note_on":
            return

        channel: int = event.get("channel", 0)
        note: int = event.get("note", 60)
        velocity: int = event.get("velocity", 100)

        frac: float = (note - MIDI_NOTE_LOW) / max(
            MIDI_NOTE_HIGH - MIDI_NOTE_LOW, 1)
        frac = max(0.0, min(1.0, frac))
        x: float = DOMAIN_MIN + frac * (DOMAIN_MAX - DOMAIN_MIN)
        vy: float = (velocity / 127.0) * 0.5

        with self._sim_lock:
            self._sim.spawn(
                self._particles_per_note, x, 0.0, 0.0, vy, channel,
            )

    # -------------------------------------------------------------------
    # MQTT
    # -------------------------------------------------------------------

    def _connect_mqtt(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError("pip install paho-mqtt")

        self._mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"glowup-nbody-viz-{int(time.time())}",
        )
        self._mqtt_client.on_connect = self._on_connect
        self._mqtt_client.on_message = self._on_message

        try:
            self._mqtt_client.connect(self._broker, self._mqtt_port)
            self._mqtt_client.loop_start()
            self._mqtt_connected = True
        except Exception as exc:
            logger.error("MQTT failed: %s", exc)

    def _disconnect_mqtt(self) -> None:
        if self._mqtt_client:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception:
                pass

    def _on_connect(self, client: Any, userdata: Any, flags: Any,
                    rc: Any, properties: Any = None) -> None:
        if rc == 0:
            topic: str = MQTT_SIGNAL_PREFIX + self._input_signal
            client.subscribe(topic, qos=MQTT_QOS)
            logger.info("Subscribed to %s", topic)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        try:
            self._on_midi_event(json.loads(msg.payload.decode()))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="GlowUp N-body Visualizer — MIDI → particles → WebGL",
    )
    parser.add_argument(
        "--particles-per-note", dest="ppn", type=int,
        default=DEFAULT_PARTICLES_PER_NOTE,
        help=f"Particles per note (default: {DEFAULT_PARTICLES_PER_NOTE})",
    )
    parser.add_argument(
        "--max-particles", dest="max_p", type=int,
        default=DEFAULT_MAX_PARTICLES,
        help=f"Max particles (default: {DEFAULT_MAX_PARTICLES})",
    )
    parser.add_argument(
        "--fps", type=int, default=DEFAULT_SIM_FPS,
        help=f"Sim rate (default: {DEFAULT_SIM_FPS})",
    )
    parser.add_argument(
        "--broker", default=DEFAULT_BROKER,
        help=f"MQTT broker (default: {DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--http-port", dest="http_port", type=int, default=DEFAULT_HTTP_PORT,
        help=f"HTTP port (default: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    viz = NBodyVisualizer(
        broker=args.broker,
        http_port=args.http_port,
        particles_per_note=args.ppn,
        max_particles=args.max_p,
        sim_fps=args.fps,
    )

    signal.signal(signal.SIGINT, lambda *_: viz.stop())
    signal.signal(signal.SIGTERM, lambda *_: viz.stop())

    viz.start()


if __name__ == "__main__":
    main()
