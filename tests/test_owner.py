"""Asking the owner: the request is saved with its screenshot, the answer comes back through a file, and the loop
acts on it (no real input: clicks are stand-ins)."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import actions
import loop
import requests_owner
import screen
from hands import Hands


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(requests_owner, "REQUESTS", tmp_path / "requests")
    monkeypatch.setattr(requests_owner, "ANSWERS", tmp_path / "answers")
    return tmp_path


def test_a_request_waits_for_the_owners_answer(data):
    events: list = []

    def answer_soon():
        time.sleep(0.4)
        rid = events[0]["id"]
        (data / "answers" / f"{rid}.json").write_text(json.dumps({"answer": "Approve"}), encoding="utf-8")

    threading.Thread(target=answer_soon, daemon=True).start()
    ans = requests_owner.ask("approve", "Approve clicking 'Submit application'?", ["Approve", "Deny"],
                             url="https://jobs.example.com", png=b"\x89PNG fake",
                             emit=lambda e, **f: events.append({"event": e, **f}), poll=0.1)
    assert ans == "Approve"
    e = events[0]
    assert e["event"] == "needs_you" and e["kind"] == "approve" and e["choices"] == ["Approve", "Deny"]
    assert (data / "requests" / e["screenshot"]).read_bytes() == b"\x89PNG fake"
    saved = json.loads((data / "requests" / f"{e['id']}.json").read_text(encoding="utf-8"))
    assert saved["closed"]                               # answered: no longer pending


def test_no_answer_in_time_pauses(data):
    with pytest.raises(requests_owner.NoAnswer):
        requests_owner.ask("text", "Which city?", wait=0.3, poll=0.1)


def _loop(answer):
    asked = []

    def asker(kind, question, choices, url, png):
        asked.append((kind, question, choices))
        return answer
    lp = loop.Loop("apply for the job", Hands(dry_run=True), brain=type("B", (), {"requests": 0, "local_requests": 0})(),
                   asker=asker)
    return lp, asked


def test_approve_clicks_and_deny_stops(monkeypatch):
    clicked = []
    monkeypatch.setattr(actions, "click", lambda item, win, hands: clicked.append(item.name))
    import requests_owner as ro
    monkeypatch.setattr(ro, "screenshot", lambda rect=None, highlight=None: None)
    item = screen.Item(4, "Button", "Submit application", (0, 0, 10, 10))
    lp, asked = _loop("Approve")
    assert lp._approval(item, "clicking 'Submit application'", None) == "approved"
    assert clicked == ["Submit application"] and asked[0][0] == "approve"
    lp, asked = _loop("Deny")
    assert lp._approval(item, "clicking 'Submit application'", None) == "declined"
    assert clicked == ["Submit application"]             # nothing more was clicked
    lp = loop.Loop("x", Hands(dry_run=True), brain=type("B", (), {"requests": 0, "local_requests": 0})())
    assert lp._approval(item, "clicking it", None) == "needs_approval"   # no app to ask: stops as before


def test_an_owners_answer_becomes_part_of_the_task(monkeypatch):
    import requests_owner as ro
    monkeypatch.setattr(ro, "screenshot", lambda rect=None, highlight=None: None)
    lp, _asked = _loop("Springfield, Illinois")
    ans = lp._ask("text", "Which Springfield?")
    lp._owner_said("Which Springfield?", ans)
    assert "OWNER ANSWERED: Springfield, Illinois" in lp.history[-1]["result"]
    assert any("Springfield, Illinois" in h for h in lp.owner_instructions)


def test_auth_alternatives_and_password_field_detection():
    items = [
        screen.Item(1, "Edit", "Enter your password", (0, 0, 100, 20)),
        screen.Item(2, "Hyperlink", "Try another way", (0, 30, 100, 50)),
        screen.Item(3, "Button", "Next", (0, 60, 100, 80)),
    ]
    v = screen.View("Google Sign-in", "msedge.exe", (0, 0, 800, 600), "https://accounts.google.com", items)
    assert loop.Loop.has_password_field(v) is True
    alts = loop.Loop.find_auth_alternatives(v)
    assert len(alts) == 1 and alts[0].name == "Try another way"


def _signin_view(*extra):
    items = [
        screen.Item(1, "Edit", "Enter your password", (0, 0, 100, 20)),
        screen.Item(2, "Hyperlink", "Try another way", (0, 30, 100, 50)),
        screen.Item(3, "Button", "Next", (0, 60, 100, 80)),
        *extra,
    ]
    return screen.View("Google Sign-in", "msedge.exe", (0, 0, 800, 600), "https://accounts.google.com/v3/signin", items)


def test_only_an_offered_choice_is_clicked(monkeypatch):
    """The owner's pick acts on the item it came from; free text is never matched against the screen (2026-10-06:
    an answer like 'Springfield' would have clicked a 'Springfield...' link, and 'I tapped Yes' a 'Yes' button)."""
    clicked = []
    monkeypatch.setattr(actions, "click", lambda item, win, hands: clicked.append(item.name))
    v = _signin_view(screen.Item(4, "Hyperlink", "Springfield weather", (0, 90, 100, 110)),
                     screen.Item(5, "Button", "Yes", (0, 120, 100, 140)))
    lp = loop.Loop("sign in", Hands(dry_run=True))
    offered = lp._signin_options(v)
    assert list(offered) == [loop.TYPE_PASSWORD, "Try another way"]
    assert "Try another way" in (lp._pick("try another way", offered) or "") and clicked == ["Try another way"]
    for free_text in ("Springfield", "click 3", "Next", "3", loop.TAPPED_YES):
        assert lp._pick(free_text, offered) is None
    assert clicked == ["Try another way"]


def test_a_password_goes_into_its_box_and_nowhere_else(monkeypatch):
    """The owner types their password in the app; the tool types it into the password box. It never reaches the
    model's messages, the history, the owner notes or the step log (2026-10-06: it was sent to the model)."""
    import requests_owner as ro
    import task as tasks
    monkeypatch.setattr(ro, "screenshot", lambda rect=None, highlight=None: None)
    monkeypatch.setattr(tasks, "check_may_type_here", lambda task, win: None)
    typed = []
    monkeypatch.setattr(actions, "type_into",
                        lambda item, text, win, hands, secret=False: typed.append((item.name, text, secret)))
    logged = []
    lp, asked = _loop("hunter2-secret")
    lp.log = type("L", (), {"write": lambda self, rec: logged.append(rec), "step": lambda self, *a, **k: None})()
    v = _signin_view()
    assert lp._pick(loop.TYPE_PASSWORD, lp._signin_options(v))
    assert typed == [("Enter your password", "hunter2-secret", True)]
    assert asked[0][0] == "secret"
    everything = json.dumps([lp.history, lp.hints, lp.owner_instructions, logged, lp._messages(v)])
    assert "hunter2" not in everything


