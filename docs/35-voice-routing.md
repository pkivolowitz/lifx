# Voice Pipeline Routing

How utterance text and TTS audio flow between satellites, the
coordinator, and physical speakers.

## Components

| Role | Process | Host | Source |
|------|---------|------|--------|
| Satellite | `voice.satellite.daemon` | One per room (Pi) | `voice/satellite/daemon.py` |
| Coordinator | `voice.coordinator.daemon` | One per system (Daedalus) | `voice/coordinator/daemon.py` |
| Speaker daemon | `voice.speaker.daemon` | Optional, anywhere | `voice/speaker/daemon.py` |

A satellite captures audio, runs wake-word detection, captures the
utterance, and publishes the WAV bytes to MQTT. The coordinator
subscribes to all utterances, runs STT, intent parsing, action
execution, and decides what to say back. **How** that response gets
spoken depends on per-room speaker routing.

The STT stack itself (MLX-Whisper primary, faster-whisper fallback,
state reporting) is documented in [36-stt-stack.md](36-stt-stack.md).

## TTS Routing Modes

The coordinator's config has a `room_speakers` map. Each room has a
`speaker` mode:

| Mode | Meaning |
|------|---------|
| `local` | Coordinator speaks the response itself through its own audio output (e.g. Daedalus's Mac speakers via `say` or local Piper). The originating satellite is not involved in playback. |
| `satellite` | Coordinator publishes the response **text** back to MQTT topic `glowup/tts/text/<room>`. The satellite for that room receives the text, runs Piper locally, and plays through its own ALSA output. |

A third path exists historically: a satellite can also be configured
with `tts_output: "mqtt"` to bypass its own Piper and publish text to
the coordinator (or to a `voice.speaker.daemon`) for remote synthesis.
This is the original "Daedalus is mouthpiece" arrangement; it predates
per-satellite local Piper.

### When to use which

- **`satellite`** is the default for any room whose host has functional
  audio output. Latency is lower (no extra MQTT round trip), the
  coordinator does less work, and each room sounds the same regardless
  of how many rooms are active concurrently.
- **`local`** is the right choice when the satellite host has *no*
  speakers — historically, the dining-room satellite ran on Pi 5
  "glowup" with no audio out at all, so the coordinator on Daedalus
  spoke for it through Daedalus's speakers in the kitchen. With a
  USB audio device on glowup, this is no longer required.

## Per-Host Audio Output Quality

The Pi 4 onboard 3.5 mm jack is a PWM-driven cheap DAC. Even feeding
self-powered speakers it sounds tinny because the source has no real
low-frequency content. **For any Pi acting as a voice satellite, plan
on a USB audio device (sound card, USB speakerphone, or USB DAC).**
HDMI audio is acceptable if a display is already attached.

USB devices that have been verified working as both mic *and* speaker
on a Pi 5 (`glowup`):

- Jabra SPEAK 410 USB — verified, used as the dining-room satellite's
  full-duplex audio device

USB devices that did **not** work on Pi 4 (`mbclock`):

- Jabra SPEAK 410 USB — capture returned `read error: Input/output
  error` and the satellite's ALSA stream EOFed on first read on every
  USB port tried. Same unit works fine on macOS and on a Pi 5. Pi 4
  USB stack incompatibility, not a unit defect.

## ALSA Routing on Glowup (Dining Room)

The Pi 5 enumerates the Jabra as `card 2: USB [Jabra SPEAK 410 USB]`,
behind two HDMI playback cards (`card 0: vc4-hdmi-0`, `card 1:
vc4-hdmi-1`). Linux's default ALSA device on Pi 5 is the first
playback card — HDMI 0 — which is wrong for a satellite that wants to
speak through the Jabra.

**Fix:** `~/.asoundrc` on glowup overrides the system default:

```
pcm.!default {
    type plug
    slave.pcm "plughw:2,0"
}
ctl.!default {
    type hw
    card 2
}
```

The `plug` wrapper plus `plughw` slave is critical: it forces ALSA to
do automatic sample-rate conversion. The Jabra's native rate is
48 kHz; the satellite's Piper output is 22050 Hz; the cached "working"
audio cue was originally 16 kHz. Without the plug-on-plughw chain,
playing 22 kHz audio through `hw:2,0` either fails or comes out at the
wrong speed.

## Sample-Rate Gotcha: Cached Audio Cues

The "Star Trek computer working..." audio cue
(`~/models/tos_working.wav` on glowup) was originally 16 kHz mono.
That played fine through devices that accept 16 kHz natively, but the
Jabra returns garbage when given 16 kHz raw without resampling. The
cue file was resampled in place to 48 kHz with `sox`:

```
sox tos_working.wav -r 48000 tos_working_48k.wav
```

Original kept as `tos_working.wav.bak`. The same fix would apply to
any other cached audio cue files added in the future — match the
target device's native rate, or rely on ALSA `plug` resampling at
playback time.

## Reference: Coordinator config (`coordinator_config.json`)

Excerpt showing per-room routing as deployed on Daedalus:

```json
{
    "mqtt": {"broker": "10.0.0.214", "port": 1883},
    "room_speakers": {
        "Dining Room": {"speaker": "satellite"},
        "Main Bedroom": {"speaker": "satellite"}
    },
    "piper_model": "/Users/perrykivolowitz/models/en_US-ryan-low.onnx"
}
```

The coordinator's own `piper_model` is still loaded — it remains
available for any future room set to `local`, and for any
direct-publish path through the speaker daemon.

## Reference: Glowup satellite config (`/home/a/satellite_config.json`)

```json
{
    "room": "Dining Room",
    "mqtt": {"broker": "localhost", "port": 1883},
    "wake": {"model_path": "/home/a/models/hey_glowup.onnx"},
    "piper_model": "/home/a/models/en_US-ryan-medium.onnx"
}
```

Note absence of `tts_output` and `tts_topic` — they default to local
Piper synthesis. Note `piper_model` points at `ryan-medium`, which is
22050 Hz; the satellite reads the rate from the model's `.onnx.json`
companion file at startup.

## Restart Recipe

When changing routing configuration:

- Edit `coordinator_config.json` on Daedalus, then:
  `sudo launchctl kickstart -k system/com.glowup.coordinator`
- Edit `satellite_config.json` on the satellite host, then:
  `sudo systemctl restart glowup-satellite`

The satellite re-reads its config on every start. The coordinator does
the same. Neither hot-reloads.
