"""Before every attempt: recall, then adjust.

This is what makes an agent improve *within* a race. Attempt 3 lowers toss
velocity because attempts 1 and 2 overshot, and it says so out loud in the QM
channel so memory is visible live instead of buried in a backend.

Two sources, on purpose:

* the in-scope episode log — fresh, zero-latency, guaranteed to contain the
  attempt that just missed;
* ``memorable recall`` — the durable cross-race prior, which is what makes
  race 2 start smarter than race 1.

Adjustments are deterministic and explainable. A judge (or a teammate) can read
the rationale line and check it against the episodes it names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import MemorableClient, Recollection
from .config import Settings
from .episode import (
    GRASP_OUTCOMES,
    OVERSHOOT_OUTCOMES,
    SHORT_OUTCOMES,
    WIDE_OUTCOMES,
    Episode,
    distance_bucket,
)
from .store import EpisodeStore
from .trace import recall_query

#: Single-step adjustment sizes. Small enough to converge, big enough to see.
OVERSHOOT_VELOCITY_CUT = 0.15
SHORT_VELOCITY_BUMP = 0.10
KNOCKOVER_VELOCITY_CUT = 0.20
KNOCKOVER_RELEASE_RAISE_CM = 5.0
RIM_RELEASE_DROP_CM = 5.0
GRASP_WIDTH_OPEN_CM = 1.0


@dataclass
class AttemptBrief:
    """What the agent should do next, and why."""

    situation: str
    attempt: int
    suggested_params: dict[str, float]
    deltas: dict[str, float] = field(default_factory=dict)
    rationale: str = "no prior experience at this situation, using baseline"
    prior_attempts: int = 0
    prior_successes: int = 0
    recollections: list[Recollection] = field(default_factory=list)
    procedure_context: str = ""
    misses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def channel_line(self) -> str:
        """One line to post in the shared QM channel."""
        return f"[attempt {self.attempt}] {self.rationale}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "situation": self.situation,
            "attempt": self.attempt,
            "suggested_params": self.suggested_params,
            "deltas": self.deltas,
            "rationale": self.rationale,
            "channel_line": self.channel_line,
            "prior_attempts": self.prior_attempts,
            "prior_successes": self.prior_successes,
            "recollections": [r.to_dict() for r in self.recollections],
            "procedure_context": self.procedure_context,
            "misses": self.misses,
        }


class Advisor:
    """Turns past episodes into the next attempt's parameters."""

    def __init__(
        self,
        store: EpisodeStore,
        client: MemorableClient | None,
        settings: Settings,
    ):
        self.store = store
        self.client = client
        self.settings = settings

    def brief(
        self,
        strategy: str,
        trash_type: str,
        distance_cm: float,
        baseline_params: dict[str, float],
        attempt: int | None = None,
        race_id: str | None = None,
        scope: str | None = None,
        bearing_deg: float = 0.0,
    ) -> AttemptBrief:
        race_id = race_id or self.settings.race_id
        scope = scope or self.settings.scope
        situation = recall_query(strategy, trash_type, distance_cm, bearing_deg)

        if attempt is None:
            attempt = self.store.next_attempt_number(race_id, scope, strategy)

        # Mid-race, an agent may only read its own namespace.
        history = self.store.similar(
            strategy, trash_type, distance_cm, scope=scope, bearing_deg=bearing_deg
        )

        params = dict(baseline_params)
        # A past success at this situation beats any heuristic.
        won_before = next((e for e in history if e.success), None)
        if won_before is not None:
            params.update(won_before.params)

        # Only misses since that success still carry information. Without this,
        # a known-good parameter set keeps getting cut by failures it already
        # superseded, and the agent walks away from the throw that worked.
        deltas, notes = self._adjust(params, self._unresolved(history))

        brief = AttemptBrief(
            situation=situation,
            attempt=attempt,
            suggested_params=params,
            deltas=deltas,
            prior_attempts=len(history),
            prior_successes=sum(1 for e in history if e.success),
            misses=[
                {
                    "attempt": e.attempt,
                    "outcome": e.outcome.value,
                    "reason": e.reason,
                    "params": e.params,
                }
                for e in history
                if not e.success
            ][:5],
        )

        if self.client is not None and self.client.available():
            brief.recollections = self.client.recall(situation, limit=3)
            if brief.recollections:
                brief.procedure_context = self.client.show(brief.recollections[0].slug)

        brief.rationale = self._rationale(brief, notes, won_before)
        return brief

    @staticmethod
    def _unresolved(history: list[Episode]) -> list[Episode]:
        """Misses more recent than the last success. ``history`` is newest first."""
        recent: list[Episode] = []
        for episode in history:
            if episode.success:
                break
            recent.append(episode)
        return recent

    # -- heuristics -------------------------------------------------------

    def _adjust(
        self, params: dict[str, float], history: list[Episode]
    ) -> tuple[dict[str, float], list[str]]:
        """Nudge the knob that the failures point at."""
        misses = [e for e in history if not e.success]
        if not misses:
            return {}, []

        before = dict(params)
        notes: list[str] = []

        overshoots = [e for e in misses if e.outcome in OVERSHOOT_OUTCOMES]
        shorts = [e for e in misses if e.outcome in SHORT_OUTCOMES]
        knockovers = [e for e in misses if e.bin_knocked_over]
        rim_outs = [e for e in misses if e.outcome.value == "rim_out"]
        no_grasps = [e for e in misses if e.outcome in GRASP_OUTCOMES]
        wides = [e for e in misses if e.outcome in WIDE_OUTCOMES]

        if knockovers:
            self._scale(params, "toss_velocity_mps", 1 - KNOCKOVER_VELOCITY_CUT)
            self._shift(params, "release_height_cm", KNOCKOVER_RELEASE_RAISE_CM)
            notes.append(
                f"{len(knockovers)} knockover episode(s), cutting velocity "
                f"{int(KNOCKOVER_VELOCITY_CUT * 100)}% and releasing higher"
            )
        elif overshoots:
            self._scale(params, "toss_velocity_mps", 1 - OVERSHOOT_VELOCITY_CUT)
            notes.append(
                f"{len(overshoots)} overshoot episode(s), dropping velocity "
                f"{int(OVERSHOOT_VELOCITY_CUT * 100)}%"
            )
        elif shorts:
            self._scale(params, "toss_velocity_mps", 1 + SHORT_VELOCITY_BUMP)
            notes.append(
                f"{len(shorts)} short episode(s), raising velocity "
                f"{int(SHORT_VELOCITY_BUMP * 100)}%"
            )

        if rim_outs and not knockovers:
            self._shift(params, "release_height_cm", -RIM_RELEASE_DROP_CM)
            notes.append(f"{len(rim_outs)} rim-out episode(s), releasing lower")

        if no_grasps:
            self._shift(params, "grasp_width_cm", GRASP_WIDTH_OPEN_CM)
            notes.append(f"{len(no_grasps)} failed grasp(s), opening the gripper")

        if wides:
            # A lateral miss is an aim problem, not a power problem. Only correct
            # it when the strategy actually exposes an aim parameter; otherwise
            # say so rather than turning the wrong knob.
            if "aim_offset_deg" in params:
                mean_bearing = sum(e.bin_bearing_deg for e in wides) / len(wides)
                self._shift(params, "aim_offset_deg", -mean_bearing / 2.0)
                notes.append(f"{len(wides)} wide miss(es), correcting aim")
            else:
                notes.append(
                    f"{len(wides)} wide miss(es), but this strategy has no aim "
                    "parameter to correct"
                )

        deltas = {
            key: round(params[key] - before[key], 4)
            for key in params
            if key in before and params[key] != before[key]
        }
        return deltas, notes

    @staticmethod
    def _scale(params: dict[str, float], key: str, factor: float) -> None:
        if key in params:
            params[key] = round(params[key] * factor, 4)

    @staticmethod
    def _shift(params: dict[str, float], key: str, delta: float) -> None:
        if key in params:
            params[key] = round(max(0.0, params[key] + delta), 4)

    def _rationale(
        self, brief: AttemptBrief, notes: list[str], won_before: Episode | None
    ) -> str:
        parts: list[str] = []
        if won_before is not None:
            parts.append(
                f"reusing the parameters that worked on attempt {won_before.attempt} "
                f"of {won_before.race_id}"
            )
            if not notes:
                parts.append("nothing has missed since, holding them")
        if notes:
            parts.append("recalled " + "; ".join(notes))
        if brief.recollections:
            top = brief.recollections[0]
            parts.append(
                f"memorable hit {top.slug.split('/')[-1]} "
                f"({top.score:.2f} {top.method})"
            )
        if not parts:
            return "no prior experience at this situation, using baseline"
        return ", ".join(parts)


