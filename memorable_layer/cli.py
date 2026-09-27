"""``python -m memorable_layer`` — the surface a QM agent calls through ``execute``.

Every subcommand takes flags or JSON on stdin and prints JSON on stdout, so a
sandboxed agent, the referee, or a shell script can all use the same entry point.

    memorable-layer brief    --strategy toss --trash bottle --bin-cm 50
    memorable-layer record   -            # episode JSON on stdin
    memorable-layer episodes --race race-1
    memorable-layer evidence --race race-1 --winner toss
    memorable-layer answer-key --race race-1
    memorable-layer status
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

from .advisor import baseline_params
from .client import MemorableClient
from .config import STRATEGIES, TRASH_TYPES, Settings
from .episode import Episode, Outcome
from .evidence import (
    answer_key_rows,
    distance_policy_hint,
    race_episodes,
    race_evidence,
    skillopt_rows,
    strategy_record,
)
from .recorder import AttemptRecorder
from .store import EpisodeStore


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=False, default=str)
    sys.stdout.write("\n")


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    overrides: dict[str, Any] = {}
    if getattr(args, "scope", None):
        overrides["scope"] = args.scope
    if getattr(args, "race", None):
        overrides["race_id"] = args.race
    if getattr(args, "store_root", None):
        overrides["store_root"] = Path(args.store_root).expanduser()
    if getattr(args, "memorable_home", None):
        overrides["memorable_home"] = Path(args.memorable_home).expanduser()
    return Settings(**{**settings.__dict__, **overrides}) if overrides else settings


def _recorder(args: argparse.Namespace) -> AttemptRecorder:
    settings = _settings(args)
    client = None if getattr(args, "no_memorable", False) else MemorableClient(settings)
    return AttemptRecorder(settings=settings, client=client)


def _read_json(source: str) -> Any:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return json.loads(text)


# -- commands -------------------------------------------------------------


def cmd_brief(args: argparse.Namespace) -> int:
    recorder = _recorder(args)
    params = json.loads(args.params) if args.params else None
    brief = recorder.brief(
        strategy=args.strategy,
        trash_type=args.trash,
        bin_distance_cm=args.bin_cm,
        bin_bearing_deg=args.bin_bearing_deg,
        params=params,
    )
    if args.line:
        print(brief.channel_line)
    else:
        _emit(brief.to_dict())
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    recorder = _recorder(args)
    payload = _read_json(args.episode)
    payload.setdefault("race_id", recorder.settings.race_id)
    payload.setdefault("scope", recorder.settings.scope)
    payload.setdefault(
        "attempt",
        recorder.store.next_attempt_number(
            payload["race_id"], payload["scope"], payload["strategy"]
        ),
    )
    episode = Episode.from_dict(payload)
    _emit(recorder.record(episode, ingest=not args.no_ingest))
    return 0


def cmd_episodes(args: argparse.Namespace) -> int:
    store = EpisodeStore(_settings(args).store_root)
    _emit(race_episodes(store, args.race))
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    store = EpisodeStore(_settings(args).store_root)
    _emit(race_evidence(store, args.race, winner=args.winner))
    return 0


def cmd_skillopt_rows(args: argparse.Namespace) -> int:
    store = EpisodeStore(_settings(args).store_root)
    _emit(skillopt_rows(store, args.race))
    return 0


def cmd_answer_key(args: argparse.Namespace) -> int:
    store = EpisodeStore(_settings(args).store_root)
    _emit(answer_key_rows(store, args.race, si=not args.cm))
    return 0


def cmd_record_card(args: argparse.Namespace) -> int:
    store = EpisodeStore(_settings(args).store_root)
    _emit(strategy_record(store))
    return 0


def cmd_policy_hint(args: argparse.Namespace) -> int:
    store = EpisodeStore(_settings(args).store_root)
    _emit(distance_policy_hint(store, args.race))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = EpisodeStore(settings.store_root)
    client = MemorableClient(settings)
    _emit(
        {
            "scope": settings.scope,
            "race_id": settings.race_id,
            "store_root": str(settings.store_root),
            "episodes": sum(1 for _ in store),
            "races": store.races(),
            "writes_enabled": settings.writes_enabled,
            "memorable": client.status(),
        }
    )
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """Merge per-scope stores into one. How the referee sees the whole race."""
    store = EpisodeStore(_settings(args).store_root)
    merged = {str(path): store.merge_from(Path(path)) for path in args.sources}
    _emit({"merged": merged, "episodes": sum(1 for _ in store)})
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Replay a race's winning attempts into a shared backend after the verdict.

    During a race each competitor writes to its own namespace, which is the
    fairness guarantee. Afterwards the winner's experience should be shared, so
    this re-ingests it against whichever ``MEMORABLE_HOME`` is given.
    """
    settings = _settings(args)
    store = EpisodeStore(settings.store_root)
    target = Settings(**{**settings.__dict__, "memorable_home": Path(args.into).expanduser()})
    client = MemorableClient(target)
    from .trace import build_trace

    promoted = []
    for episode in store.race_episodes(args.race, strategy=args.winner):
        if not episode.success:
            continue
        result = client.ingest(build_trace(episode))
        promoted.append(
            {"episode_id": episode.episode_id, "attempt": episode.attempt, **result.to_dict()}
        )
    _emit({"race": args.race, "winner": args.winner, "into": args.into, "promoted": promoted})
    return 0


