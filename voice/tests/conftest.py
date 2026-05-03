"""Pytest configuration for voice subsystem tests.

Two responsibilities, both of which must run before any voice
module is imported:

* Put the voice package and project root on ``sys.path`` so bare
  ``voice.coordinator.*`` imports resolve regardless of pytest
  invocation directory.

* Point ``GLOWUP_SITE_JSON`` at the project-level
  ``tests/_test_site.json`` so the ``glowup_site`` module-level
  singleton initializes with fixture values.  Post the
  2026-05-02 public-release cutover, ``voice.coordinator.executor``
  reads ``site.latitude``, ``site.longitude``, and
  ``zigbee_service_url`` at module / __init__ time; without this
  fixture every executor test fails at construction with
  ``SiteConfigError``.  Mirrors the project-level
  ``tests/conftest.py`` so ``pytest voice/tests/`` works on its
  own (without depending on ``tests/conftest.py`` being loaded).

  An explicit caller-set ``GLOWUP_SITE_JSON`` wins.
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

_TEST_SITE_JSON: str = os.path.join(
    _PROJECT_ROOT, "tests", "_test_site.json",
)
os.environ.setdefault("GLOWUP_SITE_JSON", _TEST_SITE_JSON)
