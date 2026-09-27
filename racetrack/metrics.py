"""Per-attempt metrics: failure classification (and landing error for a throw variant)."""
from __future__ import annotations

import math
import re
from typing import Optional

from models import Attempt, FailureMode, LandingError, RawObservation, ScoringConfig

_TIMEOUT = re.compile(r"time\s?out|timed out|killed|exceeded|deadline", re.I)


def landing_error(raw: RawObservation) -> Optional[LandingError]:
    """Project the rim-height crossing point onto the throw line (base -> bin)."""
    if raw.crossing_xy is None:
        return None
    bx, by = raw.base_xy
    cx, cy = raw.bin_xy
    px, py = raw.crossing_xy
    ux, uy = cx - bx, cy - by
    norm = math.hypot(ux, uy)
    if norm < 1e-9:
        return None
    ux, uy = ux / norm, uy / norm
    dx, dy = px - cx, py - cy
    return LandingError(along=round(dx * ux + dy * uy, 4), across=round(-dx * uy + dy * ux, 4))


def classify(a: Attempt, cfg: ScoringConfig) -> FailureMode:
    """Root cause first: code problems, then drops, then (throw only) where it landed."""
    if a.success:
        return FailureMode.none
    if a.error:
        return FailureMode.timeout if _TIMEOUT.search(a.error) else FailureMode.code_error
    if a.dropped:
        return FailureMode.dropped
    err = a.landing_error_m
    if err is not None:
        if math.hypot(err.along, err.across) <= cfg.bin_radius_m - cfg.inside_margin_m:
            return FailureMode.rim_bounce_out
        if abs(err.along) >= abs(err.across):
            return FailureMode.short if err.along < 0 else FailureMode.long
        return FailureMode.left if err.across > 0 else FailureMode.right
    if a.collisions > 0:
        return FailureMode.collision
    return FailureMode.missed


def enrich(a: Attempt, cfg: ScoringConfig) -> Attempt:
    """Fill in derived fields the sender didn't set. Never overrides what was sent."""
    if a.landing_error_m is None and a.raw is not None:
        a = a.model_copy(update={"landing_error_m": landing_error(a.raw)})
    if a.failure_mode is None:
        a = a.model_copy(update={"failure_mode": classify(a, cfg)})
    return a


def error_headline(error: Optional[str], limit: int = 90) -> Optional[str]:
    """Last non-empty line of a traceback is usually the useful one."""
    if not error:
        return None
    lines = [l.strip() for l in error.strip().splitlines() if l.strip()]
    line = lines[-1] if lines else error.strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"
