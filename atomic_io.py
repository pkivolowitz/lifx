"""Atomic file-write helpers.

Centralises the ``write to <target>.tmp`` → ``os.replace(tmp, target)``
pattern so the four-plus sites that persist JSON state files
(``groups.json``, ``schedules.json``, ``devices.json`` /
``device_registry.json``, server.json migrations, the STT state
file, the curated joke pool) all get the same crash-safety
guarantees from one place.

What "atomic" means here, and what it doesn't:

  ``os.replace`` is atomic on POSIX and on Windows for files on the
  same filesystem.  After a crash, an observer reading ``target``
  sees either the old contents or the new contents, never a half-
  written truncated file.  That is the only guarantee — if the host
  loses power before ``os.replace`` returns, the rename may be
  lost (the old file persists), but the file is never corrupt.
  Callers that need stronger durability must call ``os.fsync`` on
  the parent directory after the replace; this module does not
  attempt that because every current caller has accepted the
  rename-or-old semantics.

Crash-safety motivation: the 2026-05-02 audit found that
``handlers/dashboard.py`` was writing ``groups.json``,
``schedules.json``, and ``server.json`` via plain
``open(path, "w")``, which truncates first and then writes.  A
SIGKILL or power loss between truncate and write would leave the
state file empty or partial, and the server would refuse to start
on the next boot.  Callers shouldn't have to remember the tmp+rename
dance, so we put it here.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import json
import os
import tempfile
from typing import Any, Optional

# Suffix used for the temporary file beside the target.  Chosen so a
# stray temp file from a crashed write is obviously diagnosable
# (``cat foo.json.tmp``) and so wildcard cleanup scripts can find them.
_TMP_SUFFIX: str = ".tmp"


def write_atomic(
    target: str,
    data: bytes,
    *,
    mode: int = 0o644,
) -> None:
    """Write ``data`` to ``target`` atomically.

    Args:
        target: Destination path.
        data:   Bytes to write.
        mode:   POSIX file mode for the destination file (default
                ``0o644``).  Set explicitly because tempfile.mkstemp
                creates files with mode 0o600, which would silently
                tighten permissions on every save.

    The temporary file is created in the same directory as ``target``
    so ``os.replace`` is a same-filesystem rename (atomic).  On any
    error during write the temp file is removed; the destination is
    untouched.
    """
    parent: str = os.path.dirname(os.path.abspath(target)) or "."
    fd: int
    tmp_path: str
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(target) + ".",
        suffix=_TMP_SUFFIX,
        dir=parent,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except BaseException:
        # Catches KeyboardInterrupt too — we don't want a stale .tmp
        # cluttering the directory after a Ctrl-C between fdopen and
        # replace.  Re-raise to preserve the caller's contract.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(
    target: str,
    obj: Any,
    *,
    indent: Optional[int] = 4,
    sort_keys: bool = False,
    trailing_newline: bool = True,
    mode: int = 0o644,
) -> None:
    """JSON-encode ``obj`` and atomically write it to ``target``.

    Convenience wrapper around :func:`write_atomic` for the
    overwhelmingly common case of "persist this dict as pretty-printed
    JSON".

    Args:
        target:           Destination path.
        obj:              JSON-serializable Python object.
        indent:           ``json.dump`` ``indent``.  Default 4 to match
                          the existing handwritten state files.  Pass
                          ``None`` for compact output.
        sort_keys:        ``json.dump`` ``sort_keys``.  Default False
                          to preserve insertion order, matching the
                          legacy callers; set True (e.g.
                          ``device_registry``) when stable on-disk
                          ordering matters for git diffs.
        trailing_newline: Append ``"\\n"`` after the JSON.  Default
                          True — matches the existing files and keeps
                          POSIX text-file conventions.
        mode:             POSIX file mode for the destination
                          (default ``0o644``).

    Raises:
        OSError: If the temp file cannot be created or renamed.
        TypeError / ValueError: If ``obj`` is not JSON-serializable.
    """
    text: str = json.dumps(obj, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += "\n"
    write_atomic(target, text.encode("utf-8"), mode=mode)
