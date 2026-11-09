"""The banner on Take over's screen: what a run is doing (activity.py)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import activity


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "DATA", tmp_path)
    monkeypatch.setattr(activity, "PATH", tmp_path / "activity.json")


def test_a_run_shows_its_goal_step_and_what_it_is_doing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    activity.start("Open notepad and type hello")
    activity.note("progress", {"plan": ["open notepad", "type"]})
    activity.note("progress", {"step": "2.1", "action": "type", "text": "hello", "target": "Text editor",
                               "expect": "the paragraph is typed", "chained": True})
    now = activity.current()
    assert now["running"] and now["goal"] == "Open notepad and type hello" and now["step"] == "2.1"
    assert now["doing"] == "Typing “hello” into “Text editor”"


def test_each_step_reads_like_someone_describing_it(tmp_path, monkeypatch):
    """2026-10-10, the owner: "Clicking on start menu... typing Notepad, pressing enter, typing a joke"."""
    _isolate(tmp_path, monkeypatch)
    activity.start("Open Notepad and type a joke")
    for f in ({"step": 1, "action": "click", "target": "Start"},
              {"step": 2, "action": "launch_app", "app": "Notepad"},
              {"step": 3, "action": "type", "text": "Why did the golfer bring two pairs of pants? In case he got a hole in one."},
              {"step": "3.1", "action": "press", "keys": "ctrl+w", "chained": True},
              {"step": 4, "action": "click", "target": "Delete", "problem": "not the task's own window"}):
        activity.note("progress", f)
    activity.finish("done", answer="typed the joke and closed Notepad")
    log = activity.current()["log"]
    assert [e["text"] for e in log] == [
        "Clicking “Start”", "Opening Notepad",
        "Typing “Why did the golfer bring two pairs of pants? In case he got…”",
        "Pressing Ctrl+W", "Clicking “Delete”: not allowed (not the task's own window)",
        "Done: typed the joke and closed Notepad"]
    assert [e["level"] for e in log] == ["info", "info", "info", "info", "warn", "ok"]
    assert log[1]["step"] == 2 and log[0]["t"].count(":") == 2


def test_secrets_never_reach_the_log(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    activity.start("sign in")
    activity.note("progress", {"step": 3, "action": "type", "text": "hunter2-secret", "expect": "signed in"})
    activity.note("progress", {"step": 4, "action": "type", "text": "correct horse battery", "target": "Password"})
    activity.note("progress", {"step": 5, "action": "type", "text": "482913"})
    stored = (tmp_path / "activity.json").read_text(encoding="utf-8")
    assert "hunter2" not in stored and "horse" not in stored and "482913" not in stored
    assert [e["text"] for e in activity.current()["log"]] == [
        "Typing something private (hidden)", "Typing something private (hidden) into “Password”",
        "Typing something private (hidden)"]


def test_plain_words_and_addresses_are_shown():
    for text in ("Notepad", "notepad.exe", "www.example.com", "Springfield weather", "alex@example.com"):
        assert not activity.secret_like(text), text
    for text in ("Tr0ub4dor", "p@ssw0rd!", "1234"):
        assert activity.secret_like(text), text


def test_a_sentence_made_by_the_runner_is_used_as_is(tmp_path, monkeypatch):
    """agentcomputeruse.emit_run makes the sentence and drops the typed text before the event leaves the process."""
    _isolate(tmp_path, monkeypatch)
    activity.start("x")
    activity.note("progress", {"step": 1, "action": "type", "say": "Typing “hi”", "level": "info"})
    assert activity.current()["log"][-1]["text"] == "Typing “hi”"


def test_a_finished_run_shows_briefly_then_goes(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    activity.start("Open notepad")
    activity.finish("done", answer="opened it")
    now = activity.current()
    assert not now["running"] and now["status"] == "done" and now["doing"] == "done: opened it"
    assert activity.current(now=10 ** 12) == {"running": False}


def test_a_run_whose_process_died_isnt_shown_as_running(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    activity.start("Open notepad")
    assert activity.current()["running"]                     # this very process: alive
    monkeypatch.setattr(activity, "_alive", lambda pid: False)
    assert activity.current() == {"running": False}


def test_nothing_running(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert activity.current() == {"running": False}
