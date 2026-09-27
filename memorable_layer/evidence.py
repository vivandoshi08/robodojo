"""The seam to GBrain.

Division of labour, kept deliberately clean so there are two integrations
rather than one duplicated:

* Memorable (here) records raw experience — every attempt, its parameters, its
  outcome, and one line on why it ended that way.
* GBrain distils that into policy and improved skills — the verdict page and
  ``gbrain skillopt``.

The referee calls into this module when it writes the verdict: ``race_episodes``
for the evidence it links, ``skillopt_rows`` for the numbers it trains on.
Nothing here writes to GBrain; that is the referee's side of the seam.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .episode import (
    Episode,
    bearing_bucket,
    distance_bucket,
    params_to_si,
    situation_phrase,
)
from .store import EpisodeStore


def race_episodes(store: EpisodeStore, race_id: str) -> list[dict[str, Any]]:
    """Every attempt in one race, winners and losers, as plain dicts.

    This is the call the referee uses: the winner's successes and the losers'
    failures, each with the parameters and the reason, ready to be linked as
    evidence from the GBrain verdict page.
    """
    return [episode.to_dict() for episode in store.race_episodes(race_id)]


def strategy_summary(episodes: list[Episode]) -> dict[str, Any]:
    """Scoreboard for one strategy: what the referee ranks on."""
    attempts = len(episodes)
    successes = [e for e in episodes if e.success]
    durations = [e.duration_s for e in successes if e.duration_s is not None]
    first_win = min((e.attempt for e in successes), default=None)
    best = min(
        successes,
        key=lambda e: (e.duration_s if e.duration_s is not None else float("inf"), e.attempt),
        default=None,
    )
    return {
        "attempts": attempts,
        "successes": len(successes),
        "success_rate": round(len(successes) / attempts, 4) if attempts else 0.0,
        "knockovers": sum(1 for e in episodes if e.bin_knocked_over),
        "mean_success_duration_s": round(sum(durations) / len(durations), 3) if durations else None,
        "first_success_attempt": first_win,
        "best_params": best.params if best is not None else None,
        "failure_reasons": [
            {"attempt": e.attempt, "outcome": e.outcome.value, "reason": e.reason}
            for e in episodes
            if not e.success
        ],
        "memorable_slugs": sorted({e.memorable_slug for e in episodes if e.memorable_slug}),
        "refusals": sorted({e.memorable_refusal for e in episodes if e.memorable_refusal}),
    }


def rank_strategies(summaries: dict[str, dict[str, Any]]) -> list[str]:
    """Highest success rate first; ties broken by knockovers, then speed, then attempts."""

    def key(item: tuple[str, dict[str, Any]]) -> tuple:
        name, s = item
        duration = s["mean_success_duration_s"]
        return (
            -s["success_rate"],
            s["knockovers"],
            duration if duration is not None else float("inf"),
            s["first_success_attempt"] if s["first_success_attempt"] is not None else 99,
            name,
        )

    return [name for name, _ in sorted(summaries.items(), key=key)]


def distance_policy_hint(store: EpisodeStore, race_id: str | None = None) -> dict[str, Any]:
    """Which strategy wins at which distance, straight from the episodes.

    A *hint*, not a policy: turning this into "toss over 30 cm, drop under" is
    GBrain's job. This just hands over the observed numbers behind it.
    """
    episodes = store.race_episodes(race_id) if race_id else list(store)
    by_bucket: dict[int, dict[str, list[Episode]]] = defaultdict(lambda: defaultdict(list))
    for episode in episodes:
        by_bucket[distance_bucket(episode.bin_distance_cm)][episode.strategy].append(episode)

    buckets: list[dict[str, Any]] = []
    for bucket in sorted(by_bucket):
        rates = {
            strategy: round(
                sum(1 for e in eps if e.success) / len(eps), 4
            )
            for strategy, eps in by_bucket[bucket].items()
        }
        best = max(rates, key=lambda s: (rates[s], s)) if rates else None
        buckets.append(
            {
                "distance_cm": bucket,
                "success_rate_by_strategy": rates,
                "best_strategy": best,
                "attempts": sum(len(eps) for eps in by_bucket[bucket].values()),
            }
        )
    return {"buckets": buckets, "note": "observed rates only; GBrain writes the policy"}


def skillopt_rows(store: EpisodeStore, race_id: str) -> list[dict[str, Any]]:
    """Clean numeric rows for ``gbrain skillopt``.

    One row per (strategy, trash type, distance bucket): attempts, successes,
    success rate, knockovers, mean duration, first-success attempt.
    """
    return store.score_rows(race_id)


def race_evidence(
    store: EpisodeStore, race_id: str, winner: str | None = None
) -> dict[str, Any]:
    """Everything the referee needs to write one verdict page."""
    episodes = store.race_episodes(race_id)
    by_strategy: dict[str, list[Episode]] = defaultdict(list)
    for episode in episodes:
        by_strategy[episode.strategy].append(episode)

    summaries = {name: strategy_summary(eps) for name, eps in by_strategy.items()}
    ranking = rank_strategies(summaries)
    declared = winner or (ranking[0] if ranking else None)

    return {
        "race_id": race_id,
        "scopes": sorted({e.scope for e in episodes}),
        "trash_types": sorted({e.trash_type for e in episodes}),
        "total_attempts": len(episodes),
        "winner": declared,
        "ranking": ranking,
        "by_strategy": summaries,
        "winner_params": summaries.get(declared, {}).get("best_params") if declared else None,
        "distance_policy_hint": distance_policy_hint(store, race_id),
        "skillopt_rows": skillopt_rows(store, race_id),
        "answer_key": answer_key_rows(store, race_id),
        "strategy_record": strategy_record(store),
        "episodes": [e.to_dict() for e in episodes],
    }


def _quantise(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = (
        ordered[mid]
        if len(ordered) % 2
        else round((ordered[mid - 1] + ordered[mid]) / 2, 4)
    )
    return {
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
        "median": round(median, 4),
        "n": len(ordered),
    }


def answer_key_rows(
    store: EpisodeStore, race_id: str | None = None, si: bool = True
) -> list[dict[str, Any]]:
    """Per-situation parameter ranges over throws that actually went in.

    This is the answer key behind a skillopt practice question: given a trash
    type, a bin distance and a bearing, which parameter values are known to
    work. Ranges come only from successful episodes, so an empty list for a
    situation means memory genuinely does not know it yet — that is a real
    signal, not a gap to paper over.

    With ``si`` (the default) the parameter names are the metre-based mirror,
    matching how GBrain phrases questions.
    """
    episodes = store.race_episodes(race_id) if race_id else list(store)
    grouped: dict[tuple, list[Episode]] = defaultdict(list)
    for episode in episodes:
        if not episode.success:
            continue
        grouped[
            (
                episode.strategy,
                episode.trash_type,
                distance_bucket(episode.bin_distance_cm),
                bearing_bucket(episode.bin_bearing_deg),
            )
        ].append(episode)

    rows: list[dict[str, Any]] = []
    for (strategy, trash_type, distance, bearing), eps in sorted(grouped.items()):
        params: dict[str, list[float]] = defaultdict(list)
        for episode in eps:
            source = params_to_si(episode.params) if si else episode.params
            for key, value in source.items():
                params[key].append(float(value))
        rows.append(
            {
                "strategy": strategy,
                "trash_type": trash_type,
                "bin_distance_cm": distance,
                "bin_distance_m": round(distance / 100.0, 3),
                "bin_bearing_deg": bearing,
                "question": situation_phrase(strategy, trash_type, distance, bearing),
                "successes": len(eps),
                "params": {key: _quantise(vals) for key, vals in sorted(params.items())},
                "units": "metres" if si else "centimetres",
                "evidence_episode_ids": [e.episode_id for e in eps],
            }
        )
    return rows


def strategy_record(store: EpisodeStore) -> dict[str, dict[str, Any]]:
    """Each strategy's race record across every race in this store.

    Feeds the pre-race briefing: "won 2 of 3 races". A race counts as won by the
    top of its ranking; a race nobody landed a throw in has no winner.
    """
    races = store.races()
    wins: dict[str, int] = defaultdict(int)
    entered: dict[str, int] = defaultdict(int)
    attempts: dict[str, int] = defaultdict(int)
    successes: dict[str, int] = defaultdict(int)

    for race_id in races:
        episodes = store.race_episodes(race_id)
        by_strategy: dict[str, list[Episode]] = defaultdict(list)
        for episode in episodes:
            by_strategy[episode.strategy].append(episode)
        for strategy, eps in by_strategy.items():
            entered[strategy] += 1
            attempts[strategy] += len(eps)
            successes[strategy] += sum(1 for e in eps if e.success)
        summaries = {name: strategy_summary(eps) for name, eps in by_strategy.items()}
        ranking = rank_strategies(summaries)
        if ranking and summaries[ranking[0]]["successes"] > 0:
            wins[ranking[0]] += 1

    return {
        strategy: {
            "races_entered": entered[strategy],
            "races_won": wins[strategy],
            "record": f"won {wins[strategy]} of {entered[strategy]} races",
            "attempts": attempts[strategy],
            "successes": successes[strategy],
            "success_rate": round(successes[strategy] / attempts[strategy], 4)
            if attempts[strategy]
            else 0.0,
        }
        for strategy in sorted(entered)
    }
