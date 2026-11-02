"""Retasks: the same task done cleanly before is repeated without the AI, inside the owner's hard lines
(2026-10-11): only the SAME task; answers never replayed (up to the reading point); writing never replayed; only
a clean run saved; off course hands over to the AI."""
from unittest.mock import MagicMock

import goals
import loop
import retasks
import screen
from hands import Hands
from loop import Loop

GROCERIES = "Open Groceries.txt on the Desktop and tell me the items on the list."
NOTE = "Open Notepad, write a two-line note saying the batch finished, and save it as BatchNote.txt on the Desktop."


class FakeBrain:
    def __init__(self, text="A fresh note.\nSecond line."):
        self.requests = self.local_requests = 0
        self.text = text

    def plan(self, messages):
        self.requests += 1
        return {"text": self.text}

    def step(self, messages):
        raise AssertionError("a replay never asks the step model")

    check = step


def live_loop(goal, brain=None):
    h = Hands(dry_run=True)
    h.dry_run = False                     # a "real" run, with nothing real behind it
    lp = Loop(goal, hands=h, brain=brain or FakeBrain())
    lp.task.pause = 0
    return lp


def item(i, kind, name, value=""):
    return screen.Item(id=i, kind=kind, name=name, rect=(0, 0, 100, 30), value=value)


def test_only_the_same_task_matches():
    retasks.save(GROCERIES, "info", [{"action": "open_file", "path": "Groceries.txt"}])
    assert retasks.find("  open groceries.txt on the desktop and tell me the items on the list ") is not None
    assert retasks.find("Open Packing.txt on the Desktop and tell me the items on the list.") is None
    assert retasks.find("Open Groceries.txt on the Desktop and tell me the first item.") is None


def test_written_text_is_never_stored_but_short_values_are():
    note = retasks.step_of({"action": "type", "text": "Dear neighbor,\nthanks!"}, "Document", "Text editor")
    assert note["write"] is True and "text" not in note
    search = retasks.step_of({"action": "type", "text": "Hoover Dam", "submit": True}, "Edit", "Search Wikipedia")
    assert search["write"] is False and search["text"] == "Hoover Dam" and search["submit"] is True


def test_a_password_field_is_never_saved():
    assert retasks.save("Sign in to example.com", "task",
                        [{"action": "type", "kind": "Edit", "name": "Password", "text": "x", "write": False}]) is None


def test_a_clean_run_is_saved_and_a_messy_one_isnt(monkeypatch):
    lp = live_loop(NOTE)
    lp.recorded = [{"action": "launch_app", "app": "notepad"},
                   {"action": "type", "kind": "Document", "name": "Text editor", "write": True},
                   {"action": "save_as", "path": "BatchNote.txt"}]
    lp._finish(loop.Result("x"), "done")
    assert retasks.find(NOTE)["kind"] == "task"
    retasks.forget(retasks.retask_id(NOTE))
    messy = live_loop(NOTE)
    messy.recorded = list(lp.recorded)
    messy.history.append({"action": "click", "result": "REFUSED: not yours", "expect": None})
    messy._finish(loop.Result("x"), "done")
    assert retasks.find(NOTE) is None
    retyped = live_loop(NOTE)
    retyped.recorded, retyped.retyped = list(lp.recorded), True
    retyped._finish(loop.Result("x"), "done")
    assert retasks.find(NOTE) is None


def _fake_screen(lp, monkeypatch, items):
    acted = []
    monkeypatch.setattr(lp, "look", lambda: MagicMock(items=items))
    monkeypatch.setattr(lp, "check", lambda reply, view: None)
    monkeypatch.setattr(lp, "act", lambda reply, view: acted.append(dict(reply)) or f"did {reply['action']}")
    monkeypatch.setattr(lp, "_note_fields", lambda: None)
    return acted


def test_a_task_is_repeated_with_no_requests_except_fresh_writing(monkeypatch):
    retasks.save(NOTE, "task", [{"action": "launch_app", "app": "notepad"},
                                {"action": "type", "kind": "Document", "name": "Text editor", "write": True},
                                {"action": "save_as", "path": "BatchNote.txt"}])
    brain = FakeBrain("Batch finished.\nAll good.")
    lp = live_loop(NOTE, brain)
    acted = _fake_screen(lp, monkeypatch, [item(3, "Document", "Text editor")])
    monkeypatch.setattr(goals, "saved_file_problem", lambda *a: None)
    monkeypatch.setattr(lp, "_still_open_problem", lambda: None)
    done = lp._try_retask()
    assert done and done["detail"]["repeated_steps"] == 3
    assert [a["action"] for a in acted] == ["launch_app", "type", "save_as"]
    assert acted[1]["text"] == "Batch finished.\nAll good." and acted[1]["id"] == 3
    assert brain.requests == 1                     # the fresh text only
    assert retasks.find(NOTE)["uses"] == 1