def cmd_fixture(args: argparse.Namespace) -> int:
    """Generate real-shaped episodes without a simulator.

    So the GBrain side can be built and tested against the real schema before
    the sim is wired in. Marked ``fixture: true`` in ``extra`` — never mistake
    this for measured data.
    """
    settings = _settings(args)
    store = EpisodeStore(settings.store_root)
    rng = random.Random(args.seed)
    written = 0

    for race_index in range(1, args.races + 1):
        race_id = f"{args.race_prefix}{race_index}"
        trash_type = TRASH_TYPES[(race_index - 1) % len(TRASH_TYPES)]
        distance = rng.choice([30.0, 50.0, 70.0, 100.0])
        bearing = rng.choice([-15.0, 0.0, 15.0])
        # Whichever strategy suits the distance tends to win, so fixture races
        # produce a policy hint with the same shape real races would.
        favoured = "toss" if distance >= 50 else "drop"
        for strategy in STRATEGIES:
            params = baseline_params(strategy, trash_type, distance)
            for attempt in range(1, args.attempts + 1):
                lands = strategy == favoured and attempt >= 2 and rng.random() > 0.2
                if lands:
                    outcome, reason = Outcome.IN, "landed clean inside the bin"
                elif attempt == 1:
                    outcome, reason = (
                        Outcome.BOUNCED_OUT,
                        f"{trash_type} released too high, bounced off the far rim",
                    )
                else:
                    outcome, reason = (
                        Outcome.RIM_OUT,
                        f"{trash_type} clipped the near rim and fell outside",
                    )
                episode = Episode(
                    race_id=race_id,
                    scope=f"scope-{strategy}",
                    strategy=strategy,
                    attempt=attempt,
                    trash_type=trash_type,
                    bin_distance_cm=distance,
                    bin_bearing_deg=bearing,
                    params={k: round(v * rng.uniform(0.95, 1.05), 3) for k, v in params.items()},
                    outcome=outcome,
                    reason=reason,
                    bin_knocked_over=outcome is Outcome.RIM_OUT and rng.random() > 0.85,
                    duration_s=round(rng.uniform(3.0, 7.5), 2),
                    seed=rng.randint(1, 9999),
                    extra={"fixture": True},
                )
                store.append(episode)
                written += 1

    _emit(
        {
            "fixture": True,
            "episodes_written": written,
            "races": store.races(),
            "store_root": str(settings.store_root),
        }
    )
    return 0


# -- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memorable-layer", description=__doc__)
    parser.add_argument("--scope", help="QM scope id (defaults to $ROBODOJO_SCOPE)")
    parser.add_argument("--store-root", help="episode log directory")
    parser.add_argument("--memorable-home", help="MEMORABLE_HOME for this namespace")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("brief", help="recall and suggest parameters for the next attempt")
    p.add_argument("--strategy", required=True)
    p.add_argument("--trash", required=True, choices=list(TRASH_TYPES))
    p.add_argument("--bin-cm", type=float, required=True)
    p.add_argument("--bin-bearing-deg", type=float, default=0.0)
    p.add_argument("--params", help="baseline parameters as JSON")
    p.add_argument("--race")
    p.add_argument("--line", action="store_true", help="print only the QM channel line")
    p.add_argument("--no-memorable", action="store_true", help="episode log only")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("record", help="record an attempt, ingest it if it landed")
    p.add_argument("episode", nargs="?", default="-", help="episode JSON file, or -")
    p.add_argument("--race")
    p.add_argument("--no-ingest", action="store_true")
    p.add_argument("--no-memorable", action="store_true")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("episodes", help="every attempt in a race (the GBrain seam)")
    p.add_argument("--race", required=True)
    p.set_defaults(func=cmd_episodes)

    p = sub.add_parser("evidence", help="verdict bundle for the referee's GBrain page")
    p.add_argument("--race", required=True)
    p.add_argument("--winner")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("skillopt-rows", help="clean numeric rows for gbrain skillopt")
    p.add_argument("--race", required=True)
    p.set_defaults(func=cmd_skillopt_rows)

    p = sub.add_parser("answer-key", help="parameter ranges from throws that went in")
    p.add_argument("--race")
    p.add_argument("--cm", action="store_true", help="centimetres instead of metres")
    p.set_defaults(func=cmd_answer_key)

    p = sub.add_parser("strategy-record", help='per-strategy "won 2 of 3 races"')
    p.set_defaults(func=cmd_record_card)

    p = sub.add_parser("policy-hint", help="observed best strategy per distance bucket")
    p.add_argument("--race")
    p.set_defaults(func=cmd_policy_hint)

    p = sub.add_parser("status", help="layer and Memorable state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("collect", help="merge per-scope episode logs")
    p.add_argument("sources", nargs="+")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("promote", help="share the winner's experience after the verdict")
    p.add_argument("--race", required=True)
    p.add_argument("--winner", required=True)
    p.add_argument("--into", required=True, help="target MEMORABLE_HOME")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("fixture", help="generate real-shaped episodes for testing")
    p.add_argument("--races", type=int, default=3)
    p.add_argument("--attempts", type=int, default=4)
    p.add_argument("--race-prefix", default="fixture-race-")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_fixture)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
