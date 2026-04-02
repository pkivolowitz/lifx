"""Pytest configuration for voice subsystem tests.

Ensures the voice package and project root are on sys.path.
"""

import os
import sys

_VOICE_ROOT: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."),
)
_PROJECT_ROOT: str = os.path.abspath(
    os.path.join(_VOICE_ROOT, ".."),
)
for p in (_VOICE_ROOT, _PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
