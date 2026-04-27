# STT Stack — MLX-Whisper primary, faster-whisper fallback

Speech-to-text on Daedalus runs on a pluggable engine stack: MLX-Whisper
(Apple Silicon GPU via unified memory) as the primary, with faster-whisper
(CPU-only, int8) pre-warmed alongside as the fallback. The facade writes
`~/.glowup/stt_state.json` whenever the active engine changes so the
morning report on the hub can surface degraded state in red.

See also [35-voice-routing.md](35-voice-routing.md) for how the coordinator
ties STT into the broader voice pipeline.

## Why two engines

The faster-whisper stack worked but ran CPU-only on Apple Silicon —
CTranslate2's Metal backend requires a manual rebuild that is not in
the shipped wheels. MLX-Whisper runs the same Whisper weights through
Apple's MLX framework, exercising the M-series GPU at 2–3× the speed
at identical accuracy.

That said, MLX is macOS/arm64 only and is a newer dependency. Keeping
faster-whisper loaded as a pre-warmed fallback means a primary-load
failure (bad weights, mlx version skew, OS incompatibility) does not
take voice offline. Both engines share the 16 kHz PCM input contract,
so the fallback is a drop-in replacement with a quality/latency delta
the operator can tolerate while the root cause is fixed.

## Layout

```
voice/coordinator/
  stt.py                         # SpeechToText facade — engine selection,
                                 # pre-warm, state file, fallback logic
  stt_engines/
    __init__.py                  # exports
    base.py                      # STTEngine protocol, write_state(),
                                 # pcm_to_wav() helper, state file paths
    mlx_whisper.py               # MLXWhisperEngine (Apple Silicon GPU)
    faster_whisper.py            # FasterWhisperEngine (CPU, int8)
    mock.py                      # MockEngine — deterministic or prompt
```

The `MockEngine` is reachable via `MockSpeechToText` in
`voice/coordinator/stt.py` and kicks in when `mock_stt: true` is set in
`coordinator_config.json`. The daemon imports `SpeechToText` and
`MockSpeechToText` by name from `voice.coordinator.stt` — that surface
is kept stable across refactors.

## Configuration

In `~/coordinator_config.json` on Daedalus:

```json
{
  "stt": {
    "engine":          "mlx-whisper",
    "fallback_engine": "faster-whisper",
    "model":           "large-v3-turbo",
    "model_root":      "/Volumes/Mini-Dock/glowup/models",
    "language":        "en",
    "device":          "cpu",
    "compute_type":    "int8"
  }
}
```

| Key | Meaning | Default |
|-----|---------|---------|
| `engine` | Primary engine name | `mlx-whisper` |
| `fallback_engine` | Pre-warmed alongside the primary; loaded even when the primary succeeds so a later swap is latency-free. Set equal to `engine` to disable the fallback. | `faster-whisper` |
| `model` | Model name. The engine maps this to an HF repo ID internally. | `large-v3-turbo` |
| `model_root` | Local directory to search for pre-fetched weights before falling back to the HF cache. Convention: `<model_root>/<engine>/<model>/`. | empty (use HF cache only) |
| `language` | ISO-639-1 code. Whisper is multilingual but we pin to `en` for English voice control. | `en` |
| `device` / `compute_type` | faster-whisper specific. Ignored by MLX-Whisper. | `cpu`, `int8` |

Legacy `model_size` key is accepted as an alias for `model` for one
migration cycle.

## Model storage

Weights live in two places simultaneously, so the loss of either does
not take STT offline:

1. `<model_root>/<engine>/<model>/` on Mini-Dock
   (e.g. `/Volumes/Mini-Dock/glowup/models/mlx-whisper/large-v3-turbo/`)
2. `~/.cache/huggingface/hub/` on Daedalus.

Both copies are populated by `tools/fetch_stt_models.py`:

```
# On the coordinator host, as the operator user, with the coordinator venv active:
~/venv/bin/python ~/lifx/tools/fetch_stt_models.py
```

Default behaviour: both engines, `large-v3-turbo`, Mini-Dock target.
Re-running is cheap — `huggingface_hub.snapshot_download` skips
already-present files. See `--help` for `--engine` / `--model` / `--root`
overrides.

## Fallback behaviour (load-time)

At coordinator start, both the primary and fallback are constructed
and their `load()` methods are called. Each `load()`:

