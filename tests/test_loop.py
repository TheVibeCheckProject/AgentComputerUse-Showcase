"""The loop's accuracy rules, with a fake screen and no model requests or real input."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import loop
import screen
from brain import BrainError, parse_json
from hands import Hands


class FakeBrain:
    requests = local_requests = 0


def make_loop():
    return loop.Loop("find the weather", Hands(dry_run=True), brain=FakeBrain())  # type: ignore[arg-type]  # test double, not a Brain


def view():
    items = [screen.Item(1, "Edit", "Address and search bar", (0, 0, 100, 20), value="https://www.bing.com"),
             screen.Item(2, "Hyperlink", "Springfield, IL Hourly Weather | AccuWeather", (0, 30, 300, 50)),
             screen.Item(3, "Text", "78°F Sunny", (0, 60, 100, 80), source="ocr"),
             screen.Item(4, "Button", "Submit application", (0, 90, 100, 110))]
    return screen.View("Bing - Edge", "msedge.exe", (0, 0, 800, 600), "https://www.bing.com", items)


def test_only_items_in_the_current_list_and_typing_only_into_fields():
    lp, v = make_loop(), view()
    assert lp.check({"action": "click", "id": 2}, v) is None
    assert "isn't in the current list" in (lp.check({"action": "click", "id": 99}, v))
    assert "needs an item number" in (lp.check({"action": "click"}, v))
    assert "not a field" in (lp.check({"action": "type", "id": 2, "text": "x"}, v))
    assert lp.check({"action": "type", "id": 1, "text": "weather"}, v) is None
    assert "unknown action" in (lp.check({"action": "teleport"}, v))


def test_an_answer_needs_evidence_that_is_really_on_screen():
    lp, v = make_loop(), view()
    for i in v.items:
        lp.seen_text.append(i.name)
    assert "needs evidence" in (lp.check({"action": "done", "answer": "78 and sunny"}, v))
    assert "isn't on the current page" in (lp.check({"action": "done", "answer": "80", "evidence": ["80°F Cloudy"]}, v))
    assert lp.check({"action": "done", "answer": "78°F and sunny", "evidence": ["78°F Sunny"]}, v) is None


def test_risky_clicks_always_need_approval():
    assert loop.RISKY_CLICK.search("Submit application")
    assert loop.RISKY_CLICK.search("Place order")
    assert loop.RISKY_CLICK.search("Enter now")
    assert loop.RISKY_CLICK.search("Sign up")
    assert not loop.RISKY_CLICK.search("Search")
    assert not loop.RISKY_CLICK.search("Springfield, IL Hourly Weather | AccuWeather")
    lp, v = make_loop(), view()
    with pytest.raises(loop._NeedsApproval):
        lp.act({"action": "click", "id": 4}, v)              # whatever the model said, it stops first


def test_model_replies_are_read_even_with_fences_or_extra_words():
    assert parse_json('```json\n{"action": "wait"}\n```') == {"action": "wait"}
    assert parse_json('Sure! {"action": "done", "answer": "x"} hope that helps') == {"action": "done", "answer": "x"}
    with pytest.raises(BrainError):
        parse_json("no json here")
    with pytest.raises(BrainError):
        parse_json("{not json at all}")


def test_a_reply_with_two_objects_reads_the_first():
    """task-231 (2026-10-26) died on 'JSONDecodeError: Extra data' when the model sent two objects."""
    assert parse_json('{"action": "click", "id": 3}\n{"action": "wait"}') == {"action": "click", "id": 3}
    assert parse_json('Plan: {"action": "type", "id": 1, "text": "a{b}"} then {"action": "wait"}') == \
        {"action": "type", "id": 1, "text": "a{b}"}


def test_the_owners_windows_are_look_only_for_clicks_too(monkeypatch):
    """A dry run chose to click a button in the owner's code editor."""
    import task as tasks

    class Win:
        NativeWindowHandle = 77
        Name = "AlwaysOnAgent - Antigravity IDE"
    monkeypatch.setattr(tasks, "selected_tab", lambda w: (None, None))
    lp, v = make_loop(), view()
    lp.window = Win()
    assert "look-only" in (lp.check({"action": "click", "id": 2}, v))
    lp.task.opened(Win())                    # a window the task opened: fine
    assert lp.check({"action": "click", "id": 2}, v) is None



def test_keys_only_go_to_its_own_windows(monkeypatch):
    """2026-10-10: a retried run pressed Ctrl+W in a Notepad tab it didn't make. Keys are checked like clicks."""
    import task as tasks

    class Win:
        NativeWindowHandle = 78
        Name = "*Hello from AlwaysOnAgent - Notepad"
    monkeypatch.setattr(tasks, "selected_tab", lambda w: (None, None))
    monkeypatch.setattr(tasks, "root_owner", lambda h: 0)
    lp, v = make_loop(), view()
    lp.window = Win()
    assert "look-only" in (lp.check({"action": "press", "keys": "ctrl+w"}, v))
    lp.task.opened(Win())
    assert lp.check({"action": "press", "keys": "enter"}, v) is None


