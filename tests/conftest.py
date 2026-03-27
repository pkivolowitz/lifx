"""Pytest configuration for the GlowUp test suite.

Ensures the project root is on sys.path so that bare imports
(``from transport import ...``, ``from effects import ...``, etc.)
work regardless of which directory pytest is invoked from.
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
