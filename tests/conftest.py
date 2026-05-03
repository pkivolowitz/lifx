"""Pytest configuration for the GlowUp test suite.

Two responsibilities, both of which must run before any project
modules import:

* Insert the project root onto ``sys.path`` so bare imports
  (``from transport import ...``, ``from effects import ...``, etc.)
  resolve regardless of where pytest was invoked from.

* Point ``GLOWUP_SITE_JSON`` at ``tests/_test_site.json`` so the
  ``glowup_site`` module's import-time singleton gets the fixture
  values instead of failing on missing fleet config.  This matters
  on dev machines (Conway, Bed) and CI hosts that don't ship a
  ``/etc/glowup/site.json``: post the 2026-05-02 public-release
  cutover, the voice executor and several other modules require
  ``latitude``, ``longitude``, and ``zigbee_service_url`` at module
  import — without this fixture every test that touches them
  raises ``SiteConfigError`` at collection.

  If the environment already has ``GLOWUP_SITE_JSON`` set (e.g. an
  operator running tests against their own real fixture), that
  value wins — we never overwrite an explicit caller choice.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

import os
import sys

# Insert the project root (one level up from tests/) at the front
# of sys.path so all bare imports resolve correctly.
_PROJECT_ROOT: str = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Site-config fixture.  Set BEFORE any project import so the
# glowup_site module-level singleton sees it.
_TEST_SITE_JSON: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "_test_site.json"),
)
os.environ.setdefault("GLOWUP_SITE_JSON", _TEST_SITE_JSON)