def test_a_dialog_its_own_window_opened_is_its_own(monkeypatch):
    """A Save prompt is a window of its own whose owner is the app window (2026-10-10: refused as "not yours")."""
    import task as tasks
    t = tasks.Task("x")
    t.windows.add(100)
    monkeypatch.setattr(tasks, "root_owner", lambda h: 100 if h == 200 else 0)
    dialog = type("W", (), {"NativeWindowHandle": 200})()
    stranger = type("W", (), {"NativeWindowHandle": 300})()
    assert t.owns_window(dialog) and not t.owns_window(stranger)


def test_the_double_check_gets_exact_text_and_knows_ocr_can_be_off(monkeypatch):
    """2026-10-10: OCR read 'AlwaysOnAgent' as 'AlwaysQnAgen_t' and a correct Notepad task was refused twice."""
    lp, v = make_loop(), view()
    lp.goal = "Open Notepad and type: Hello from AlwaysOnAgent."
    lp.plan = {"steps": [], "done_when": "Notepad shows the text"}
    lp.window = type("W", (), {"Name": "*Hello from AlwaysOnAgent. - Notepad"})()
    monkeypatch.setattr(lp, "_field_readings", lambda: ["Text editor: 'Hello from AlwaysOnAgent.'"])
    sent = []
    def _double_check(msgs):
        sent.append(msgs)
        return {"supported": True, "why": "ok"}
    monkeypatch.setattr(lp, "_double_check", _double_check)
    assert lp.verify_task({"answer": "typed it"}, v) is None
    system, user = sent[0][0]["content"], sent[0][1]["content"]
    assert "OCR" in system and "O/Q" in system
    assert "FIELDS NOW (exact" in user and "Hello from AlwaysOnAgent." in user
    assert "WINDOW TITLE NOW: *Hello from AlwaysOnAgent. - Notepad" in user

def test_the_little_rock_mistake_is_refused():
    """2026-10-05, the first live run: it typed a Springfield address without pressing Enter, read Chicago's
    76 degrees and answered 'Springfield is 76', with evidence ['76', '°']."""
    lp = loop.Loop("Find the current temperature in Springfield, Illinois", Hands(dry_run=True), brain=FakeBrain())  # type: ignore[arg-type]  # test double, not a Brain
    assert loop.goal_subjects(lp.goal) == ["springfield", "illinois"]
    items = [screen.Item(1, "Edit", "Address and search bar", (0, 0, 100, 20),
                         value="https://weather.com/weather/today/l/Springfield+IL"),
             screen.Item(2, "Header", "Downtown Chicago Weather", (0, 30, 300, 50)),
             screen.Item(3, "Text", "Downtown Chicago, Chicago, Illinois", (0, 60, 300, 80)),
             screen.Item(4, "Text", "76°", (0, 90, 100, 110))]
    v = screen.View("weather.com", "msedge.exe", (0, 0, 800, 600), "https://weather.com", items)
    assert "at least 8 characters" in (lp.check({"action": "done", "answer": "76", "evidence": ["76", "°"]}, v) or "")
    # typed text (the address bar) isn't evidence
    assert "current page" in (lp.check({"action": "done", "answer": "76",
                                       "evidence": ["weather/today/l/Springfield+IL"]}, v) or "")
    # 'Illinois' is on the page, but the line is about Chicago: the most specific name is required
    assert "about Springfield" in (lp.check({"action": "done", "answer": "76",
                                          "evidence": ["Downtown Chicago, Chicago, Illinois"]}, v) or "")
    # a page about another place entirely, with no subject word: refused
    assert "must show it's about" in (lp.check({"action": "done", "answer": "76",
                                               "evidence": ["Downtown Chicago Weather"]}, v) or "")


def test_an_answer_the_double_check_rejects_is_not_accepted():
    class Checker(FakeBrain):
        def plan(self, msgs):
            return {"supported": False, "why": "the page is Chicago's weather, not Springfield's", "quote": ""}
    lp = loop.Loop("Find the current temperature in Springfield, Illinois", Hands(dry_run=True), brain=Checker())  # type: ignore[arg-type]  # test double, not a Brain
    v = screen.View("w", "msedge.exe", (0, 0, 1, 1), None, [screen.Item(1, "Header", "Downtown Chicago Weather",
                                                                         (0, 0, 9, 9))])
    why = lp.verify_answer({"answer": "76"}, v)
    assert why and "Chicago" in why