def baseline_params(strategy: str, trash_type: str, distance_cm: float) -> dict[str, float]:
    """Cold-start parameters for a situation nothing is known about.

    Intentionally crude — the point of the race is that memory replaces these.
    """
    bucket = distance_bucket(distance_cm)
    # grasp_angle_deg is present for every strategy: the GBrain side formats it
    # on every episode row.
    if strategy == "toss":
        params = {
            "toss_velocity_mps": round(0.9 + bucket / 100.0, 3),
            "release_height_cm": 35.0,
            "grasp_angle_deg": 90.0,
            "grasp_width_cm": 8.0,
        }
    elif strategy == "drop":
        params = {
            "release_height_cm": 10.0,
            "grasp_angle_deg": 90.0,
            "grasp_width_cm": 8.0,
        }
    elif strategy == "push_off_edge":
        params = {
            "push_velocity_mps": 0.25,
            "grasp_angle_deg": 45.0,
            "grasp_width_cm": 8.0,
        }
    else:  # pick_place
        params = {
            "release_height_cm": 5.0,
            "grasp_angle_deg": 90.0,
            "grasp_width_cm": 8.0,
        }
    if trash_type == "bottle":
        params["grasp_width_cm"] = 7.0
    elif trash_type == "can":
        params["grasp_width_cm"] = 6.5
    return params
