import math

from metrics import classify, enrich, error_headline, landing_error
from models import Attempt, FailureMode, RawObservation, ScoringConfig
from scoring import aggregate, leader, lessons, pick_winner, rank, seed_outcomes

CFG = ScoringConfig(seeds_per_agent=2, max_attempts=5)


def att(seed, attempt, success, agent="agent-1", **kw):
    return enrich(Attempt(agent_id=agent, seed=seed, attempt=attempt, success=success, **kw), CFG)


def test_classify_from_result_json_fields():
    assert classify(Attempt(seed=0, success=True), CFG) == FailureMode.none
    assert classify(Attempt(seed=0, success=False, error="Traceback...\nNameError: name 'np' is not defined"),
                    CFG) == FailureMode.code_error
    assert classify(Attempt(seed=0, success=False, error="policy timed out after 30 s"), CFG) == FailureMode.timeout
    assert classify(Attempt(seed=0, success=False, dropped=True, collisions=2), CFG) == FailureMode.dropped
    assert classify(Attempt(seed=0, success=False, collisions=1), CFG) == FailureMode.collision
    assert classify(Attempt(seed=0, success=False), CFG) == FailureMode.missed


def test_error_headline_takes_last_traceback_line():
    assert error_headline("Traceback (most recent call last):\n  File x\nKeyError: 'bottle'\n") == "KeyError: 'bottle'"
    assert error_headline(None) is None


def test_extra_result_json_fields_are_kept():
    a = Attempt.model_validate({"seed": 0, "success": True, "new_executor_field": 3})
    assert a.model_dump()["new_executor_field"] == 3


def test_throw_variant_landing_error():
    e = landing_error(RawObservation(bin_xy=(1.0, 0.0), crossing_xy=(0.9, 0.05)))
    assert math.isclose(e.along, -0.10, abs_tol=1e-6) and math.isclose(e.across, 0.05, abs_tol=1e-6)
    a = att(0, 1, False, raw={"bin_xy": [1, 0], "crossing_xy": [0.7, 0.0]})
    assert a.failure_mode == FailureMode.short


def test_seed_outcomes_stop_at_first_success_and_mark_done():
    tries = [att(0, 1, False, dropped=True), att(0, 2, True, time_s=9.0), att(0, 3, False),   # try 3 ignored
             att(1, 1, False), att(1, 2, False)]                                              # still going
    out = {o.seed: (o, used, final) for o, used, final in seed_outcomes(tries, CFG)}
    assert out[0][0].solved and out[0][0].solved_at == 2 and out[0][0].done
    assert len(out[0][1]) == 2 and out[0][2].attempt == 2
    assert not out[1][0].solved and not out[1][0].done
    five_fails = [att(2, k, False) for k in range(1, 6)]
    o, _, _ = seed_outcomes(five_fails, CFG)[0]
    assert o.done and not o.solved


def test_score_formula():
    # seed 0 solved on try 1 (10 s, energy 20, 0 collisions); seed 1 solved on try 3 (14 s, energy 40, 1 collision)
    tries = [att(0, 1, True, time_s=10.0, energy=20.0),
             att(1, 1, False, dropped=True), att(1, 2, False, error="ValueError: bad xyz"),
             att(1, 3, True, time_s=14.0, energy=40.0, collisions=1)]
    s = aggregate("agent-1", tries, CFG, {"persona": "careful"})
    # 100*1.0 - 10*mean(0, 2) - 1*mean(10, 14) - 10*mean(0, 1) - 0.1*mean(20, 40) = 100 - 10 - 12 - 5 - 3 = 70
    assert s.score == 70.0
    assert s.solve_rate == 1.0 and s.first_try_rate == 0.5 and s.mean_tries_to_solve == 2.0
    assert s.complete and s.skill_eligible and s.attempts_total == 4
    assert s.failure_modes == {"dropped": 1, "code_error": 1}
    assert s.top_error == "ValueError: bad xyz"
    assert s.best_attempt.seed == 0 and s.best_attempt.attempt == 1


def test_winner_needs_complete_agent_leader_does_not():
    full = aggregate("a", [att(0, 1, True, "a", time_s=12), att(1, 1, True, "a", time_s=12)], CFG)
    partial = aggregate("b", [att(0, 1, True, "b", time_s=5)], CFG)
    assert pick_winner([full, partial]).agent_id == "a"
    assert leader([full, partial]).agent_id == "b"
    assert pick_winner([partial]) is None and pick_winner([partial], final=True).agent_id == "b"
    assert [s.agent_id for s in rank([partial, full])] == ["b", "a"]


def test_lessons_return_winning_code_as_skill():
    code = "robot.move_to(state['objects']['can'])"
    tries = {"a": [att(0, 1, False, "a", dropped=True), att(0, 2, True, "a", time_s=11, code=code),
                   att(1, 1, True, "a", time_s=12, code=code + "  # v2")]}
    scores = [aggregate("a", tries["a"], CFG, {"persona": "careful"})]
    out = lessons(scores, tries, CFG, final=True)
    assert out["winner"] == "a" and out["skill_eligible"]
    assert out["skill"]["code"] == code and out["skill"]["seed"] == 0
    assert any("solved 2/2 seeds" in l for l in out["lines"])
    assert any(l.startswith("Winner") for l in out["lines"])
