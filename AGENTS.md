# GlowUp Agent Instructions

This file exists to make project-specific startup behavior cheap to
recover. If the user says `onboard` or `precompact`, do the exact steps
below instead of inventing a generic meaning.

## `onboard`

`onboard` means: execute the project startup checklist, recover shared
context, and report current state before doing other work.

Required sequence:

- Run `scutil --get ComputerName`
- Run `hostname`
- Run `date`
- Verify NAS is mounted with `ls ~/NAS/.claude/global/`
- Read `~/NAS/.claude/global/identity.md`
- Read `~/NAS/.claude/global/rules.md`
- Read debugging lecture PDF:
  `/Users/perrykivolowitz/lifx/docs/Discourses and Dialogs on Debugging.pdf`
- Check handoff file for this machine at `~/NAS/.claude/handoff/<machine>.md`
  - If present: read it, internalize it, archive it per global rules
- Check precompact file:
  `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/_precompact.md`
  - If present: read it, internalize it, then delete it
- Pull latest branch state from `staging` before code changes
- Read project memory:
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/MEMORY.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/reference_project_state.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/reference_project_state_note.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/reference_network.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/reference_pi_infrastructure.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/reference_broker2.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/feedback_session_startup.md`
  - `~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/feedback_debugging_methodology.md`
- Inspect repo state:
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

## Notes

- Production server is Pi 5 `glowup` at `10.0.0.214`
- Deploy target is `./deploy.sh glowup`
- Primary git remote is `staging` over SSH to NAS
- GlowUp is a generalized SOE platform, not merely a lighting app
