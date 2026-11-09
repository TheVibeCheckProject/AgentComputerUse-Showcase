"""Tests for procedural recipe replay and zero-cost execution."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import recipes
import screen
from hands import Hands
from task import Task


def test_recipe_goal_pattern_matching():
    r = recipes.Recipe(
        name="google_weather",
        goal_pattern=r"weather\s+in\s+(?P<loc>.+)",
        steps=[]
    )

    params = r.match_goal("What is the weather in Springfield, Illinois?")
    assert params is not None
    assert "loc" in params
    assert "Springfield, Illinois" in params["loc"]

    params_no = r.match_goal("Search for pizza places")
    assert params_no is None


def test_save_and_load_recipes(tmp_path):
    r = recipes.Recipe(
        name="test_routine",
        goal_pattern=r"lookup\s+(?P<item>\w+)",
        steps=[
            recipes.RecipeStep(action="open_url", url="https://example.com/search?q={item}"),
            recipes.RecipeStep(action="wait_landmark", pattern=r"Results"),
            recipes.RecipeStep(action="extract", pattern=r"Found \d+ results")
        ]
    )

    saved_path = recipes.save_recipe(r, folder=tmp_path)
    assert saved_path.is_file()

    loaded = recipes.load_recipes(folder=tmp_path)
    assert len(loaded) == 1
    assert loaded[0].name == "test_routine"
    assert len(loaded[0].steps) == 3


def test_execute_recipe_success():
    r = recipes.Recipe(
        name="mock_search",
        goal_pattern=r"find\s+(?P<term>\w+)",
        steps=[
            recipes.RecipeStep(action="open_url", url="https://example.com/{term}"),
            recipes.RecipeStep(action="wait_landmark", pattern=r"Springfield"),
            recipes.RecipeStep(action="extract", pattern=r"71°F")
        ]
    )

    fake_items = [
        screen.Item(1, "Text", "Springfield Weather", (10, 10, 100, 30), False, "uia"),
        screen.Item(2, "Text", "71°F Clear", (10, 40, 100, 60), False, "uia"),
    ]
    fake_view = screen.View("Browser", "msedge.exe", (0, 0, 800, 600), "https://example.com", fake_items)

    class DummyWin:
        pass

    with patch("actions.open_url"), \
         patch("screen.look", return_value=fake_view):
        hands = Hands(dry_run=True)
        task_obj = Task("find weather")
        outcome = recipes.execute_recipe(r, {"term": "weather"}, DummyWin(), hands, task_obj)

        assert outcome is not None
        assert "71°F" in outcome["answer"]
        assert outcome["recipe"] == "mock_search"


def test_execute_recipe_divergence_returns_none():
    r = recipes.Recipe(
        name="mock_search",
        goal_pattern=r"find\s+(?P<term>\w+)",
        steps=[
            recipes.RecipeStep(action="wait_landmark", pattern=r"ExpectedLandmark"),
        ]
    )

    fake_items = [
        screen.Item(1, "Text", "Different Page Content", (10, 10, 100, 30), False, "uia"),
    ]
    fake_view = screen.View("Browser", "msedge.exe", (0, 0, 800, 600), "https://example.com", fake_items)

    class DummyWin:
        pass

    with patch("screen.look", return_value=fake_view):
        hands = Hands(dry_run=True)
        task_obj = Task("find weather")
        outcome = recipes.execute_recipe(r, {"term": "weather"}, DummyWin(), hands, task_obj)

        # Fails landmark, returns None so full LLM loop takes over
        assert outcome is None


# ------------------------------------------------------------------ recipes inside the loop (2026-10-08)
import actions
import loop
from hands import Stopped


def _recipe_loop(monkeypatch, outcome=None, raises=None):
    lp = loop.Loop("Find the weather in Springfield", Hands(dry_run=True))
    lp.hands.dry_run = False                       # recipes only run for real; nothing below touches a device
    r = recipes.Recipe("weather", r"weather in (?P<loc>\w+)", [])
    monkeypatch.setattr(recipes, "find_matching_recipe", lambda goal: (r, {"loc": "Springfield"}))
    lp.edge = object()
    monkeypatch.setattr(loop.Loop, "_alive", staticmethod(lambda w: True))

    def run_it(*a, **k):
        if raises:
            raise raises
        return outcome
    monkeypatch.setattr(recipes, "execute_recipe", run_it)
    return lp


def test_the_kill_switch_during_a_recipe_stops_the_run(monkeypatch):
    # It was swallowed by `except Exception`, and the AI loop carried on after Ctrl+Alt+End.
    lp = _recipe_loop(monkeypatch, raises=Stopped("the kill switch (Ctrl+Alt+End) was pressed"))
    with pytest.raises(Stopped):
        lp._try_recipe()


def test_a_recipe_answer_needs_the_same_evidence_as_the_models(monkeypatch):
    # Chicago's page must not answer a Springfield question, recipe or not (the 2026-10-05 wrong answer).
    lp = _recipe_loop(monkeypatch, outcome={"answer": "71°F Clear", "evidence": ["71°F Clear in Chicago"]})
    page = screen.View("Edge", "msedge.exe", (0, 0, 800, 600), "https://example.com",
                       [screen.Item(1, "Text", "71°F Clear in Chicago", (10, 10, 200, 30))])
    monkeypatch.setattr(lp, "look", lambda: page)
    assert lp._try_recipe() is None
    assert "didn't finish" in lp.history[-1]["result"]


def test_a_recipe_answer_with_good_evidence_finishes_the_task(monkeypatch):
    lp = _recipe_loop(monkeypatch, outcome={"answer": "71°F", "evidence": ["Springfield, IL 71°F Clear"]})
    page = screen.View("Edge", "msedge.exe", (0, 0, 800, 600), "https://example.com",
                       [screen.Item(1, "Text", "Springfield, IL 71°F Clear", (10, 10, 200, 30))])
    monkeypatch.setattr(lp, "look", lambda: page)
    done = lp._try_recipe()
    assert done["answer"] == "71°F" and done["detail"] == {"recipe": "weather"}


def test_no_recipe_in_a_dry_run(monkeypatch):
    lp = _recipe_loop(monkeypatch, outcome={"answer": "x", "evidence": ["x"]})
    lp.hands.dry_run = True
    assert lp._try_recipe() is None


def test_a_recipe_never_types_into_a_window_the_task_doesnt_own(monkeypatch):
    r = recipes.Recipe("search", r"search (?P<q>\w+)", [recipes.RecipeStep(action="type", target_kind="Edit",
                                                                           text="{q}", submit=True)])
    view = screen.View("Notes", "notepad.exe", (0, 0, 800, 600), None,
                       [screen.Item(1, "Edit", "Text editor", (10, 10, 400, 300))])
    typed = []
    monkeypatch.setattr(screen, "look", lambda *a, **k: view)
    monkeypatch.setattr(actions, "type_into", lambda *a, **k: typed.append(a))

    import task as tasks
    monkeypatch.setattr(tasks, "selected_tab", lambda win: (None, None))     # not a browser: no tabs

    class OwnersWindow:
        Name = "My notes - Notepad"
        NativeWindowHandle = 4242
    with pytest.raises(Exception, match="look-only"):
        recipes.execute_recipe(r, {"q": "hello"}, OwnersWindow(), Hands(dry_run=True), Task("search hello"))
    assert typed == []