def _typed_a_password(monkeypatch, answers):
    """A run where the owner typed their password in the app; `answers` are the owner's next replies."""
    import credentials
    import requests_owner as ro
    import task as tasks
    monkeypatch.setattr(ro, "screenshot", lambda rect=None, highlight=None: None)
    monkeypatch.setattr(tasks, "check_may_type_here", lambda task, win: None)
    monkeypatch.setattr(actions, "type_into", lambda *a, **k: None)
    monkeypatch.setattr(credentials, "find_for_url", lambda url: None)
    saved, asked = [], []
    monkeypatch.setattr(credentials, "store_secure_credential", lambda d, u, p: saved.append((d, u, p)))
    replies = iter(answers)

    def asker(kind, question, choices, url, png):
        asked.append((kind, question, choices))
        return next(replies)
    lp = loop.Loop("sign in to AI Studio", Hands(dry_run=True),
                   brain=type("B", (), {"requests": 0, "local_requests": 0})(), asker=asker)
    monkeypatch.setattr(lp, "_url", lambda: "https://accounts.google.com/v3/signin")
    lp._last_typed_user = "agent@example.com"
    assert lp._pick(loop.TYPE_PASSWORD, lp._signin_options(_signin_view()))
    return lp, saved, asked


def test_a_typed_password_is_saved_only_after_the_run_worked_and_the_owner_agrees(monkeypatch):
    """2026-10-26's auto-save stored it at once (a typo stuck), under a guessed domain and user."""
    lp, saved, asked = _typed_a_password(monkeypatch, ["Hunter2!x", "Save"])
    assert saved == []                                   # nothing yet: the run hasn't finished
    lp.offer_to_save_password("done")
    assert saved == [("accounts.google.com", "agent@example.com", "Hunter2!x")]
    assert asked[-1][2] == ["Save", "Don't save"] and "Hunter2" not in asked[-1][1]


def test_a_failed_run_or_a_no_saves_nothing(monkeypatch):
    lp, saved, asked = _typed_a_password(monkeypatch, ["wrong-pw1", "Save"])
    lp.offer_to_save_password("needs_owner")             # the sign-in didn't get through
    assert saved == [] and len(asked) == 1               # not even asked
    lp.offer_to_save_password("done")                    # and it's forgotten, not kept for later
    assert saved == []
    lp, saved, _ = _typed_a_password(monkeypatch, ["Hunter2!x", "Don't save"])
    lp.offer_to_save_password("done")
    assert saved == []


def test_a_secret_answer_is_deleted_once_read(data):
    events: list = []

    def answer_soon():
        time.sleep(0.3)
        (data / "answers" / f"{events[0]['id']}.json").write_text(json.dumps({"answer": "123456"}), encoding="utf-8")

    threading.Thread(target=answer_soon, daemon=True).start()
    assert requests_owner.ask("secret", "Your Google code", emit=lambda e, **f: events.append(f), poll=0.1) == "123456"
    assert not list((data / "answers").glob("*.json"))
    assert "123456" not in (data / "requests" / f"{events[0]['id']}.json").read_text(encoding="utf-8")


def test_owner_types_needs_a_field():
    v = _signin_view()
    lp = loop.Loop("sign in", Hands(dry_run=True))
    assert lp.check({"action": "owner_types", "id": 3}, v) == "item 3 is a Button, not a field you can type into"
    assert lp.check({"action": "owner_types", "id": 1}, v) is None


def test_no_phone_alert_off_a_signin_page():
    items = [screen.Item(1, "Text", "Check your phone for the latest deals", (0, 0, 100, 20))]
    v = screen.View("Deals", "msedge.exe", (0, 0, 800, 600), "https://news.example.com/article", items)
    assert loop.Loop.detect_phone_prompt(v) is None


def test_phone_prompt_detection():
    items = [
        screen.Item(1, "Text", "Check your phone", (0, 0, 100, 20)),
        screen.Item(2, "Text", "Google sent a notification to your phone. Tap Yes, then select number 42", (0, 30, 300, 50)),
    ]
    v = screen.View("Google Sign-in", "msedge.exe", (0, 0, 800, 600), "https://accounts.google.com", items)
    prompt = loop.Loop.detect_phone_prompt(v)
    assert prompt is not None
    assert "tap Yes" in prompt
    assert "42" in prompt

