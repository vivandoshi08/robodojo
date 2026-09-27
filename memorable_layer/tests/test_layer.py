"""Tests for the Memorable layer. Standard library only, no network.

The Memorable CLI is replaced by a fake binary, so the suite proves the trace
shape and the fail-open behaviour without spending extraction allowance.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memorable_layer import (  # noqa: E402
    Advisor,
    AttemptRecorder,
    Episode,
    EpisodeStore,
    MemorableClient,
    Outcome,
    Settings,
    answer_key_rows,
    baseline_params,
    build_trace,
    gbrain_race_result,
    gbrain_rows,
    race_episodes,
    race_evidence,
    situation_phrase,
    skillopt_rows,
    strategy_record,
)
from memorable_layer.config import scope_home  # noqa: E402

FAKE_STORED = """#!/bin/sh
case "$1" in
  status) echo "  backend        local"; echo "  write consent  read-write"; echo "  stored 3 procedures"; exit 0;;
  ingest) cat > /dev/null; echo "memorable: stored procedures/abc123-toss-a-bottle, first recording of this task"; exit 0;;
  recall) echo "  0.860  procedures/abc123-toss-a-bottle  [lexical]"; exit 0;;
  show)   echo "## A previous session solved a near-identical task"; exit 0;;
  list)   echo '[]'; exit 0;;
esac
exit 1
"""

FAKE_REFUSED = """#!/bin/sh
case "$1" in
  status) echo "  write consent  read-write"; exit 0;;
  ingest) cat > /dev/null; echo "memorable: not stored: the service did not admit this workflow (no_postcondition, prefilter)"; exit 0;;
