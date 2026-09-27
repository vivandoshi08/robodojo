"""The unit of experience: one attempt at getting one piece of trash in the bin."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    """What actually happened to the trash."""

    IN = "in"
    RIM_OUT = "rim_out"
    BOUNCED_OUT = "bounced_out"
    MISS = "miss"
    WIDE = "wide"
    NO_GRASP = "no_grasp"
    DROPPED_EARLY = "dropped_early"

    @property
    def landed_in(self) -> bool:
        return self is Outcome.IN


#: Coarse failure direction, used by the advisor to pick which knob to turn.
OVERSHOOT_OUTCOMES = (Outcome.BOUNCED_OUT,)
SHORT_OUTCOMES = (Outcome.MISS, Outcome.DROPPED_EARLY)
WIDE_OUTCOMES = (Outcome.WIDE,)
GRASP_OUTCOMES = (Outcome.NO_GRASP,)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def distance_bucket(distance_cm: float) -> int:
    """Round to the nearest 10 cm.

    Recall matches lexically before it reaches for a vector, so a stable
    bucketed phrase ("a bin 50 cm away") lands far more often than raw floats.
    """
    return int(round(distance_cm / 10.0) * 10)


def bearing_bucket(bearing_deg: float) -> int:
    """Round a bearing to the nearest 15 deg, matching how situations are phrased."""
    return int(round(bearing_deg / 15.0) * 15)


def situation_phrase(
    strategy: str, trash_type: str, distance_cm: float, bearing_deg: float = 0.0
) -> str:
    """The shared phrasing for recall queries and stored task descriptions."""
    phrase = (
        f"{strategy.replace('_', ' ')} a {trash_type} "
        f"into a bin {distance_bucket(distance_cm)} cm away"
    )
    bearing = bearing_bucket(bearing_deg)
    if bearing:
        side = "left" if bearing < 0 else "right"
        phrase += f" {abs(bearing)} degrees to the {side}"
    return phrase


#: Outcome vocabularies differ across the seam. This is the mapping, in one
#: place: robodojo's outcome on the left, the GBrain side's on the right.
GBRAIN_OUTCOME = {
    Outcome.IN: "in_bin",
    Outcome.RIM_OUT: "rim_out",
    Outcome.BOUNCED_OUT: "long",
    Outcome.MISS: "short",
    Outcome.WIDE: "wide",
    Outcome.NO_GRASP: "dropped",
    Outcome.DROPPED_EARLY: "dropped",
}

#: Parameters the GBrain verdict renderer formats for every episode. It calls
#: ``.toFixed()`` on each one, so a row must carry all three or the page throws.
GBRAIN_PARAM_KEYS = ("release_height_m", "toss_velocity_mps", "grasp_angle_deg")

#: Scoring. In the bin is what counts; a rim-out earns partial credit for being
#: close; a fast success earns a small bonus. Knocking the bin over forfeits all
#: credit and costs the penalty, however the throw ended — the same rule as
#: :attr:`Episode.success`, so the standings and the winner never disagree.
#: Standings are the sum over a competitor's attempts, so these numbers only
#: need to order strategies sensibly.
SCORE_IN_BIN = 10.0
SCORE_NEAR_MISS = 2.0
SCORE_KNOCKOVER_PENALTY = 5.0
SCORE_SPEED_BONUS_MAX = 2.0
SCORE_SPEED_REFERENCE_S = 5.0


def params_to_si(params: dict[str, float]) -> dict[str, float]:
    """Centimetre parameters mirrored into metres, keys renamed ``*_m``.

    Units live in the parameter names on both sides of the seam, so a value can
    never be read in the wrong unit by accident.
    """
    out: dict[str, float] = {}
    for key, value in params.items():
        if key.endswith("_cm"):
            out[key[:-3] + "_m"] = round(value / 100.0, 4)
        else:
            out[key] = value
    return out


@dataclass
class Episode:
    """One attempt, with the numbers that made it and the reason it ended."""

    race_id: str
    scope: str
    strategy: str
    attempt: int
    trash_type: str
    bin_distance_cm: float
    params: dict[str, float]
    outcome: Outcome
    reason: str
    #: Bearing to the bin, degrees; negative is left of straight ahead.
    bin_bearing_deg: float = 0.0
    #: Competitor identity on the GBrain side; defaults to the scope.
    agent_id: str | None = None
    bin_knocked_over: bool = False
    duration_s: float | None = None
    seed: int | None = None
    sim: bool = True
    bin_position: dict[str, float] | None = None
    recorded_at: str = field(default_factory=_now)
    #: Slug Memorable gave this attempt's procedure, when it was admitted.
    memorable_slug: str | None = None
    #: Why Memorable refused, when it was not (e.g. "no_postcondition").
    memorable_refusal: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.outcome, str):
            self.outcome = Outcome(self.outcome)
        if not self.agent_id:
            self.agent_id = self.scope
        if self.attempt < 1:
            raise ValueError("attempt is 1-based")

    # -- identity ---------------------------------------------------------

    @property
    def episode_id(self) -> str:
        raw = f"{self.race_id}|{self.scope}|{self.strategy}|{self.attempt}|{self.recorded_at}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @property
    def success(self) -> bool:
        """In the bin, bin still standing. The only thing that counts."""
        return self.outcome.landed_in and not self.bin_knocked_over

    @property
    def score(self) -> float:
        """Points for this attempt. See the SCORE_* constants for the rationale."""
        if self.bin_knocked_over:
            return -SCORE_KNOCKOVER_PENALTY
        if self.outcome.landed_in:
            points = SCORE_IN_BIN
            if self.duration_s is not None:
                bonus = SCORE_SPEED_BONUS_MAX - (self.duration_s / SCORE_SPEED_REFERENCE_S)
                points += max(0.0, min(SCORE_SPEED_BONUS_MAX, bonus))
        elif self.outcome is Outcome.RIM_OUT:
            points = SCORE_NEAR_MISS
        else:
            points = 0.0
        return round(points, 3)

    # -- phrasing ---------------------------------------------------------

    def situation(self) -> str:
        """Natural-language task, shared by recall queries and stored traces.

        Deliberately describes the *situation*, not the attempt: Memorable keeps
        one procedure per task with revisions beside it, so every race on this
        situation accumulates on one intent instead of littering the store.
        """
        return situation_phrase(
            self.strategy, self.trash_type, self.bin_distance_cm, self.bin_bearing_deg
        )

    def situation_key(self) -> str:
        return (
            f"{self.strategy}/{self.trash_type}/{distance_bucket(self.bin_distance_cm)}"
            f"/{bearing_bucket(self.bin_bearing_deg):+d}"
        )

    def session_id(self) -> str:
        return f"{self.race_id}:{self.scope}:{self.strategy}:a{self.attempt}"

    def sim_command(self) -> str:
        """The command that reproduces this attempt.

        Extraction keeps commands verbatim in the stored procedure, so the
        tuned numbers have to live on the command line to survive into recall.
        """
        parts = [
            "python -m robodojo.sim",
            f"--strategy {self.strategy}",
            f"--trash {self.trash_type}",
            f"--bin-cm {self.bin_distance_cm:g}",
            f"--bin-bearing-deg {self.bin_bearing_deg:g}",
        ]
        for key in sorted(self.params):
            parts.append(f"--{key.replace('_', '-')} {self.params[key]:g}")
        if self.seed is not None:
            parts.append(f"--seed {self.seed}")
        return " ".join(parts)

    def outcome_line(self) -> str:
        """One line a human (or a QM channel) can read."""
        verdict = "PASS" if self.success else "FAIL"
        bits = [f"outcome={self.outcome.value}"]
        if self.bin_knocked_over:
            bits.append("bin_knocked_over=true")
        if self.duration_s is not None:
            bits.append(f"duration_s={self.duration_s:g}")
        bits.append(verdict)
        return " ".join(bits)

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outcome"] = self.outcome.value
        data["episode_id"] = self.episode_id
        data["success"] = self.success
        data["situation"] = self.situation()
        data["situation_key"] = self.situation_key()
        # GBrain's skillopt questions and answer keys are phrased in metres.
        # Parameter names carry their own units; these two are the mirror.
        data["bin_distance_m"] = round(self.bin_distance_cm / 100.0, 4)
        data["params_si"] = params_to_si(self.params)
        data["bin_angle_deg"] = self.bin_bearing_deg
        data["score"] = self.score
        data["gbrain_outcome"] = GBRAIN_OUTCOME[self.outcome]
        return data

    def to_gbrain_row(self) -> dict[str, Any]:
        """This episode in the shape the GBrain side's ``fromRow`` expects.

        Metres and degrees, its outcome vocabulary, a numeric score, and all
        three of its parameter keys present — missing ones are 0.0 rather than
        absent, because the verdict renderer formats every key unconditionally.
        """
        si = params_to_si(self.params)
        params = {key: float(si.get(key, 0.0)) for key in GBRAIN_PARAM_KEYS}
        return {
            "episode_id": self.episode_id,
            "race_id": self.race_id,
            "agent_id": self.agent_id,
            "strategy": self.strategy,
            "attempt": self.attempt,
            "trash_type": self.trash_type,
            "bin_position": {
                "distance_m": round(self.bin_distance_cm / 100.0, 4),
                "angle_deg": self.bin_bearing_deg,
            },
            "bin_distance_m": round(self.bin_distance_cm / 100.0, 4),
            "bin_angle_deg": self.bin_bearing_deg,
            "params": params,
            **params,
            "outcome": GBRAIN_OUTCOME[self.outcome],
            "score": self.score,
            "reason": self.reason,
            # Not in its interface, but harmless and useful on the page.
            "bin_knocked_over": self.bin_knocked_over,
            "robodojo_outcome": self.outcome.value,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in fields}
        missing = {"race_id", "scope", "strategy", "attempt", "trash_type",
                   "bin_distance_cm", "params", "outcome", "reason"} - payload.keys()
        if missing:
            raise ValueError(f"episode missing required field(s): {sorted(missing)}")
        return cls(**payload)
