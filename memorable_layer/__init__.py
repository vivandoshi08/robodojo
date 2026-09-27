"""Memorable integration for robodojo.

Memory *between attempts*, not just between races. Every attempt an agent makes
is recorded with its parameters, its outcome and one line on why it ended that
way; before the next attempt the agent recalls similar episodes and adjusts.

Roles, kept distinct on purpose:

* **Memorable (this package)** — what happens during a race, attempt by attempt.
* **GBrain (the referee's side)** — what agents know before a race and what is
  learned after it: the verdict page, the policy, and ``skillopt``.

The seam between them is :func:`race_episodes` (the evidence the verdict links),
:func:`answer_key_rows` (parameter ranges from throws that went in),
:func:`skillopt_rows` (clean numbers) and :func:`strategy_record` (the
"won 2 of 3 races" line).

Isolation: one QM scope is one Memorable namespace, pinned with
``MEMORABLE_HOME``. Agents cannot read each other's attempts mid-race.
"""

from __future__ import annotations

from .advisor import Advisor, AttemptBrief, baseline_params
from .client import IngestResult, MemorableClient, Recollection
from .config import STRATEGIES, TRASH_TYPES, Settings, scope_home
from .episode import (
    GBRAIN_OUTCOME,
    Episode,
    Outcome,
    bearing_bucket,
    distance_bucket,
    situation_phrase,
)
from .evidence import (
    answer_key_rows,
    distance_policy_hint,
    gbrain_race_result,
    gbrain_rows,
    race_episodes,
    race_evidence,
    skillopt_rows,
    strategy_record,
    strategy_summary,
)
from .recorder import AttemptRecorder, open_recorder
from .store import EpisodeStore
from .trace import build_trace, recall_query

__all__ = [
    "Advisor",
    "GBRAIN_OUTCOME",
    "AttemptBrief",
    "AttemptRecorder",
    "Episode",
    "EpisodeStore",
    "IngestResult",
    "MemorableClient",
    "Outcome",
    "Recollection",
    "STRATEGIES",
    "Settings",
    "TRASH_TYPES",
    "answer_key_rows",
    "baseline_params",
    "bearing_bucket",
    "build_trace",
    "distance_bucket",
    "distance_policy_hint",
    "gbrain_race_result",
    "gbrain_rows",
    "open_recorder",
    "race_episodes",
    "race_evidence",
    "recall_query",
    "scope_home",
    "situation_phrase",
    "skillopt_rows",
    "strategy_record",
    "strategy_summary",
]

__version__ = "0.1.0"
