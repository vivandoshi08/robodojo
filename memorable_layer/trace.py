"""Episode -> Memorable trace.

Shaped by what the extraction service actually admits. A trace of grasp/throw
calls carrying outcome fields is refused with ``no_postcondition, prefilter``.
What gets stored is a trace that looks like real work:

1. it reads the strategy's locked skill file,
2. it shows the dead ends (the attempts that missed, with their reason),
3. it *changes* something — the tuned parameter file,
4. it ends on a command that verified the change and passed.

Step 4 is the postcondition. Step 3 is the mutation. Without both, nothing is
stored. The tuned numbers are repeated as flags on the verifying command because
extraction keeps commands verbatim, so that is what survives into recall.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .episode import Episode

HARNESS = "robodojo-qm"


def params_path(strategy: str) -> str:
    return f"strategies/{strategy}/params.json"


def skill_path(strategy: str) -> str:
    return f"strategies/{strategy}/SKILL.md"


def _attempt_step(episode: Episode) -> dict[str, Any]:
    """One attempt as a tool call, with the outcome the sim measured."""
    return {
        "name": "shell",
        "input": {"command": episode.sim_command()},
        "result": {
            "exit_code": 0 if episode.success else 1,
            "stdout": f"{episode.outcome_line()} reason={episode.reason}",
        },
    }


def build_trace(
    episode: Episode,
    dead_ends: Sequence[Episode] = (),
    harness: str = HARNESS,
) -> dict[str, Any]:
    """Build the trace for a *successful* attempt.

    ``dead_ends`` are the earlier attempts at the same situation in this scope.
    They are what makes the stored procedure a record of tuning rather than of
    one lucky throw; synthesis drops the ones that led nowhere.
    """
    if not episode.success:
        raise ValueError(
            "only a successful attempt can be ingested: the extraction service "
            "refuses a trace with no verified postcondition"
        )

    tool_calls: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "input": {"path": skill_path(episode.strategy)},
            "result": {"ok": True},
        }
    ]

    for miss in dead_ends:
        tool_calls.append(_attempt_step(miss))

    tool_calls.append(
        {
            "name": "write_file",
            "input": {
                "path": params_path(episode.strategy),
                "content": json.dumps(episode.params, indent=2, sort_keys=True),
            },
            "result": {"ok": True, "bytes": len(json.dumps(episode.params))},
        }
    )

    # The postcondition: the winning run, re-stated with its numbers on the
    # command line so recall surfaces them.
    tool_calls.append(_attempt_step(episode))

    return {
        "session_id": episode.session_id(),
        "task_description": episode.situation(),
        "harness": harness,
        "tool_calls": tool_calls,
    }


def recall_query(
    strategy: str, trash_type: str, distance_cm: float, bearing_deg: float = 0.0
) -> str:
    """The phrase used to look for prior experience at this situation."""
    from .episode import situation_phrase

    return situation_phrase(strategy, trash_type, distance_cm, bearing_deg)
