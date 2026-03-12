# LIFX Effect Engine

Modular effect engine for LIFX devices. See README.md for overview, MANUAL.md for full docs.

## NAS Memory Check
At the start of every conversation, verify the NAS is mounted:
```
ls /Volumes/perryk/.claude/projects/-Users-perrykivolowitz-lifx/memory/MEMORY.md
```
If the file is missing, tell Perry: "The NAS at /Volumes/perryk is not mounted — shared memory is unavailable. Mount it before we proceed so I have full context."

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
