# LIFX Effect Engine

Modular effect engine for LIFX devices. See README.md for overview, MANUAL.md for full docs.

## Bootstrap (start of every conversation)
1. Identify the current machine:
```
scutil --get ComputerName 2>/dev/null || hostname -s 2>/dev/null || hostname
```
Report the machine name to Perry (e.g. "Running on **Bed**.").

2. Verify the NAS is mounted:
```
ls ~/NAS/.claude/projects/-Users-perrykivolowitz-lifx/memory/MEMORY.md
```
If the file is missing, tell Perry: "The NAS at ~/NAS is not mounted. Run: `mount_smbfs //perryk@10.0.0.24/perryk ~/NAS` — shared memory is unavailable until it's mounted."

## Code Standards
- PEP 257 docstrings on all public classes, methods, and functions
- Explanatory inline comments
- Type hints on all function signatures and variables
- Version strings (`__version__`) in every module
- No magic numbers — each file to contain a constants section
- Honor the 3-level help system
- py_compile each module before testing
- Expansive commit messages; every high-level change gets its own commit
- Code to be bullet and idiot proofed
- Push back on bad ideas — notice questionable practices and suggest improvement
- Do the **right** thing not the expedient thing — technical debt to be avoided
