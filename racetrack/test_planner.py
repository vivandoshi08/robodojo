import json
from types import SimpleNamespace

import pytest

from planner import (load_strategy_prompt, load_task, memory_from_tracker, plan_strategies,
                     strategy_prompt, validate)

TASK = load_task("tasks/can_to_bin.json")
AXES = [{"name": "grasp", "why_it_matters": "slip", "options": ["top-down", "side"]},
        {"name": "release", "why_it_matters": "bounce", "options": ["drop from height", "lower then open"]}]


def strat(name, grasp, release, mode="explore"):
    return {"name": name, "mode": mode, "choices": {"grasp": grasp, "release": release},
            "approach": f"{name}: move above the can, grasp {grasp}, carry to the bin, {release}.",
            "starting_parameters": {"speed": 0.15}, "risks": "can slips during transport"}


class FakeClient:
    """Stands in for anthropic.Anthropic(): returns queued submit_plan inputs, records calls."""
    def __init__(self, *plans):
        self.plans, self.calls = list(plans), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        block = SimpleNamespace(type="tool_use", id=f"toolu_{len(self.calls)}", name="submit_plan",
                                input=self.plans.pop(0))
        return SimpleNamespace(content=[block])


def good_plan(modes=("explore",) * 4):
    combos = [("top-down", "drop from height"), ("top-down", "lower then open"),
              ("side", "drop from height"), ("side", "lower then open")]
    return {"axes": AXES, "coverage_rationale": "all four combinations",
            "strategies": [strat(f"S{i}", g, r, m) for i, ((g, r), m) in enumerate(zip(combos, modes))]}


def test_plan_assigns_agents_and_forces_the_tool():
    client = FakeClient(good_plan())
    plan = plan_strategies(TASK, 4, client=client)
    assert [s["agent_id"] for s in plan["strategies"]] == ["agent-1", "agent-2", "agent-3", "agent-4"]
    assert plan["warnings"] == [] and len(client.calls) == 1
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "submit_plan"}
    assert "no memory" in call["system"] and "4 robot agents" in call["system"]
    text = call["messages"][0]["content"][-1]["text"]
    assert "move_to" in text and "Pick up the can" in text        # task + API reach the model


def test_duplicate_strategies_trigger_one_repair():
    bad = good_plan()
    bad["strategies"][1]["choices"] = dict(bad["strategies"][0]["choices"])
    client = FakeClient(bad, good_plan())
    plan = plan_strategies(TASK, 4, client=client)
    assert len(client.calls) == 2 and plan["warnings"] == []
    repair = client.calls[1]["messages"][-1]["content"][0]
    assert repair["type"] == "tool_result" and repair["is_error"] and "same choice" in repair["content"]


def test_memory_with_skill_requires_exploit_split():
    memory = {"lessons": ["S0: solved 2/3 seeds; failed tries: 1 dropped"],
              "skills": [{"persona": "S0", "code": "robot.open_gripper()", "solve_rate": 0.67,
                          "mean_tries_to_solve": 2.0, "race_id": "race-001"}]}
    client = FakeClient(good_plan(("exploit", "exploit", "explore", "explore")))
    plan = plan_strategies(TASK, 4, memory=memory, client=client)
    assert plan["warnings"] == [] and plan["used_memory"]
    assert "Exactly 2 strategies" in client.calls[0]["system"]
    assert "robot.open_gripper()" in client.calls[0]["messages"][0]["content"][-1]["text"]


def test_validate_catches_count_missing_axis_and_modes():
    p = good_plan()
    p["strategies"][0]["choices"].pop("release")
    problems = validate(p, 5, 1)
    assert any("expected 5" in x for x in problems)
    assert any("no choice for axes: release" in x for x in problems)
    assert any("exploit" in x for x in problems)


def test_image_is_sent_when_given(tmp_path):
    img = tmp_path / "scene.png"
    img.write_bytes(b"\x89PNG fake")
    client = FakeClient(good_plan())
    plan_strategies(TASK, 4, image=str(img), client=client)
    first = client.calls[0]["messages"][0]["content"][0]
    assert first["type"] == "image" and first["source"]["media_type"] == "image/png"


def test_strategy_prompt_and_loading(tmp_path):
    plan = plan_strategies(TASK, 4, client=FakeClient(good_plan()))
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    text = load_strategy_prompt(str(p), "agent-3")
    assert "S2" in text and "- grasp: side" in text and "speed=0.15" in text
    assert "rather than switching" in text
    with pytest.raises(KeyError):
        load_strategy_prompt(str(p), "agent-9")
    assert strategy_prompt(plan["strategies"][0]).startswith("## Your strategy")


def test_task_needs_api():
    with pytest.raises(ValueError):
        load_task(description="stack the blocks")


def test_memory_from_tracker_uses_latest_closed_race_on_same_task():
    class T:
        def races(self):
            return [{"race_id": "r1", "task": "can_to_bin", "status": "closed"},
                    {"race_id": "r2", "task": "other", "status": "closed"},
                    {"race_id": "r3", "task": "can_to_bin", "status": "open"}]

        def lessons(self, rid):
            assert rid == "r1"
            return {"lines": ["A: solved 3/3"], "skill_eligible": True,
                    "skill": {"agent_id": "agent-1", "persona": "A", "code": "x = 1"}}
    mem = memory_from_tracker(T(), "can_to_bin")
    assert mem["lessons"] == ["[r1] A: solved 3/3"] and mem["skills"][0]["code"] == "x = 1"
