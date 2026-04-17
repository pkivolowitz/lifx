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
  `identity.md`, `rules.md`, `MEMORY.md`, and any `_precompact.md` or
  handoff content between `--- begin ---` / `--- end ---` markers.
  Do NOT re-Read files whose content is already in the blob — that
  doubles per-turn token cost for zero benefit.
- If the bootstrap block says **handoff PRESENT**: its body is embedded
  inline — internalize it, then archive to
  `~/NAS/.claude/handoff/archive/<from>_to_<machine>_<YYYY-MM-DD>.md`
- If the bootstrap block says **precompact PRESENT**: content is inlined —
  internalize it, then DELETE the file (Compaction Protocol in rules.md)
- Pull latest branch state from `staging` before code changes
- Read project memory files **only if they were NOT inlined** in the blob.
  Files commonly inlined by the bootstrap script:
  - `~/NAS/.claude/global/identity.md`
  - `~/NAS/.claude/global/rules.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/MEMORY.md`
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

`/Users/perrykivolowitz/lifx/docs/Discourses and Dialogs on Debugging.pdf`
is **not** part of the onboard sequence. It is ~37 image-rendered slides
and costs ~55K tokens to read. The core principles are already captured
in `feedback_debugging_methodology.md` (read at every onboard).

Read the PDF only when Perry explicitly tells you to — typically when
he sees you "going in circles" debugging and wants you to recalibrate
on the scientific method.

## `precompact`

`precompact` means: write a short ephemeral reload file capturing only
expensive-to-rediscover context for the next session.

Write:

- `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/_precompact.md`

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
- Do not add `_precompact.md` to `MEMORY.md`

