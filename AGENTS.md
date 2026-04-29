# GlowUp Agent Instructions

This file exists to make project-specific startup behavior cheap to
recover. If the user says `onboard` or `precompact`, do the exact steps
below instead of inventing a generic meaning.

## `onboard`

`onboard` means: execute the project startup checklist, recover shared
context, and report current state before doing other work.

### Discovery (handled by the SessionStart hook)

Machine identity, date, NAS mount, handoff presence, precompact
presence, and project memory index presence are reported automatically
by `~/NAS/.claude/bin/session-bootstrap.sh`, invoked via the
SessionStart hook in `~/.claude/settings.json`. The result appears as a
`SESSION BOOTSTRAP` block in initial context.

Do **not** re-run those checks with manual `ls` / `hostname` / `date`
calls. If the bootstrap block is missing (older session, hook
misconfigured), run the script manually:

    bash ~/NAS/.claude/bin/session-bootstrap.sh

### Post-discovery actions (Claude's responsibility)

- **Trust the bootstrap blob.** The SessionStart hook already inlines
  `identity.md`, `rules.md`, `MEMORY.md`, every `_precompact_*.md`, and
  any handoff content between `--- begin ---` / `--- end ---` markers.
  Do NOT re-Read files whose content is already in the blob — that
  doubles per-turn token cost for zero benefit.
- If the bootstrap block says **handoff PRESENT**: its body is embedded
  inline — internalize it, then archive to
  `~/NAS/.claude/handoff/archive/<from>_to_<machine>_<YYYY-MM-DD>.md`
- The `## precompact` section may contain MULTIPLE entries — one per
  machine that has compacted recently. Each entry is tagged with its
  owner. Internalize **every** entry; **delete only the entries marked
  `(own machine: <short-host>)`** (matching this machine), since the
  bootstrap has now folded their content back into the live session.
  Peer machines' entries are tagged `(peer machine: …)` — leave those
  files alone; the originating machine deletes its own on its next
  session start. Legacy unsuffixed `_precompact.md` is treated as
  belonging to this machine and deleted the same way (Compaction
  Protocol in rules.md).
- Pull latest branch state from `staging` before code changes — for
  **every repo the bootstrap reports**, not just `~/glowup`. The
  bootstrap inlines `## git` for the project repo and `## git_twin: …`
  for each path listed in
  `~/NAS/.claude/projects/-Users-perrykivolowitz-glowup/memory/_twin_repos.txt`
  (today: `~/glowup-infra`). Twins are pulled in lock step with the
  project repo; if any twin shows uncommitted edits, surface them to
  Perry **before** pulling, never silently stash. The 2026-04-29
  inventory.yaml event is the named precedent: stashing pre-existing
  uncommitted twin edits hid Perry's in-flight work behind a stash and
  forced him to ask "what are my edits?" to discover them.
- Read project memory files **only if they were NOT inlined** in the blob.
  Files commonly inlined by the bootstrap script:
  - `~/NAS/.claude/global/identity.md`
  - `~/NAS/.claude/global/rules.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-glowup/memory/MEMORY.md`
  If these appear in the blob, skip them. Only Read files the task
  actually requires (e.g., a specific project or reference memory).
- Inspect repo state (the bootstrap script does not touch git):
  - `git branch --show-current`
  - `git status --short --branch`
  - `git remote -v`
  - `git log --oneline --decorate -5`
- Report only:
  - machine
  - time/date
  - NAS status
  - handoff status
  - precompact status
  - branch/remote/worktree state
  - last known project context

Do not give a generic repo tour unless the user asks for one.

## On-demand: debugging lecture

`/Users/perrykivolowitz/glowup/docs/Discourses and Dialogs on Debugging.pdf`
is **not** part of the onboard sequence. It is ~37 image-rendered slides
and costs ~55K tokens to read. The core principles are already captured
in `feedback_debugging_methodology.md` (read at every onboard).

Read the PDF only when Perry explicitly tells you to — typically when
he sees you "going in circles" debugging and wants you to recalibrate
on the scientific method.

## `precompact`

`precompact` means: write a short ephemeral reload file capturing only
expensive-to-rediscover context for the next session.

Write to a **per-machine filename** so concurrent sessions on different
machines do not silently overwrite each other:

- `~/NAS/.claude/projects/-Users-perrykivolowitz-glowup/memory/_precompact_<short-host>.md`

`<short-host>` is the value reported in the SESSION BOOTSTRAP `## machine`
line (e.g. `Conway`, `Bed`, `Daedalus`).  The bootstrap inlines every
`_precompact_*.md` it finds at session start so cross-machine hive-mind
handoff still works — Bed sees Conway's tail-context and vice versa.

Lifecycle:

- A machine writes its **own** precompact at compact / shutdown time.
- On its **own** next session start, that machine internalizes the
  inlined precompact (now folded back into the live transcript) and
  **deletes its own file**.
- Peer machines' precompacts are inlined for context but **not deleted**;
  the originating machine cleans up on its own next session.

Content must be concise facts, not narrative:

- current task and branch
- exact repo/worktree state that matters
- non-obvious debugging discoveries
- architecture understanding gained this session
- deployment gotchas
- decisions made and why
- anything Perry cares about that is not in git

Rules:

- Tag memory writes with machine name and date
- Do not copy large logs
- Do not duplicate stable knowledge already in project memory
- Do not add any `_precompact_*.md` to `MEMORY.md`
- Legacy unsuffixed `_precompact.md` is still read by the bootstrap for
  back-compat; never *write* to that filename.

