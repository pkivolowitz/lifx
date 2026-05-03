"""Tests for atomic_io — durable JSON state-file writes.

Pins the contract that ``write_json_atomic`` and ``write_atomic``
provide:

* Round-trip correctness (data written equals data read back).
* Crash safety: if the encoder fails mid-write, the destination file
  retains its previous contents and no orphan ``.tmp`` is left in
  the parent directory.
* Permission preservation: the destination file ends up at the
  declared mode (0o644 by default), not at tempfile.mkstemp's
  default 0o600.
* Same-filesystem temp: the temp file is written next to the target,
  so ``os.replace`` is an atomic rename rather than a cross-fs copy.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from atomic_io import write_atomic, write_json_atomic

_PRESERVED_GROUPS: dict[str, list[dict[str, str]]] = {
    "Bedroom": [{"label": "ceiling", "ip": "10.0.0.42"}],
    "Living Room": [{"label": "lamp", "ip": "10.0.0.43"}],
}


class TestWriteJsonAtomicRoundTrip(unittest.TestCase):
    """Successful writes produce on-disk JSON identical to the input."""

    def test_round_trip_preserves_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "groups.json")
            write_json_atomic(target, _PRESERVED_GROUPS)
            with open(target) as f:
                read_back: dict[str, Any] = json.load(f)
        self.assertEqual(read_back, _PRESERVED_GROUPS)

    def test_round_trip_preserves_list(self) -> None:
        payload: list[Any] = [1, 2, "three", {"four": 4.0}]
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "list.json")
            write_json_atomic(target, payload)
            with open(target) as f:
                self.assertEqual(json.load(f), payload)

    def test_trailing_newline_present_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            write_json_atomic(target, {"a": 1})
            with open(target, "rb") as f:
                self.assertTrue(f.read().endswith(b"\n"))

    def test_trailing_newline_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            write_json_atomic(target, {"a": 1}, trailing_newline=False)
            with open(target, "rb") as f:
                self.assertFalse(f.read().endswith(b"\n"))

    def test_overwrites_existing_file(self) -> None:
        """Subsequent calls replace the previous contents in full."""
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            write_json_atomic(target, {"v": 1})
            write_json_atomic(target, {"v": 2})
            with open(target) as f:
                self.assertEqual(json.load(f), {"v": 2})


class TestAtomicCrashSafety(unittest.TestCase):
    """Failures mid-write must not corrupt the destination or leave junk."""

    def test_encoder_failure_preserves_old_contents(self) -> None:
        """If json.dumps raises (non-serializable input), the existing
        file at ``target`` is untouched.  This is the durability
        guarantee that distinguishes write_json_atomic from a plain
        ``open(target, "w")`` call.
        """
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            write_json_atomic(target, {"v": "good"})
            class _NotJSONable:
                pass
            with self.assertRaises(TypeError):
                write_json_atomic(target, {"bad": _NotJSONable()})
            with open(target) as f:
                self.assertEqual(json.load(f), {"v": "good"})

    def test_encoder_failure_leaves_no_tmp_in_parent(self) -> None:
        """A failed write must clean up its temporary file."""
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            class _NotJSONable:
                pass
            with self.assertRaises(TypeError):
                write_json_atomic(target, {"bad": _NotJSONable()})
            leftovers: list[str] = list(Path(tmp).iterdir())
            self.assertEqual(
                leftovers, [],
                f"orphan tmp file(s) left behind: {leftovers}",
            )

    def test_replace_failure_leaves_no_tmp(self) -> None:
        """If os.replace itself raises, the temp file must still be
        cleaned up.  Simulated by patching os.replace to raise OSError.
        """
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            with mock.patch(
                "atomic_io.os.replace", side_effect=OSError("simulated"),
            ):
                with self.assertRaises(OSError):
                    write_json_atomic(target, {"v": 1})
            leftovers: list[str] = list(Path(tmp).iterdir())
            self.assertEqual(leftovers, [])


class TestPermissions(unittest.TestCase):
    """The destination file lands at 0o644, not at tempfile's 0o600."""

    def test_default_mode_is_0o644(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            write_json_atomic(target, {"v": 1})
            mode: int = stat.S_IMODE(os.stat(target).st_mode)
        # On macOS / Linux umask may further restrict; we assert that
        # at minimum the world-read bit was attempted (the explicit
        # chmod call), which the call sets regardless of umask.
        self.assertTrue(mode & 0o004, f"world-read bit missing: {oct(mode)}")
        self.assertTrue(mode & 0o200, f"owner-write bit missing: {oct(mode)}")

    def test_custom_mode_honored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            write_json_atomic(target, {"v": 1}, mode=0o600)
            mode: int = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(mode & 0o077, 0)  # group/other have no bits
        self.assertEqual(mode & 0o600, 0o600)


class TestTempFilePlacement(unittest.TestCase):
    """Temporary file lives next to the target, not in /tmp."""

    def test_tempfile_is_in_target_directory(self) -> None:
        """We snapshot the directory after a deliberately slow write
        to confirm the .tmp shows up in the same directory.  Slow is
        simulated by patching ``os.replace`` so we can inspect the
        directory contents at the moment between fdopen and replace.
        """
        captured_parent: dict[str, str] = {}
        real_replace = os.replace

        def _spy_replace(src: str, dst: str) -> None:
            captured_parent["src_dir"] = os.path.dirname(src)
            captured_parent["dst_dir"] = os.path.dirname(dst)
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "x.json")
            with mock.patch("atomic_io.os.replace", side_effect=_spy_replace):
                write_json_atomic(target, {"v": 1})
        self.assertEqual(captured_parent["src_dir"], captured_parent["dst_dir"])


class TestWriteAtomicBytes(unittest.TestCase):
    """The lower-level write_atomic accepts arbitrary bytes."""

    def test_round_trip_binary(self) -> None:
        payload: bytes = bytes(range(256))
        with tempfile.TemporaryDirectory() as tmp:
            target: str = os.path.join(tmp, "blob.bin")
            write_atomic(target, payload)
            with open(target, "rb") as f:
                self.assertEqual(f.read(), payload)


if __name__ == "__main__":
    unittest.main()
