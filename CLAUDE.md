# LIFX Effect Engine

Modular effect engine for LIFX devices. See README.md for overview, docs/MANUAL.md for full docs.

## Session Start
Do the full startup checklist from [AGENTS.md](/Users/perrykivolowitz/glowup/AGENTS.md).

If the user says `onboard`, that means: execute the project startup
checklist from `AGENTS.md`, recover shared context, inspect git state,
and report status. It does not mean "give a generic repo overview."

If the user says `precompact`, that means: write the ephemeral reload
file described in `AGENTS.md` to a **per-machine** filename:

`~/NAS/.claude/projects/-Users-perrykivolowitz-glowup/memory/_precompact_<short-host>.md`

`<short-host>` comes from the SESSION BOOTSTRAP `## machine` line
(`Conway`, `Bed`, `Daedalus`, etc.). Per-machine filenames stop two
sessions on different machines from silently overwriting each other's
precompacts; see AGENTS.md for the full lifecycle.

Compaction context path (one entry per machine):
`~/NAS/.claude/projects/-Users-perrykivolowitz-glowup/memory/_precompact_<short-host>.md`

Legacy `_precompact.md` (no host suffix) is still recognized by the
bootstrap for back-compat — never *write* to that filename.

## Code Standards
See `~/NAS/.claude/global/rules.md` — Coding Standards section. All sessions follow those standards.
