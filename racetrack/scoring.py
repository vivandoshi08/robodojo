"""Aggregate attempts into per-agent scores, pick winners, and write lessons for memory.

Structure: race -> agent -> seed -> tries (1..max_attempts). A seed is solved by its
first successful try; tries after that are ignored. Metrics that describe the policy
itself (time, energy) come from the winning tries only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from metrics import error_headline
from models import (AgentScore, Attempt, AttemptMark, BestAttempt, FailureMode,
                    ScoringConfig, SeedOutcome)


def _mean(xs: Iterable[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _r(x: Optional[float], nd: int = 3) -> Optional[float]:
    return None if x is None else round(x, nd) + 0.0     # +0.0 turns -0.0 into 0.0


def seed_outcomes(attempts: list[Attempt], cfg: ScoringConfig) -> list[tuple[SeedOutcome, list[Attempt], Attempt]]:
    """Per seed: (outcome, tries that count, final try = the winning one or the last one)."""
    by_seed: dict[int, list[Attempt]] = defaultdict(list)
    for a in attempts:
        by_seed[a.seed].append(a)
    out = []
    for seed in sorted(by_seed):
        tries = sorted(by_seed[seed], key=lambda a: a.attempt)
        first_win = next((a for a in tries if a.success), None)
        used = [a for a in tries if first_win is None or a.attempt <= first_win.attempt]
        solved = first_win is not None and first_win.attempt <= cfg.max_attempts
        final = first_win or used[-1]
        done = first_win is not None or max(a.attempt for a in used) >= cfg.max_attempts
        marks = [AttemptMark(attempt=a.attempt, success=a.success,
                             failure_mode=a.failure_mode.value if a.failure_mode else None,
                             error=error_headline(a.error)) for a in used]
        out.append((SeedOutcome(seed=seed, tries=marks, solved=solved,
                                solved_at=first_win.attempt if solved else None, done=done),
                    used, final))
    return out


def aggregate(agent_id: str, attempts: list[Attempt], cfg: ScoringConfig,
              info: Optional[dict[str, Any]] = None) -> AgentScore:
    info = info or {}
    outcomes = seed_outcomes(attempts, cfg)
    n = len(outcomes)
    solved = [(o, used, final) for o, used, final in outcomes if o.solved]
    solve_rate = len(solved) / n if n else 0.0
    first_try_rate = sum(1 for o, _, _ in outcomes if o.solved_at == 1) / n if n else 0.0
    mean_tries = _mean(o.solved_at for o, _, _ in solved)
    extra = _mean(float(final.attempt - 1) for _, _, final in outcomes)
    wins = [final for _, _, final in solved]
    mean_time = _mean(a.time_s for a in wins)
    mean_energy = _mean(a.energy for a in wins)
    collisions = _mean(float(final.collisions) for _, _, final in outcomes) or 0.0

    breakdown = {"solved": cfg.w_success * solve_rate,
                 "extra_tries": cfg.w_extra_attempt * (extra or 0.0),
                 "collisions": cfg.w_collisions * collisions}
    if mean_time is not None:
        breakdown["time"] = cfg.w_time_s * mean_time
    if mean_energy is not None:
        breakdown["energy"] = cfg.w_energy * mean_energy
    breakdown = {k: _r(v, 2) for k, v in breakdown.items()}

    failed = [a for _, used, _ in outcomes for a in used if not a.success]
    modes = Counter(a.failure_mode.value for a in failed if a.failure_mode and a.failure_mode != FailureMode.none)
    errors = Counter(error_headline(a.error) for a in failed if a.error)
    best = min(wins, key=lambda a: (a.time_s if a.time_s is not None else float("inf"), a.seed), default=None)

    seeds_done = sum(1 for o, _, _ in outcomes if o.done)
    complete = seeds_done >= cfg.seeds_per_agent
    return AgentScore(
        agent_id=agent_id, persona=info.get("persona"), mode=info.get("mode"),
        info={k: v for k, v in info.items() if k not in ("agent_id",)},
        seeds_target=cfg.seeds_per_agent, seeds_done=seeds_done, complete=complete,
        seeds_attempted=n, seeds_solved=len(solved),
        solve_rate=_r(solve_rate, 4), first_try_rate=_r(first_try_rate, 4),
        mean_tries_to_solve=_r(mean_tries, 2), attempts_total=sum(len(u) for _, u, _ in outcomes),
        mean_time_s=_r(mean_time), collisions_per_seed=_r(collisions), mean_energy=_r(mean_energy),
        score=_r(sum(breakdown.values()), 2), score_breakdown=breakdown,
        failure_modes=dict(modes), top_failure=modes.most_common(1)[0][0] if modes else None,
        top_error=errors.most_common(1)[0][0] if errors else None,
        best_attempt=BestAttempt(seed=best.seed, attempt=best.attempt, time_s=best.time_s,
                                 video=best.video, frames=best.frames) if best else None,
        skill_eligible=complete and solve_rate >= cfg.min_solve_rate_for_skill,
        seeds=[o for o, _, _ in outcomes],
    )


def _sort_key(s: AgentScore):
    inf = float("inf")
    return (-s.score, -s.solve_rate,
            s.mean_tries_to_solve if s.mean_tries_to_solve is not None else inf,
            s.mean_time_s if s.mean_time_s is not None else inf)


def rank(scores: list[AgentScore]) -> list[AgentScore]:
    return [s.model_copy(update={"rank": i + 1}) for i, s in enumerate(sorted(scores, key=_sort_key))]


def pick_winner(scores: list[AgentScore], final: bool = False) -> Optional[AgentScore]:
    """Winner = best score among agents that finished all their seeds.
    When the race is closed (final), fall back to anything that ran."""
    pool = [s for s in scores if s.complete]
    if not pool and final:
        pool = [s for s in scores if s.attempts_total > 0]
    return min(pool, key=_sort_key) if pool else None


def leader(scores: list[AgentScore]) -> Optional[AgentScore]:
    """Who is ahead right now, finished or not. Live view only."""
    pool = [s for s in scores if s.attempts_total > 0]
    return min(pool, key=_sort_key) if pool else None


# ---------------------------------------------------------------- lessons ---

def _agent_line(s: AgentScore) -> str:
    who = s.persona or s.agent_id
    line = f"{who}: solved {s.seeds_solved}/{s.seeds_attempted} seeds"
    if s.mean_tries_to_solve is not None:
        line += f" in {s.mean_tries_to_solve:.1f} tries on average"
    firsts = sum(1 for o in s.seeds if o.solved_at == 1)
    line += f" ({firsts} on the first try)"
    if s.failure_modes:
        line += "; failed tries: " + ", ".join(
            f"{c} {m.replace('_', ' ')}" for m, c in sorted(s.failure_modes.items(), key=lambda kv: -kv[1]))
    if s.top_error:
        line += f"; most common error: {s.top_error}"
    return line


def lessons(scores: list[AgentScore], attempts: dict[str, list[Attempt]],
            cfg: ScoringConfig, final: bool = False) -> dict[str, Any]:
    """Plain-text lessons (for Memorable) and the winning policy (for the GBrain skill library)."""
    ranked = rank(scores)
    lines = [_agent_line(s) for s in ranked if s.attempts_total]

    all_modes: Counter = Counter()
    for s in ranked:
        all_modes.update(s.failure_modes)
    if all_modes:
        mode, count = all_modes.most_common(1)[0]
        lines.append(f"Most common failure across agents: {mode.replace('_', ' ')} "
                     f"({count} of {sum(all_modes.values())} failed tries).")

    top = pick_winner(ranked, final=True) if final else leader(ranked)
    skill = None
    if top:
        who = top.persona or top.agent_id
        verdict = ("eligible to become a skill" if top.skill_eligible else
                   f"below the {cfg.min_solve_rate_for_skill:.0%} solve bar, so record lessons only")
        lines.append(f"{'Winner' if final else 'Leading'}: {who} with score {top.score:.1f}, "
                     f"{top.seeds_solved}/{top.seeds_attempted} seeds solved; {verdict}.")
        if top.best_attempt:
            b = top.best_attempt
            src = next((a for a in attempts.get(top.agent_id, [])
                        if a.seed == b.seed and a.attempt == b.attempt), None)
            skill = {"agent_id": top.agent_id, "persona": top.persona, "seed": b.seed, "attempt": b.attempt,
                     "time_s": b.time_s, "video": b.video, "solve_rate": top.solve_rate,
                     "mean_tries_to_solve": top.mean_tries_to_solve,
                     "code": src.code if src else None}
    return {"lines": lines, "final": final,
            "winner": top.agent_id if (top and final) else None,
            "leader": top.agent_id if top else None,
            "skill_eligible": bool(top and top.skill_eligible),
            "skill": skill}


# ---------------------------------------------------------------- compare ---

def race_summary(race: dict[str, Any], scores: list[AgentScore]) -> dict[str, Any]:
    ran = [s for s in scores if s.attempts_total]
    final = race.get("status") == "closed"
    winner = pick_winner(scores, final=True) if final else None
    ahead = leader(scores)
    seeds = sum(s.seeds_attempted for s in ran)
    solved = sum(s.seeds_solved for s in ran)
    firsts = sum(1 for s in ran for o in s.seeds if o.solved_at == 1)
    solved_at = [o.solved_at for s in ran for o in s.seeds if o.solved_at]
    return {
        "race_id": race["race_id"], "label": race.get("label", ""), "task": race.get("task"),
        "status": race.get("status"),
        "agents": len(ran), "attempts": sum(s.attempts_total for s in ran),
        "seeds_attempted": seeds, "seeds_solved": solved,
        "solve_rate": _r(solved / seeds if seeds else 0.0, 4),
        "first_try_rate": _r(firsts / seeds if seeds else 0.0, 4),
        "mean_tries_to_solve": _r(_mean(solved_at), 2),
        "best_score": max((s.score for s in ran), default=None),
        "skill_eligible_agents": sum(1 for s in ran if s.skill_eligible),
        "winner": winner.agent_id if winner else None,
        "leader": ahead.agent_id if ahead else None,
    }


def compare(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"races": summaries}
    if len(summaries) >= 2:
        first, last = summaries[0], summaries[-1]
        out["delta"] = {k: round(last[k] - first[k], 4)
                        for k in ("solve_rate", "first_try_rate", "mean_tries_to_solve", "best_score",
                                  "skill_eligible_agents")
                        if first.get(k) is not None and last.get(k) is not None}
    return out