esac
exit 1
"""


def make_fake_cli(directory: Path, script: str) -> Path:
    path = directory / "fake-memorable"
    path.write_text(script)
    path.chmod(0o755)
    return path


def episode(**overrides) -> Episode:
    payload = dict(
        race_id="race-1",
        scope="scope-toss",
        strategy="toss",
        attempt=1,
        trash_type="bottle",
        bin_distance_cm=50.0,
        bin_bearing_deg=0.0,
        params={"toss_velocity_mps": 1.7, "release_height_cm": 42.0},
        outcome=Outcome.BOUNCED_OUT,
        reason="released too high, bounced off the far rim",
        duration_s=4.0,
    )
    payload.update(overrides)
    return Episode(**payload)


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def settings(self, binary: tuple[str, ...] = ("false",), **overrides) -> Settings:
        base = dict(
            scope="scope-toss",
            race_id="race-1",
            store_root=self.tmp / "episodes",
            memorable_home=self.tmp / "memhome",
            binary=binary,
            writes_enabled=True,
            timeout_s=10.0,
        )
        base.update(overrides)
        return Settings(**base)


class TestEpisode(TempCase):
    def test_success_requires_standing_bin(self) -> None:
        self.assertTrue(episode(outcome=Outcome.IN).success)
        self.assertFalse(episode(outcome=Outcome.IN, bin_knocked_over=True).success)
        self.assertFalse(episode(outcome=Outcome.RIM_OUT).success)

    def test_situation_is_bucketed_and_shared(self) -> None:
        # 52 cm and 48 cm are the same situation, so they accumulate on one intent.
        self.assertEqual(
            episode(bin_distance_cm=52.0).situation(),
            episode(bin_distance_cm=48.0).situation(),
        )
        self.assertEqual(
            situation_phrase("toss", "bottle", 50.0), episode().situation()
        )

    def test_bearing_appears_in_situation(self) -> None:
        self.assertIn("15 degrees to the left", episode(bin_bearing_deg=-15.0).situation())
        self.assertNotIn("degrees", episode(bin_bearing_deg=0.0).situation())

    def test_si_mirror_for_gbrain(self) -> None:
        data = episode(bin_distance_cm=100.0, params={"release_height_cm": 82.0}).to_dict()
        self.assertEqual(data["bin_distance_m"], 1.0)
        self.assertEqual(data["params_si"], {"release_height_m": 0.82})

    def test_tuned_numbers_reach_the_command_line(self) -> None:
        # Extraction keeps commands verbatim; the numbers must survive into recall.
        command = episode(params={"toss_velocity_mps": 1.45}).sim_command()
        self.assertIn("--toss-velocity-mps 1.45", command)
        self.assertIn("--bin-cm 50", command)

    def test_attempt_is_one_based(self) -> None:
        with self.assertRaises(ValueError):
            episode(attempt=0)

    def test_from_dict_rejects_incomplete_payload(self) -> None:
        with self.assertRaises(ValueError):
            Episode.from_dict({"race_id": "r", "scope": "s"})


class TestStore(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = EpisodeStore(self.tmp / "episodes")

    def test_every_attempt_is_logged_pass_or_fail(self) -> None:
        self.store.append(episode(attempt=1))
        self.store.append(episode(attempt=2, outcome=Outcome.IN, reason="clean"))
        self.assertEqual(len(self.store.race_episodes("race-1")), 2)
        self.assertEqual(self.store.next_attempt_number("race-1", "scope-toss", "toss"), 3)

    def test_similar_is_scope_isolated(self) -> None:
        self.store.append(episode(scope="scope-toss"))
        self.store.append(episode(scope="scope-drop", strategy="toss"))
        mine = self.store.similar("toss", "bottle", 50.0, scope="scope-toss")
        self.assertEqual([e.scope for e in mine], ["scope-toss"])

    def test_similar_respects_bearing(self) -> None:
        self.store.append(episode(bin_bearing_deg=-15.0))
        self.assertEqual(len(self.store.similar("toss", "bottle", 50.0, bearing_deg=-15.0)), 1)
        self.assertEqual(len(self.store.similar("toss", "bottle", 50.0, bearing_deg=45.0)), 0)

    def test_jsonl_and_sqlite_agree(self) -> None:
        self.store.append(episode())
        lines = self.store.jsonl_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["episode_id"], next(iter(self.store)).episode_id)

    def test_merge_from_is_idempotent(self) -> None:
        other = EpisodeStore(self.tmp / "other")
        other.append(episode(scope="scope-drop", strategy="drop"))
        self.assertEqual(self.store.merge_from(other.root), 1)
        self.assertEqual(self.store.merge_from(other.root), 0)


class TestTrace(TempCase):
    def test_trace_has_mutation_and_passing_postcondition(self) -> None:
        # The shape the extraction service actually admits.
        trace = build_trace(episode(attempt=2, outcome=Outcome.IN, reason="clean"))
        names = [call["name"] for call in trace["tool_calls"]]
        self.assertIn("write_file", names)
        self.assertEqual(names[-1], "shell")
        self.assertEqual(trace["tool_calls"][-1]["result"]["exit_code"], 0)
        self.assertIn("PASS", trace["tool_calls"][-1]["result"]["stdout"])
        self.assertEqual(trace["harness"], "robodojo-qm")

    def test_dead_ends_are_carried_as_evidence(self) -> None:
        trace = build_trace(
            episode(attempt=3, outcome=Outcome.IN, reason="clean"),
            dead_ends=[episode(attempt=1), episode(attempt=2)],
        )
        failures = [
            call for call in trace["tool_calls"]
            if call["name"] == "shell" and call["result"]["exit_code"] == 1
        ]
        self.assertEqual(len(failures), 2)
        self.assertIn("bounced off the far rim", failures[0]["result"]["stdout"])

    def test_a_miss_is_never_ingested(self) -> None:
        with self.assertRaises(ValueError):
            build_trace(episode(outcome=Outcome.RIM_OUT))


class TestClient(TempCase):
    def test_reads_fail_open_when_cli_is_missing(self) -> None:
        client = MemorableClient(self.settings(binary=("definitely-not-installed",)))
        self.assertFalse(client.available())
        self.assertEqual(client.recall("anything"), [])
        self.assertEqual(client.show("procedures/x"), "")
        self.assertEqual(client.list_procedures(), [])

    def test_recall_parses_score_slug_method(self) -> None:
        fake = make_fake_cli(self.tmp, FAKE_STORED)
        hits = MemorableClient(self.settings(binary=(str(fake),))).recall("toss a bottle")
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0].score, 0.86)
        self.assertEqual(hits[0].method, "lexical")

    def test_ingest_reports_stored_slug(self) -> None:
        fake = make_fake_cli(self.tmp, FAKE_STORED)
        result = MemorableClient(self.settings(binary=(str(fake),))).ingest({"a": 1})
        self.assertTrue(result.stored)
        self.assertEqual(result.slug, "procedures/abc123-toss-a-bottle")

    def test_ingest_surfaces_refusal_reason(self) -> None:
        fake = make_fake_cli(self.tmp, FAKE_REFUSED)
        result = MemorableClient(self.settings(binary=(str(fake),))).ingest({"a": 1})
        self.assertFalse(result.stored)
        self.assertIn("no_postcondition", result.refusal or "")

    def test_readonly_settings_refuse_writes(self) -> None:
        fake = make_fake_cli(self.tmp, FAKE_STORED)
        settings = self.settings(binary=(str(fake),), writes_enabled=False)
        self.assertFalse(MemorableClient(settings).ingest({"a": 1}).stored)

    def test_namespace_is_pinned_in_child_env(self) -> None:
        env = self.settings().child_env()
        self.assertEqual(env["MEMORABLE_HOME"], str(self.tmp / "memhome"))
        self.assertEqual(env["MEMORABLE_AUTO_INGEST"], "0")


class TestAdvisor(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = EpisodeStore(self.tmp / "episodes")
        self.settings_ = self.settings()
        self.advisor = Advisor(self.store, None, self.settings_)

    def brief(self, **kwargs):
        return self.advisor.brief(
            strategy="toss",
            trash_type="bottle",
            distance_cm=50.0,
            baseline_params=baseline_params("toss", "bottle", 50.0),
            **kwargs,
        )

    def test_cold_start_uses_baseline(self) -> None:
        brief = self.brief()
        self.assertEqual(brief.prior_attempts, 0)
        self.assertEqual(brief.deltas, {})
        self.assertIn("no prior experience", brief.rationale)

    def test_overshoot_lowers_velocity_and_says_so(self) -> None:
        self.store.append(episode(attempt=1, outcome=Outcome.BOUNCED_OUT))
        self.store.append(episode(attempt=2, outcome=Outcome.BOUNCED_OUT))
        brief = self.brief()
        baseline = baseline_params("toss", "bottle", 50.0)["toss_velocity_mps"]
        self.assertLess(brief.suggested_params["toss_velocity_mps"], baseline)
        self.assertIn("2 overshoot episode(s)", brief.rationale)
        self.assertIn("dropping velocity 15%", brief.channel_line)
        self.assertEqual(brief.attempt, 3)

    def test_knockover_takes_priority_over_overshoot(self) -> None:
        self.store.append(
            episode(attempt=1, outcome=Outcome.RIM_OUT, bin_knocked_over=True)
        )
        brief = self.brief()
        baseline = baseline_params("toss", "bottle", 50.0)
        self.assertLess(
            brief.suggested_params["toss_velocity_mps"], baseline["toss_velocity_mps"] * 0.85
        )
        self.assertIn("knockover", brief.rationale)

    def test_short_throw_raises_velocity(self) -> None:
        self.store.append(episode(attempt=1, outcome=Outcome.MISS, reason="fell short"))
        brief = self.brief()
        self.assertGreater(
            brief.suggested_params["toss_velocity_mps"],
            baseline_params("toss", "bottle", 50.0)["toss_velocity_mps"],
        )

    def test_a_past_success_beats_the_heuristic(self) -> None:
        winning = {"toss_velocity_mps": 1.45, "release_height_cm": 30.0}
        self.store.append(
            episode(attempt=2, outcome=Outcome.IN, reason="clean", params=winning)
        )
        brief = self.brief()
        self.assertEqual(brief.suggested_params["toss_velocity_mps"], 1.45)
        self.assertIn("reusing the parameters that worked", brief.rationale)


class TestRecorder(TempCase):
    def recorder(self, script: str = FAKE_STORED) -> AttemptRecorder:
        fake = make_fake_cli(self.tmp, script)
        settings = self.settings(binary=(str(fake),))
        return AttemptRecorder(settings=settings, client=MemorableClient(settings))

    def test_miss_is_logged_but_not_ingested(self) -> None:
        recorder = self.recorder()
        result = recorder.record(episode(attempt=1, outcome=Outcome.RIM_OUT))
        self.assertFalse(result["ingested"])
        self.assertIn("kept as episode evidence", result["refusal"])
        self.assertEqual(len(recorder.store.race_episodes("race-1")), 1)

    def test_landing_attempt_ingests_with_earlier_misses_as_dead_ends(self) -> None:
        recorder = self.recorder()
        recorder.record(episode(attempt=1, outcome=Outcome.BOUNCED_OUT))
        recorder.record(episode(attempt=2, outcome=Outcome.RIM_OUT))
        result = recorder.record(episode(attempt=3, outcome=Outcome.IN, reason="clean"))
        self.assertTrue(result["ingested"])
        self.assertEqual(result["slug"], "procedures/abc123-toss-a-bottle")
        self.assertEqual(result["dead_ends"], [1, 2])

    def test_refusal_is_recorded_on_the_episode(self) -> None:
        recorder = self.recorder(FAKE_REFUSED)
        result = recorder.record(episode(attempt=1, outcome=Outcome.IN, reason="clean"))
        self.assertFalse(result["ingested"])
        stored = recorder.store.race_episodes("race-1")[0]
        self.assertIn("no_postcondition", stored.memorable_refusal or "")

    def test_a_dead_memorable_never_breaks_recording(self) -> None:
        settings = self.settings(binary=("definitely-not-installed",))
        recorder = AttemptRecorder(settings=settings, client=MemorableClient(settings))
        result = recorder.record(episode(attempt=1, outcome=Outcome.IN, reason="clean"))
        self.assertFalse(result["ingested"])
        self.assertEqual(len(recorder.store.race_episodes("race-1")), 1)


class TestEvidence(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = EpisodeStore(self.tmp / "episodes")
        # toss lands on attempt 2; drop never lands and knocks the bin over.
        self.store.append(episode(attempt=1, outcome=Outcome.BOUNCED_OUT))
        self.store.append(
            episode(
                attempt=2,
                outcome=Outcome.IN,
                reason="clean",
                params={"toss_velocity_mps": 1.45, "release_height_cm": 30.0},
                duration_s=4.2,
            )
        )
        for attempt in (1, 2):
            self.store.append(
                episode(
                    scope="scope-drop",
                    strategy="drop",
                    attempt=attempt,
                    outcome=Outcome.RIM_OUT,
                    reason="dropped onto the rim",
                    bin_knocked_over=True,
                )
            )

    def test_race_episodes_returns_winners_and_losers(self) -> None:
        rows = race_episodes(self.store, "race-1")
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["strategy"] for row in rows}, {"toss", "drop"})

    def test_winner_is_the_strategy_that_landed(self) -> None:
        bundle = race_evidence(self.store, "race-1")
        self.assertEqual(bundle["winner"], "toss")
        self.assertEqual(bundle["ranking"][0], "toss")
        self.assertEqual(bundle["winner_params"]["toss_velocity_mps"], 1.45)
        self.assertEqual(bundle["by_strategy"]["drop"]["knockovers"], 2)
        self.assertEqual(len(bundle["by_strategy"]["drop"]["failure_reasons"]), 2)

    def test_explicit_winner_is_respected(self) -> None:
        self.assertEqual(race_evidence(self.store, "race-1", winner="drop")["winner"], "drop")

    def test_answer_key_covers_only_successful_throws(self) -> None:
        rows = answer_key_rows(self.store, "race-1")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strategy"], "toss")
        self.assertEqual(row["units"], "metres")
        self.assertEqual(row["params"]["release_height_m"]["median"], 0.3)
        self.assertEqual(row["bin_distance_m"], 0.5)
        self.assertEqual(row["successes"], 1)

    def test_skillopt_rows_are_flat_numbers(self) -> None:
        rows = skillopt_rows(self.store, "race-1")
        by_strategy = {row["strategy"]: row for row in rows}
        self.assertEqual(by_strategy["toss"]["attempts"], 2)
        self.assertEqual(by_strategy["toss"]["successes"], 1)
        self.assertEqual(by_strategy["drop"]["success_rate"], 0.0)
        self.assertEqual(by_strategy["toss"]["first_success_attempt"], 2)

    def test_strategy_record_reads_as_won_n_of_m(self) -> None:
        record = strategy_record(self.store)
        self.assertEqual(record["toss"]["record"], "won 1 of 1 races")
        self.assertEqual(record["drop"]["races_won"], 0)

    def test_policy_hint_names_the_best_strategy_per_distance(self) -> None:
        hint = race_evidence(self.store, "race-1")["distance_policy_hint"]
        self.assertEqual(hint["buckets"][0]["distance_cm"], 50)
        self.assertEqual(hint["buckets"][0]["best_strategy"], "toss")


class TestScopeIsolation(TempCase):
    def test_scope_home_sanitises_and_separates(self) -> None:
        a = scope_home(self.tmp, "qm/scope:toss")
        b = scope_home(self.tmp, "qm/scope:drop")
        self.assertNotEqual(a, b)
        self.assertNotIn("/", a.name)


class TestCli(TempCase):
    def run_cli(self, *args: str) -> dict:
        env = dict(os.environ)
        env.update(
            ROBODOJO_MEMORY_ROOT=str(self.tmp / "episodes"),
            ROBODOJO_RACE_ID="race-cli",
            ROBODOJO_SCOPE="scope-cli",
            MEMORABLE_HOME=str(self.tmp / "memhome"),
            ROBODOJO_MEMORABLE_BIN="definitely-not-installed",
        )
        proc = subprocess.run(
            [sys.executable, "-m", "memorable_layer", *args],
            capture_output=True, text=True, env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_fixture_then_evidence_round_trip(self) -> None:
        written = self.run_cli("fixture", "--races", "2", "--attempts", "3")
        self.assertTrue(written["fixture"])
        self.assertEqual(written["episodes_written"], 24)
        race = written["races"][0]
        bundle = self.run_cli("evidence", "--race", race)
        self.assertIn(bundle["winner"], {"toss", "drop", "pick_place", "push_off_edge", None})
        self.assertEqual(bundle["total_attempts"], 12)

    def test_record_then_brief_uses_the_new_episode(self) -> None:
        payload = json.dumps(
            {
                "strategy": "toss", "trash_type": "bottle", "bin_distance_cm": 50.0,
                "params": {"toss_velocity_mps": 1.7, "release_height_cm": 42.0},
                "outcome": "bounced_out", "reason": "overshot the bin",
            }
        )
        proc = subprocess.run(
            [sys.executable, "-m", "memorable_layer", "record", "-"],
            input=payload, capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[2]),
            env={
                **os.environ,
                "ROBODOJO_MEMORY_ROOT": str(self.tmp / "episodes"),
                "ROBODOJO_RACE_ID": "race-cli",
                "ROBODOJO_SCOPE": "scope-cli",
                "ROBODOJO_MEMORABLE_BIN": "definitely-not-installed",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["success"], False)
        brief = self.run_cli(
            "brief", "--strategy", "toss", "--trash", "bottle", "--bin-cm", "50",
            "--race", "race-cli",
        )
        self.assertEqual(brief["attempt"], 2)
        self.assertIn("overshoot", brief["rationale"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAdvisorRegressions(TempCase):
    """A known-good parameter set must not be eroded by superseded misses."""

    def setUp(self) -> None:
        super().setUp()
        self.store = EpisodeStore(self.tmp / "episodes")
        self.advisor = Advisor(self.store, None, self.settings())

    def brief(self):
        return self.advisor.brief(
            strategy="toss",
            trash_type="bottle",
            distance_cm=50.0,
            baseline_params=baseline_params("toss", "bottle", 50.0),
        )

    def test_success_after_misses_is_held_not_cut_again(self) -> None:
        winning = {"toss_velocity_mps": 1.45, "release_height_cm": 30.0}
        self.store.append(episode(attempt=1, outcome=Outcome.BOUNCED_OUT))
        self.store.append(episode(attempt=2, outcome=Outcome.RIM_OUT))
        self.store.append(
            episode(attempt=3, outcome=Outcome.IN, reason="clean", params=winning)
        )
        brief = self.brief()
        self.assertEqual(brief.suggested_params["toss_velocity_mps"], 1.45)
        self.assertEqual(brief.suggested_params["release_height_cm"], 30.0)
        self.assertEqual(brief.deltas, {})
        self.assertIn("nothing has missed since", brief.rationale)

    def test_a_miss_after_the_success_steers_again(self) -> None:
        winning = {"toss_velocity_mps": 1.45, "release_height_cm": 30.0}
        self.store.append(
            episode(attempt=1, outcome=Outcome.IN, reason="clean", params=winning)
        )
        self.store.append(
            episode(attempt=2, outcome=Outcome.BOUNCED_OUT, reason="overshot", params=winning)
        )
        brief = self.brief()
        self.assertLess(brief.suggested_params["toss_velocity_mps"], 1.45)
        self.assertIn("1 overshoot episode(s)", brief.rationale)


class TestGBrainContract(TempCase):
    """The row shape the GBrain side's ``fromRow`` maps, checked field by field.

    Its verdict renderer calls ``.toFixed()`` on every key in PARAM_KEYS and sums
    ``score`` for standings, so a missing parameter or score breaks its page.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store = EpisodeStore(self.tmp / "episodes")

    def test_row_carries_every_field_fromrow_reads(self) -> None:
        row = episode(
            attempt=2, outcome=Outcome.IN, reason="clean", bin_bearing_deg=-15.0,
            params={"release_height_cm": 82.0, "toss_velocity_mps": 1.9, "grasp_angle_deg": 90.0},
        ).to_gbrain_row()
        for key in ("episode_id", "race_id", "agent_id", "strategy", "attempt",
                    "trash_type", "bin_position", "params", "outcome", "score", "reason"):
            self.assertIn(key, row)
        self.assertEqual(row["bin_position"], {"distance_m": 0.5, "angle_deg": -15.0})
        self.assertEqual(row["bin_angle_deg"], -15.0)
        self.assertEqual(row["agent_id"], "scope-toss")

    def test_all_three_param_keys_are_always_present(self) -> None:
        # push_off_edge has no toss velocity; the key must still be there as 0.0
        # or the renderer throws on undefined.toFixed().
        row = episode(
            strategy="push_off_edge",
            params=baseline_params("push_off_edge", "bottle", 50.0),
        ).to_gbrain_row()
        for key in ("release_height_m", "toss_velocity_mps", "grasp_angle_deg"):
            self.assertIn(key, row["params"])
            self.assertIsInstance(row["params"][key], float)
        self.assertEqual(row["params"]["toss_velocity_mps"], 0.0)

    def test_every_baseline_carries_grasp_angle(self) -> None:
        for strategy in ("toss", "drop", "pick_place", "push_off_edge"):
            self.assertIn("grasp_angle_deg", baseline_params(strategy, "can", 50.0))

    def test_outcome_vocabulary_is_translated(self) -> None:
        cases = {
            Outcome.IN: "in_bin", Outcome.RIM_OUT: "rim_out", Outcome.BOUNCED_OUT: "long",
            Outcome.MISS: "short", Outcome.WIDE: "wide", Outcome.NO_GRASP: "dropped",
            Outcome.DROPPED_EARLY: "dropped",
        }
        for mine, theirs in cases.items():
            self.assertEqual(episode(outcome=mine).to_gbrain_row()["outcome"], theirs)

    def test_lengths_are_metres_not_centimetres(self) -> None:
        row = episode(bin_distance_cm=100.0, params={"release_height_cm": 82.0}).to_gbrain_row()
        self.assertEqual(row["bin_position"]["distance_m"], 1.0)
        self.assertEqual(row["params"]["release_height_m"], 0.82)

    def test_score_orders_outcomes_sensibly(self) -> None:
        landed = episode(outcome=Outcome.IN, reason="clean", duration_s=4.0).score
        near = episode(outcome=Outcome.RIM_OUT).score
        missed = episode(outcome=Outcome.MISS).score
        knocked = episode(outcome=Outcome.IN, reason="clean", bin_knocked_over=True).score
        self.assertGreater(landed, near)
        self.assertGreater(near, missed)
        # A knockover forfeits everything, so it ranks below even a plain miss —
        # the same rule as Episode.success, so standings cannot crown a strategy
        # that wrecks the bin.
        self.assertLess(knocked, missed)
        self.assertEqual(knocked, -5.0)

    def test_faster_success_scores_higher(self) -> None:
        self.assertGreater(
            episode(outcome=Outcome.IN, reason="clean", duration_s=2.0).score,
            episode(outcome=Outcome.IN, reason="clean", duration_s=9.0).score,
        )

    def test_contract_filter_is_winner_wins_plus_others_losses(self) -> None:
        self.store.append(episode(attempt=1, outcome=Outcome.BOUNCED_OUT))
        self.store.append(episode(attempt=2, outcome=Outcome.IN, reason="clean"))
        self.store.append(
            episode(scope="scope-drop", strategy="drop", attempt=1, outcome=Outcome.RIM_OUT)
        )
        rows = gbrain_rows(self.store, "race-1", contract=True)
        self.assertEqual(
            {(r["strategy"], r["outcome"]) for r in rows},
            {("toss", "in_bin"), ("drop", "rim_out")},
        )

    def test_race_result_standings_sum_scores_by_agent(self) -> None:
        self.store.append(episode(attempt=1, outcome=Outcome.BOUNCED_OUT))
        self.store.append(episode(attempt=2, outcome=Outcome.IN, reason="clean", duration_s=4.0))
        self.store.append(
            episode(scope="scope-drop", strategy="drop", attempt=1, outcome=Outcome.RIM_OUT)
        )
        result = gbrain_race_result(self.store, "race-1")
        self.assertEqual(result["agents"][0]["agent_id"], "scope-toss")
        self.assertEqual(result["agents"][0]["successes"], 1)
        self.assertEqual(result["agents"][0]["attempts"], 2)
        self.assertIn("won on 1 of 2 attempts", result["summary"])

    def test_agent_id_survives_a_store_round_trip(self) -> None:
        self.store.append(episode(agent_id="agent-toss-1"))
        self.assertEqual(self.store.race_episodes("race-1")[0].agent_id, "agent-toss-1")