- MLX-Whisper: transcribes one second of silence to force the model
  into MLX's LRU cache and exercise the full audio path.
- faster-whisper: constructs `WhisperModel(...)` which loads the CT2
  graph into memory.
- `MockEngine`: no-op.

Outcomes:

| Primary load | Fallback load | Active engine | State file degraded flag |
|--------------|---------------|---------------|--------------------------|
| OK | OK | Primary | false |
| OK | fails | Primary | false (but coordinator logs the fallback failure) |
| fails | OK | Fallback | **true** — reason is the primary's error |
| fails | fails | — | **true** — raises, coordinator refuses to start |

When both fail, the exception propagates out of `SpeechToText.__init__`,
which means `com.glowup.coordinator` enters launchd's throttled restart
state. The state file reflects `engine: "none"` with both failure
reasons concatenated, so the operator can diagnose from the log.

## Fallback behaviour (runtime)

Runtime transcription failures (bad audio, numerical instability, etc.)
log and return an empty string — they do **not** flip the active
engine. Flipping on transient failures would cause thrashing; a hard
crash of the engine would surface as a load failure on the next
coordinator restart. If runtime-swap logic is needed later, it belongs
in the facade, not in individual engines.

## State file contract

`~/.glowup/stt_state.json`, atomic write (per-writer unique tmp + rename).
Each `write_state()` call creates its own `stt_state.json.<random>.tmp`
via `tempfile.mkstemp` in the same directory as the target, writes the
JSON there, then `os.replace`s it onto the final path. This is the
refit after the 2026-04-20 duplicate-launchd-instance race, where two
coordinators sharing a single `.json.tmp` name could see the second
writer's rename fail with `FileNotFoundError` — the target file would
end up with stale content and the morning report would flag Daedalus
red for what was actually a phantom bug. With per-writer tmp names the
race is impossible: every writer has its own exclusive inode, and the
final rename is atomic on POSIX even under concurrent contention.

```json
{
  "engine":          "mlx-whisper",
  "primary_engine":  "mlx-whisper",
  "fallback_reason": "",
  "since":           "2026-04-20T15:42:03+00:00"
}
```

Degraded = `engine != primary_engine` **or** `fallback_reason` is
non-empty. An external fleet-monitoring script (e.g. an operator's
private morning-report tool) can read this file over SSH and render
its own status display when degraded, with the reason text quoted
verbatim.

The file is overwritten on every facade construction — it reflects
the active state, not a transition log. A missing file is ambiguous
(coordinator may be down or on an older build), so the report renders
it as yellow "warn" rather than red "fail".

## Adding a new engine

1. Create `voice/coordinator/stt_engines/<your>_engine.py`.
2. Implement the `STTEngine` protocol from
   `voice/coordinator/stt_engines/base.py`: `name`, `is_available()`,
   `load()`, `transcribe(pcm, sample_rate) -> str`. `load()` should
   raise `STTEngineLoadError` with a useful message on any failure so
   the facade can fall back cleanly.
3. Export it from `voice/coordinator/stt_engines/__init__.py`.
4. Add a factory entry to `_ENGINE_FACTORIES` in
   `voice/coordinator/stt.py`.
5. Add a repo template to `_REPO_TEMPLATES` in
   `tools/fetch_stt_models.py` if the new engine has pre-fetchable
   weights.
6. Add tests to `voice/tests/test_stt_engines.py`.

## Operational runbook

**Verify current engine:**

```
ssh mortimer.snerd@192.0.2.10 cat ~/.glowup/stt_state.json
```

**Force a fallback for testing:**

Temporarily rename the MLX model directory so the primary fails to
load, then kickstart the coordinator:

```
ssh mortimer.snerd@192.0.2.10 mv \
  /Volumes/Mini-Dock/glowup/models/mlx-whisper \
  /Volumes/Mini-Dock/glowup/models/mlx-whisper.off
ssh mortimer.snerd@192.0.2.10 /usr/local/bin/restart_coordinator
```

Morning report the next day (or the current state file immediately)
will flag the degraded state. Put the directory back afterwards.

**Swap primary engine via config:**

Edit `~/coordinator_config.json` on Daedalus, swap
`engine` ↔ `fallback_engine`, kickstart. The state file updates on
the new load.
