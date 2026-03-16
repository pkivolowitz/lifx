"""WebGL particle emitter — browser-based visualization via WebSocket.

Subscribes to ``operator:nbody:frame`` on the MQTT bus and streams
particle frames to any connected browser via WebSocket.  Serves a
self-contained HTML/WebGL page that renders particles as colored
points.

Any machine with a browser becomes a display — no native rendering
code, no GPU drivers on the display machine.

Usage::

    python3 -m distributed.webgl_emitter
    # Open http://localhost:8421 in a browser

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import asyncio
import json
import logging
import signal
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.webgl_emitter")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (Pi).
DEFAULT_BROKER: str = "10.0.0.48"

# Default MQTT port.
DEFAULT_MQTT_PORT: int = 1883

# MQTT topic prefix.
MQTT_SIGNAL_PREFIX: str = "glowup/signals/"

# Input signal (N-body frames from the operator).
DEFAULT_INPUT_SIGNAL: str = "operator:nbody:frame"

# MQTT QoS.
MQTT_QOS: int = 0

# HTTP server port for the WebGL page.
DEFAULT_HTTP_PORT: int = 8421

# WebSocket port (separate from HTTP for simplicity).
DEFAULT_WS_PORT: int = 8422

# Channel colors — matches the MIDI light bridge palette.
# CSS hex colors indexed by MIDI channel (0-15).
CHANNEL_COLORS: list[str] = [
    "#FF0000",  # Ch 0  — Red
    "#0055FF",  # Ch 1  — Blue
    "#00AA00",  # Ch 2  — Green
    "#CC00DD",  # Ch 3  — Purple
    "#88CC00",  # Ch 4  — Yellow-green
    "#00CCCC",  # Ch 5  — Cyan
    "#FF6600",  # Ch 6  — Orange
    "#8800CC",  # Ch 7  — Violet
    "#CCCC00",  # Ch 8  — Yellow
    "#009999",  # Ch 9  — Teal
    "#CC8800",  # Ch 10 — Amber
    "#44BB44",  # Ch 11 — Spring green
    "#6600AA",  # Ch 12 — Indigo
    "#BBAA00",  # Ch 13 — Gold
    "#4488CC",  # Ch 14 — Sky blue
    "#DD0066",  # Ch 15 — Magenta
]


# ---------------------------------------------------------------------------
# HTML page with embedded WebGL
# ---------------------------------------------------------------------------

def _build_html(ws_port: int) -> str:
    """Generate the self-contained HTML/WebGL page.

    Args:
        ws_port: WebSocket port to connect to.

    Returns:
        Complete HTML string.
    """
    colors_js: str = json.dumps(CHANNEL_COLORS)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>GlowUp N-body Visualizer</title>
<style>
  body {{ margin: 0; background: #000; overflow: hidden; }}
  canvas {{ display: block; width: 100vw; height: 100vh; }}
  #stats {{
    position: fixed; top: 10px; left: 10px;
    color: #888; font: 14px monospace;
    pointer-events: none;
  }}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="stats"></div>
<script>
const COLORS = {colors_js};

// Convert hex colors to [r, g, b] floats.
const COLOR_RGB = COLORS.map(h => [
  parseInt(h.slice(1,3), 16) / 255,
  parseInt(h.slice(3,5), 16) / 255,
  parseInt(h.slice(5,7), 16) / 255,
]);

const canvas = document.getElementById('c');
const gl = canvas.getContext('webgl');
const stats = document.getElementById('stats');

// Resize canvas to window.
function resize() {{
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
  gl.viewport(0, 0, canvas.width, canvas.height);
}}
window.addEventListener('resize', resize);
resize();

// Vertex shader — positions are in [-1, 1] domain.
const vsSource = `
  attribute vec2 aPos;
  attribute vec3 aColor;
  varying vec3 vColor;
  void main() {{
    gl_Position = vec4(aPos, 0.0, 1.0);
    gl_PointSize = 3.0;
    vColor = aColor;
  }}
`;

// Fragment shader — colored points with soft edges.
const fsSource = `
  precision mediump float;
  varying vec3 vColor;
  void main() {{
    float d = length(gl_PointCoord - vec2(0.5));
    if (d > 0.5) discard;
    float alpha = 1.0 - smoothstep(0.3, 0.5, d);
    gl_FragColor = vec4(vColor, alpha);
  }}
`;

function compileShader(src, type) {{
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  return s;
}}

const vs = compileShader(vsSource, gl.VERTEX_SHADER);
const fs = compileShader(fsSource, gl.FRAGMENT_SHADER);
const prog = gl.createProgram();
gl.attachShader(prog, vs);
gl.attachShader(prog, fs);
gl.linkProgram(prog);
gl.useProgram(prog);

const aPos = gl.getAttribLocation(prog, 'aPos');
const aColor = gl.getAttribLocation(prog, 'aColor');

const posBuf = gl.createBuffer();
const colBuf = gl.createBuffer();

gl.enable(gl.BLEND);
gl.blendFunc(gl.SRC_ALPHA, gl.ONE);

let currentFrame = null;
let frameCount = 0;

function render() {{
  gl.clearColor(0.0, 0.0, 0.0, 1.0);
  gl.clear(gl.COLOR_BUFFER_BIT);

  if (currentFrame && currentFrame.particles > 0) {{
    const n = currentFrame.particles;
    const x = currentFrame.x;
    const y = currentFrame.y;
    const c = currentFrame.color;

    // Build position array.
    const posData = new Float32Array(n * 2);
    const colData = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {{
      posData[i*2] = x[i];
      posData[i*2+1] = y[i];
      const rgb = COLOR_RGB[c[i] % COLOR_RGB.length];
      colData[i*3] = rgb[0];
      colData[i*3+1] = rgb[1];
      colData[i*3+2] = rgb[2];
    }}

    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
    gl.bufferData(gl.ARRAY_BUFFER, posData, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    gl.bindBuffer(gl.ARRAY_BUFFER, colBuf);
    gl.bufferData(gl.ARRAY_BUFFER, colData, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(aColor);
    gl.vertexAttribPointer(aColor, 3, gl.FLOAT, false, 0, 0);

    gl.drawArrays(gl.POINTS, 0, n);
  }}

  frameCount++;
  requestAnimationFrame(render);
}}
requestAnimationFrame(render);

// WebSocket connection.
let ws = null;
let wsFrames = 0;

function connectWS() {{
  ws = new WebSocket('ws://' + location.hostname + ':{ws_port}');
  ws.onmessage = (evt) => {{
    try {{
      currentFrame = JSON.parse(evt.data);
      wsFrames++;
      if (wsFrames % 30 === 0) {{
        stats.textContent =
          'particles: ' + (currentFrame.total_active || 0) +
          '  step: ' + (currentFrame.step || 0) +
          '  compute: ' + (currentFrame.step_ms || 0) + ' ms';
      }}
    }} catch(e) {{}}
  }};
  ws.onclose = () => setTimeout(connectWS, 1000);
  ws.onerror = () => ws.close();
}}
connectWS();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# WebSocket server (asyncio-based, stdlib only)
# ---------------------------------------------------------------------------

class WebGLEmitter:
    """Serve a WebGL page and stream N-body frames via WebSocket.

    The HTTP server serves the self-contained HTML page.  The
    WebSocket server streams particle frames to all connected
    browsers.

    Args:
        broker:       MQTT broker host.
        mqtt_port:    MQTT broker port.
        input_signal: Signal to subscribe to.
        http_port:    HTTP server port.
        ws_port:      WebSocket server port.
    """

    def __init__(self, broker: str = DEFAULT_BROKER,
                 mqtt_port: int = DEFAULT_MQTT_PORT,
                 input_signal: str = DEFAULT_INPUT_SIGNAL,
                 http_port: int = DEFAULT_HTTP_PORT,
                 ws_port: int = DEFAULT_WS_PORT) -> None:
        """Initialize the WebGL emitter.

        Args:
            broker:       MQTT broker host.
            mqtt_port:    MQTT broker port.
            input_signal: Input signal name.
            http_port:    HTTP port for the page.
            ws_port:      WebSocket port for frames.
        """
        self._broker: str = broker
        self._mqtt_port: int = mqtt_port
        self._input_signal: str = input_signal
        self._http_port: int = http_port
        self._ws_port: int = ws_port

        self._mqtt_client: Optional[Any] = None
        self._mqtt_connected: bool = False
        self._stop_event: threading.Event = threading.Event()

        # Connected WebSocket clients.
        self._ws_clients: set = set()
        self._ws_lock: threading.Lock = threading.Lock()

        # Latest frame for immediate delivery to new clients.
        self._latest_frame: Optional[str] = None

        # HTML page content.
        self._html: str = _build_html(ws_port)

    def start(self) -> None:
        """Start HTTP server, WebSocket server, and MQTT subscriber."""
        # Start HTTP server in a thread.
        http_thread: threading.Thread = threading.Thread(
            target=self._run_http,
            daemon=True,
            name="webgl-http",
        )
        http_thread.start()
        logger.info("HTTP server: http://localhost:%d", self._http_port)

        # Start WebSocket server in a thread.
        ws_thread: threading.Thread = threading.Thread(
            target=self._run_ws,
            daemon=True,
            name="webgl-ws",
        )
        ws_thread.start()
        logger.info("WebSocket server: ws://localhost:%d", self._ws_port)

        # Connect to MQTT.
        self._connect_mqtt()
        if not self._mqtt_connected:
            logger.error("Failed to connect to MQTT broker")
            return

        logger.info("WebGL emitter running — open http://localhost:%d",
                     self._http_port)

        # Block until stopped.
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(0.5)
        except KeyboardInterrupt:
            pass

        self._shutdown()

    def stop(self) -> None:
        """Signal the emitter to stop."""
        self._stop_event.set()

    def _shutdown(self) -> None:
        """Clean shutdown."""
        self._stop_event.set()
        self._disconnect_mqtt()
        logger.info("WebGL emitter stopped")

    # -------------------------------------------------------------------
    # HTTP server
    # -------------------------------------------------------------------

    def _run_http(self) -> None:
        """Run the HTTP server serving the WebGL page."""
        emitter = self

        class Handler(SimpleHTTPRequestHandler):
            """Serve the embedded HTML page."""

            def do_GET(self) -> None:
                """Serve the WebGL page for any path."""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                content: bytes = emitter._html.encode("utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: Any) -> None:
                """Suppress default HTTP logging."""
                pass

        server: HTTPServer = HTTPServer(("", self._http_port), Handler)
        server.serve_forever()

    # -------------------------------------------------------------------
    # WebSocket server (raw protocol, no external deps)
    # -------------------------------------------------------------------

    def _run_ws(self) -> None:
        """Run the WebSocket server using asyncio."""
        loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_serve())

    async def _ws_serve(self) -> None:
        """Accept WebSocket connections and keep them alive."""
        server = await asyncio.start_server(
            self._ws_handle_client, "", self._ws_port,
        )
        async with server:
            # Run until stop event is set.
            while not self._stop_event.is_set():
                await asyncio.sleep(0.1)

    async def _ws_handle_client(self, reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter) -> None:
        """Handle a single WebSocket client connection.

        Performs the HTTP upgrade handshake, then keeps the
        connection alive for frame broadcasting.

        Args:
            reader: Stream reader.
            writer: Stream writer.
        """
        import hashlib
        import base64

        # Read the HTTP upgrade request.
        request: bytes = b""
        while not request.endswith(b"\r\n\r\n"):
            chunk: bytes = await reader.read(4096)
            if not chunk:
                return
            request += chunk

        # Extract the Sec-WebSocket-Key.
        key: str = ""
        for line in request.decode("utf-8", errors="replace").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
                break

        if not key:
            writer.close()
            return

        # Compute accept key.
        WS_MAGIC: str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept: str = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC).encode()).digest()
        ).decode()

        # Send upgrade response.
        response: str = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        writer.write(response.encode())
        await writer.drain()

        # Register the client.
        with self._ws_lock:
            self._ws_clients.add(writer)

        logger.info("WebSocket client connected (%d total)",
                     len(self._ws_clients))

        # Keep alive — read pings/closes.
        try:
            while not self._stop_event.is_set():
                data: bytes = await asyncio.wait_for(
                    reader.read(1024), timeout=1.0,
                )
                if not data:
                    break
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            with self._ws_lock:
                self._ws_clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass
            logger.info("WebSocket client disconnected (%d remaining)",
                         len(self._ws_clients))

    def _broadcast_ws(self, payload: str) -> None:
        """Send a WebSocket text frame to all connected clients.

        Args:
            payload: JSON string to send.
        """
        data: bytes = payload.encode("utf-8")

        # Build WebSocket text frame.
        frame: bytes
        length: int = len(data)
        if length < 126:
            frame = bytes([0x81, length]) + data
        elif length < 65536:
            frame = bytes([0x81, 126]) + length.to_bytes(2, "big") + data
        else:
            frame = bytes([0x81, 127]) + length.to_bytes(8, "big") + data

        with self._ws_lock:
            dead: list = []
            for writer in self._ws_clients:
                try:
                    writer.write(frame)
                except Exception:
                    dead.append(writer)
            for w in dead:
                self._ws_clients.discard(w)

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

        client_id: str = f"glowup-webgl-{int(time.time())}"
        self._mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        self._mqtt_client.on_connect = self._on_connect
        self._mqtt_client.on_disconnect = self._on_disconnect
        self._mqtt_client.on_message = self._on_message

        try:
            self._mqtt_client.connect(self._broker, self._mqtt_port)
            self._mqtt_client.loop_start()
            self._mqtt_connected = True
        except Exception as exc:
            logger.error("MQTT connect failed: %s", exc)
            self._mqtt_connected = False

    def _disconnect_mqtt(self) -> None:
        """Disconnect from MQTT."""
        if self._mqtt_client:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None
            self._mqtt_connected = False

    def _on_connect(self, client: Any, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        """Subscribe to N-body frames on connect."""
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
            logger.warning("MQTT disconnected (rc=%s)", reason_code)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Forward N-body frames to WebSocket clients."""
        payload: str = msg.payload.decode("utf-8")
        self._latest_frame = payload
        self._broadcast_ws(payload)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for the WebGL emitter."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "GlowUp WebGL Emitter — browser-based N-body visualization. "
            "Subscribes to particle frames and streams to WebGL via WebSocket."
        ),
    )
    parser.add_argument(
        "--broker", default=DEFAULT_BROKER,
        help=f"MQTT broker host (default: {DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--mqtt-port", dest="mqtt_port", type=int, default=DEFAULT_MQTT_PORT,
        help=f"MQTT broker port (default: {DEFAULT_MQTT_PORT})",
    )
    parser.add_argument(
        "--input-signal", dest="input_signal",
        default=DEFAULT_INPUT_SIGNAL,
        help=f"Input signal name (default: '{DEFAULT_INPUT_SIGNAL}')",
    )
    parser.add_argument(
        "--http-port", dest="http_port", type=int, default=DEFAULT_HTTP_PORT,
        help=f"HTTP server port (default: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--ws-port", dest="ws_port", type=int, default=DEFAULT_WS_PORT,
        help=f"WebSocket port (default: {DEFAULT_WS_PORT})",
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

    emitter: WebGLEmitter = WebGLEmitter(
        broker=args.broker,
        mqtt_port=args.mqtt_port,
        input_signal=args.input_signal,
        http_port=args.http_port,
        ws_port=args.ws_port,
    )

    # Handle Ctrl+C.
    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        emitter.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    emitter.start()


if __name__ == "__main__":
    main()