def test_a_question_is_repeated_only_up_to_the_reading_point(monkeypatch):
    retasks.save(GROCERIES, "info", [{"action": "open_file", "path": "Groceries.txt"}])
    brain = FakeBrain()
    lp = live_loop(GROCERIES, brain)
    acted = _fake_screen(lp, monkeypatch, [])
    assert lp._try_retask() is None                # the AI reads the answer
    assert [a["action"] for a in acted] == ["open_file"] and brain.requests == 0
    assert "read the answer from the CURRENT screen" in lp.history[-1]["result"]
    lp._finish(loop.Result("x"), "done")
    assert retasks.find(GROCERIES)["steps"] == [{"action": "open_file", "path": "Groceries.txt"}]  # not overwritten


def test_off_course_hands_over_and_twice_drops_it(monkeypatch):
    steps = [{"action": "open_url", "url": "https://en.wikipedia.org"},
             {"action": "type", "kind": "Edit", "name": "Search Wikipedia", "text": "Hoover Dam", "submit": True,
              "write": False}]
    goal = "On Wikipedia, find what year the Hoover Dam was completed and tell me."
    retasks.save(goal, "info", steps)
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    for n in (1, 2):
        lp = live_loop(goal)
        acted = _fake_screen(lp, monkeypatch, [item(1, "Edit", "Search MDN")])   # the box isn't there
        assert lp._try_retask() is None
        assert [a["action"] for a in acted] == ["open_url"]
        assert "isn't on the screen" in lp.history[-1]["result"]
    assert retasks.find(goal) is None              # dropped after two


def test_reading_moves_are_never_stored():
    """"What year did Missouri become a state" saved 9 scrolls hunting for the answer (2026-10-11)."""
    goal = "On Wikipedia, find what year Missouri became a state and tell me."
    r = retasks.save(goal, "info", [{"action": "open_url", "url": "https://www.wikipedia.org"},
                                    {"action": "type", "kind": "Edit", "name": "Search Wikipedia", "text": "Missouri",
                                     "submit": True, "write": False},
                                    {"action": "scroll", "direction": "down"}, {"action": "press", "keys": "ctrl+home"},
                                    {"action": "scroll", "direction": "up"}, {"action": "wait"}])
    assert [s["action"] for s in r["steps"]] == ["open_url", "type"]
    assert not retasks.is_looking({"action": "press", "keys": "ctrl+s"})


def test_a_target_off_screen_is_still_found(monkeypatch):
    lp = live_loop("x")
    far = item(7, "Button", "Download")
    far.offscreen = True
    assert lp._replay_target({"kind": "Button", "name": "Download"}, MagicMock(items=[far])) is far


def test_retask_lines_have_their_own_level_for_the_app():
    """The owner (2026-10-11): color the "saved as a retask" line and every retask step on the task card."""
    import activity
    assert activity.say({"message": "saved as a retask: ...", "retask": True})[1] == "retask"
    assert activity.say({"action": "open_url", "url": "https://www.irs.gov/x", "step": "R1", "retask": True}) == \
        ("Opening www.irs.gov", "retask")
    assert activity.say({"message": "repeating an earlier run stopped at step 2 (gone)"})[1] == "warn"
    assert activity.say({"action": "click", "target": "Download", "retask": True,
                         "problem": "not yours"})[1] == "warn"
    assert activity.say({"action": "open_url", "url": "https://x.org"})[1] == "info"


def test_each_line_says_what_kind_of_step_it_is():
    """The app colors "What it did" by kind (the owner, 2026-10-11: "basically all white text")."""
    import activity
    assert activity.kind_of({"action": "open_url"}) == "open"
    assert activity.kind_of({"action": "type"}) == "type"
    assert activity.kind_of({"action": "click"}) == "click"
    assert activity.kind_of({"action": "save_as"}) == "save"
    assert activity.kind_of({"plan": ["a", "b"]}) == "plan"
    assert activity.kind_of({"message": "minimized every window, so ..."}) == "plan"
    assert activity.kind_of({"message": "that didn't work: x"}) == ""


def test_a_repeated_task_says_so_once():
    """The agent's card adds "(Repeated an earlier run: N AI requests.)"; the answer doesn't repeat it."""
    import inspect
    import replay
    assert "repeated an earlier run, no AI" not in inspect.getsource(replay)
