-- MIDI events table — structured storage for MIDI sensor data.
--
-- Stores individual MIDI events (note on/off, CC, program change, etc.)
-- as structured rows, NOT raw .mid blobs.  This enables queries like:
--
--   SELECT * FROM midi_events WHERE note = 60 AND event_type = 'note_on';
--   SELECT source_file, count(*) FROM midi_events GROUP BY source_file;
--   SELECT DISTINCT tempo_bpm FROM midi_events WHERE event_type = 'set_tempo';
--
-- Populated by the persistence emitter subscribing to sensor:midi:events,
-- or by bulk ingest (replay --speed 0).
--
-- Connection: postgresql://glowup:glowup@10.0.0.42:5432/glowup

CREATE TABLE IF NOT EXISTS midi_events (
    id           BIGSERIAL    PRIMARY KEY,

    -- Source identification.
    source_file  TEXT         NOT NULL,
    track        SMALLINT     NOT NULL DEFAULT 0,

    -- Timing.
    tick         INTEGER      NOT NULL DEFAULT 0,
    time_s       DOUBLE PRECISION NOT NULL DEFAULT 0.0,

    -- Event classification.
    event_type   TEXT         NOT NULL,
    channel      SMALLINT     DEFAULT -1,

    -- Note events (note_on, note_off).
    note         SMALLINT     DEFAULT -1,
    velocity     SMALLINT     DEFAULT -1,

    -- Control change events.
    cc_number    SMALLINT     DEFAULT -1,
    cc_value     SMALLINT     DEFAULT -1,

    -- Program change.
    program      SMALLINT     DEFAULT -1,

    -- Pitch bend (14-bit, 0-16383, center=8192).
    pitch_bend   INTEGER      DEFAULT -1,

    -- Aftertouch / channel pressure.
    pressure     SMALLINT     DEFAULT -1,

    -- Meta events.
    meta_type    SMALLINT     DEFAULT -1,
    meta_value   TEXT,

    -- Tempo (set_tempo events only).
    tempo_bpm    DOUBLE PRECISION DEFAULT -1.0,

    -- Ingest timestamp.
    ingested_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Index for common queries.
CREATE INDEX IF NOT EXISTS idx_midi_events_source
    ON midi_events (source_file);

CREATE INDEX IF NOT EXISTS idx_midi_events_type
    ON midi_events (event_type);

CREATE INDEX IF NOT EXISTS idx_midi_events_note
    ON midi_events (note)
    WHERE note >= 0;

CREATE INDEX IF NOT EXISTS idx_midi_events_time
    ON midi_events (time_s);

COMMENT ON TABLE midi_events IS
    'Structured MIDI event storage — one row per event, queryable by note/channel/type/time.';
