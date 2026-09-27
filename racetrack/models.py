"""Data contracts for the tracker.

An Attempt is one trip around the Phase 1 agent loop: the identity run_agent.py knows
(agent, seed, attempt) plus the executor's result.json exactly as the plan freezes it.
Unknown extra fields in result.json are kept, so the executor can grow without breaking this.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class FailureMode(str, Enum):
    none = "none"
    code_error = "code_error"        # policy raised, didn't parse, or called outside the API
    timeout = "timeout"              # killed by the executor's 30 s limit
    dropped = "dropped"              # result.json "dropped": true
    collision = "collision"          # failed and hit something
    missed = "missed"                # ran cleanly, object not in the bin
    # Only if a throwing variant reports landing geometry (raw.crossing_xy):
    short = "short"
    long = "long"
    left = "left"
    right = "right"
    rim_bounce_out = "rim_bounce_out"


class LandingError(BaseModel):
    """Throw variant only. Where the object crossed rim height, relative to the bin center.
    along: + = past the center, - = short.  across: + = left of the throw line."""
    along: float
    across: float


class RawObservation(BaseModel):
    """Throw variant only. Send it and the tracker derives landing_error_m."""
    base_xy: tuple[float, float] = (0.0, 0.0)
    bin_xy: tuple[float, float]
    crossing_xy: Optional[tuple[float, float]] = None


class Attempt(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Identity: from run_agent.py / the race runner, not part of result.json.
    agent_id: str = "agent-1"
    seed: int
    attempt: int = 1
    task: Optional[str] = None

    # result.json, as frozen in the Phase 1 plan.
    success: bool
    time_s: Optional[float] = None
    collisions: int = 0
    energy: Optional[float] = None
    dropped: bool = False
    error: Optional[str] = None
    frames: list[str] = Field(default_factory=list)
    video: Optional[str] = None
    code: Optional[str] = None

    # Derived by the tracker (never overrides a value the sender set).
    failure_mode: Optional[FailureMode] = None
    landing_error_m: Optional[LandingError] = None
    raw: Optional[RawObservation] = None


class ScoringConfig(BaseModel):
    """Per agent, over its seeds (a seed ends when solved or after max_attempts tries):

    score = w_success * solve_rate
          + w_extra_attempt * mean(tries used - 1)
          + w_time_s * mean time_s of the winning tries
          + w_collisions * collisions per seed (final try)
          + w_energy * mean energy of the winning tries

    Calibrate w_time_s and w_energy once real numbers exist, so solving still dominates."""
    seeds_per_agent: int = 3
    max_attempts: int = 5
    w_success: float = 100.0
    w_extra_attempt: float = -10.0
    w_time_s: float = -1.0
    w_collisions: float = -10.0
    w_energy: float = -0.1
    min_solve_rate_for_skill: float = 0.6
    bin_radius_m: float = 0.15           # throw variant only
    inside_margin_m: float = 0.01        # throw variant only


class AttemptMark(BaseModel):
    attempt: int
    success: bool
    failure_mode: Optional[str] = None
    error: Optional[str] = None


class SeedOutcome(BaseModel):
    seed: int
    tries: list[AttemptMark]
    solved: bool
    solved_at: Optional[int] = None      # which try solved it
    done: bool                           # solved, or out of tries


class BestAttempt(BaseModel):
    seed: int
    attempt: int
    time_s: Optional[float] = None
    video: Optional[str] = None
    frames: list[str] = Field(default_factory=list)


class AgentScore(BaseModel):
    agent_id: str
    persona: Optional[str] = None
    mode: Optional[str] = None
    info: dict[str, Any] = Field(default_factory=dict)
    seeds_target: int
    seeds_done: int
    complete: bool
    seeds_attempted: int
    seeds_solved: int
    solve_rate: float
    first_try_rate: float
    mean_tries_to_solve: Optional[float] = None
    attempts_total: int
    mean_time_s: Optional[float] = None
    collisions_per_seed: float
    mean_energy: Optional[float] = None
    score: float
    score_breakdown: dict[str, float]
    failure_modes: dict[str, int]
    top_failure: Optional[str] = None
    top_error: Optional[str] = None
    best_attempt: Optional[BestAttempt] = None
    skill_eligible: bool
    seeds: list[SeedOutcome] = Field(default_factory=list)
    rank: Optional[int] = None


class RaceCreate(BaseModel):
    race_id: Optional[str] = None        # auto: race-001, race-002, ...
    label: str = ""                      # e.g. "phase1", "cold", "warm"
    task: str = "can_to_bin"
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
