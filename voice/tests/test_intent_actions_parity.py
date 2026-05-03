"""Parity check: intent.py system prompt ↔ actions.yml.

The LLM intent classifier is steered by a hardcoded action menu in
``voice/coordinator/intent.py:_SYSTEM_PROMPT_TEMPLATE``.  The executor
dispatches against ``voice/coordinator/actions.yml``.  When an action is
added to one and not the other the failure mode is silent: the LLM
emits a JSON intent that the executor either falls through to ``chat``
(noisy and confusing) or rejects with a generic error.

This test is the contract: every action named in the prompt must be
backed by ``actions.yml`` (or live on the small explicit exception list
of pipeline-intercepted actions), and every action defined in
``actions.yml`` must be advertised in the prompt — otherwise the
classifier will never select it.

Background: 2026-05-02, the public-release Phase 1+2 cutover plus a
speaker-bleed bug exposed how easy it is to drift these two surfaces.
This file's job is to prevent the next one.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import re
import unittest
from pathlib import Path
from typing import Any

import yaml

from voice.coordinator import intent as intent_mod

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Pattern that matches the per-action bullet header inside the prompt
# template.  Format established by intent.py: each action is introduced
# by ``- name: description`` at the start of a line.  Anchored to start
# of line so substrings inside descriptions don't match.
_ACTION_BULLET_RE: re.Pattern[str] = re.compile(
    r"^- ([a-z_][a-z0-9_]*):\s",
    re.MULTILINE,
)

# Actions that intentionally appear in the prompt but NOT in actions.yml
# because they are intercepted by the pipeline (or pre-pipeline) layer
# before the executor ever sees them.  Any new addition here needs a
# code reference proving the interception.
_PROMPT_ONLY_ACTIONS: frozenset[str] = frozenset({
    # Pipeline (voice/coordinator/pipeline.py:289-302) detects "flush"
    # transcripts and runs an epoch bump + cancellation path before the
    # intent classifier runs at all.  The prompt still mentions it so a
    # nearly-flush utterance ("cancel that") that slips past the literal
    # match is not misrouted to chat.
    "flush",
})

# actions.yml has top-level keys that aren't actions (config-only), e.g.
# the ``plugs`` table.  These are NOT expected to appear in the prompt.
_ACTIONS_YAML_NON_ACTION_KEYS: frozenset[str] = frozenset({"plugs"})

_ACTIONS_YAML_PATH: Path = (
    Path(intent_mod.__file__).parent / "actions.yml"
)


def _prompt_actions() -> set[str]:
    """Return the set of action names declared in the prompt template."""
    return set(_ACTION_BULLET_RE.findall(intent_mod._SYSTEM_PROMPT_TEMPLATE))


def _yaml_actions() -> set[str]:
    """Return the set of action names defined in actions.yml."""
    with open(_ACTIONS_YAML_PATH) as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    return {
        k for k in data.keys()
        if k not in _ACTIONS_YAML_NON_ACTION_KEYS
    }


class TestIntentActionsParity(unittest.TestCase):
    """Cross-check the prompt action menu against actions.yml."""

    def test_every_prompt_action_has_a_yaml_action_or_is_allowlisted(
        self,
    ) -> None:
        """Every action advertised to the LLM must be dispatchable.

        The executor's silent fall-through to ``chat`` for unknown
        actions (executor.py around line 753) hides this kind of drift
        from logs, so the contract is enforced here at test time
        instead.
        """
        prompt: set[str] = _prompt_actions()
        yaml_actions: set[str] = _yaml_actions()
        unknown: set[str] = prompt - yaml_actions - _PROMPT_ONLY_ACTIONS
        self.assertEqual(
            unknown, set(),
            "Actions named in intent.py prompt have no actions.yml "
            "entry and aren't on the pipeline-intercept allowlist: "
            f"{sorted(unknown)}.  Either add them to actions.yml, "
            "remove them from the prompt, or extend "
            "_PROMPT_ONLY_ACTIONS in this test (and document the "
            "pipeline interception).",
        )

    def test_every_yaml_action_is_advertised_in_prompt(self) -> None:
        """An action defined in YAML but missing from the prompt is
        unreachable — the classifier will never pick it.
        """
        prompt: set[str] = _prompt_actions()
        yaml_actions: set[str] = _yaml_actions()
        unreachable: set[str] = yaml_actions - prompt
        self.assertEqual(
            unreachable, set(),
            "Actions defined in actions.yml are not advertised in the "
            "intent.py prompt — the LLM cannot select them: "
            f"{sorted(unreachable)}.  Add a corresponding "
            "``- <name>: <description>`` bullet to "
            "_SYSTEM_PROMPT_TEMPLATE.",
        )


if __name__ == "__main__":
    unittest.main()
