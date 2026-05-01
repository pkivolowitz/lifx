"""Purgatory — effects whose concept is sound but not viable on current hardware.

Modules in this subpackage are deliberately NOT imported by the
auto-discovery loop in :mod:`effects.__init__` (which only iterates
the top-level ``effects/`` directory).  They stay on disk so future
hardware (higher-resolution matrix fixtures) can revive them with
minimal work — typically just moving the file back up one level and
updating the relative imports — but they don't appear in
``glowup.py effects``, can't be ``play``-dispatched, and don't get
registered with the effect metaclass.

Why an effect lands here:

- ``emoji_slideshow`` (2026-05-01): 8x8 features-only bitmaps don't
  read as recognisable faces at the SuperColor Ceiling's pixel
  density.  Concept (slideshow with dissolve / wipe / slide
  transitions) is sound; resolution is the limiter.  Revisit if a
  16x16 or larger matrix fixture lands.

- ``morph_shapes`` (2026-05-01): geometric shapes (hline/vline/
  slash/bslash/square/diamond/circle/asterisk) shrinking to a
  centre point and expanding into a new shape.  Same resolution
  ceiling — at 8x8 the 1-cell-thick outlines collapse to a few
  pixels and several shapes become indistinguishable from each
  other.  Test file at ``tests/boneyard/test_morph_shapes.py``;
  ``--ignore=tests/boneyard`` keeps it out of the active suite.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"
