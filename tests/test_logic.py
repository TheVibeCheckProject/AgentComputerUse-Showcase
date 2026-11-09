"""The parts that decide things, tested without touching the real mouse or keyboard."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import popups
import screen
from hands import Guard, Hands, Stopped
from ocr import OcrLine


# ------------------------------------------------------------------------------------------ the guard
class FakeMouse:
    def __init__(self):
        self.position = (100, 100)


def test_the_guard_gives_way_when_the_owner_moves_the_mouse():
    mouse = FakeMouse()
    g = Guard(seconds=60, mouse=mouse)
    g.remember((100, 100))
    g.check()                                     # where it left it: fine
    mouse.position = (101, 99)
    g.check()                                     # a pixel of driver jitter: fine
    mouse.position = (300, 240)
    with pytest.raises(Stopped, match="taken over"):
        g.check()


def test_the_kill_switch_and_the_time_limit_stop_it():
    now = [1000.0]
    g = Guard(seconds=10, mouse=FakeMouse(), clock=lambda: now[0])
    g.check()
    g.killed.set()
    with pytest.raises(Stopped, match="kill switch"):
        g.check()
    g2 = Guard(seconds=10, mouse=FakeMouse(), clock=lambda: now[0])
    now[0] += 11
    with pytest.raises(Stopped, match="time limit"):
        g2.check()


def test_a_dry_run_only_describes():
    h = Hands(dry_run=True)
    h.click(10, 20)
    h.type_text("héllo")
    h.press("ctrl+l")
    h.scroll(5, 5, -3)
    assert h.log == ["move to 10,20", "left click at 10,20", "type 5 characters", "press ctrl+l",
                     "move to 5,5", "scroll -3 at 5,5"]


# ------------------------------------------------------------------------------------------ merging
def item(kind, name, rect, offscreen=False, value=None):
    return screen.Item(0, kind, name, rect, offscreen, "uia", value)


def test_ocr_duplicates_are_dropped_unnamed_items_get_named_and_the_rest_is_added():
    items = [item("Button", "Submit order", (10, 10, 110, 40)),
             item("Button", "", (200, 10, 240, 40)),                  # an icon button with no label
             item("Edit", "Customer name:", (10, 60, 300, 90), value="Test Name")]
    lines = [OcrLine("Submit order", (20, 15, 100, 35)),             # the button's own label: duplicate
             OcrLine("Enter now", (205, 15, 235, 35)),               # names the unlabeled button
             OcrLine("Test Name", (15, 65, 90, 85)),                 # what's typed in the box: duplicate
             OcrLine("Free shipping on orders over $50", (10, 120, 400, 140))]   # only OCR sees it
    out = screen.number(screen.merge(items, lines))
    names = [(i.kind, i.name, i.source) for i in out]
    assert ("Button", "Enter now", "uia+ocr") in names
    assert ("Text", "Free shipping on orders over $50", "ocr") in names
    assert sum(1 for i in out if i.name == "Submit order") == 1
    assert not any(i.source == "ocr" and i.name == "Test Name" for i in out)


def test_the_list_puts_what_is_visible_first_and_marks_what_is_further_down():
    items = [item("Hyperlink", "Footer link", (10, 3000, 90, 3020), offscreen=True),
             item("Button", "Search", (10, 50, 90, 70)),
             item("Hyperlink", "Top link", (10, 20, 90, 40))]
    view = screen.View("Test - Edge", "msedge.exe", (0, 0, 800, 600), "https://example.com", screen.number(items))
    text = view.text()
    assert [i.name for i in view.items] == ["Top link", "Search", "Footer link"]
    assert "[3] Hyperlink 'Footer link' (below)" in text and "further down the page (1 more)" in text
    assert text.startswith("Window: 'Test - Edge' | address: https://example.com")


# ------------------------------------------------------------------------------------------ pop-ups
def test_only_exact_harmless_answers_are_chosen():
    assert popups.safe_choice(["Microsoft Privacy Statement", "Got it", "Customize"]) == "Got it"
    assert popups.safe_choice(["Accept all", "Reject all"]) == "Reject all"
    assert popups.safe_choice(["Continue to checkout", "Back"]) is None          # not the harmless "Continue"
    assert popups.safe_choice(["Allow", "Block"]) is None
    assert popups.safe_choice(["Turn on sync", "Sign in"]) is None
    assert popups.safe_choice(["OK"], text="This site wants to use your location") is None   # risky: owner


def test_risky_popups_are_recognised():
    assert popups.Popup((0, 0, 1, 1), "Leave site? Changes you made may not be saved.", ["Leave", "Cancel"]).risky
    assert not popups.Popup((0, 0, 1, 1), "We use cookies", ["Got it"]).risky


# ------------------------------------------------------------------------------------------ the command line
def test_the_demo_is_a_dry_run_without_go(tmp_path):
    env = {"ACU_RUNS_DIR": str(tmp_path / "runs"), "PYTHONIOENCODING": "utf-8", "SYSTEMROOT": "C:\\Windows"}
    import os
    env = {**os.environ, **env}
    res = subprocess.run([sys.executable, str(ROOT / "agentcomputeruse.py"), "demo"], capture_output=True,
                         text=True, encoding="utf-8", env=env, timeout=60, check=False)
    events = [json.loads(l) for l in res.stdout.splitlines() if l.startswith("{")]
    assert res.returncode == 0, res.stdout + res.stderr
    assert events[0]["message"].startswith("DRY RUN")
    done = events[-1]
    assert done["event"] == "done" and done["dry_run"] is True
    assert any(step.startswith("Start menu opened with") for step in done["steps"])   # no Windows key needed
    assert "press ctrl+s" in done["steps"]


def test_caps_lock_is_turned_off_before_typing():
    """2026-10-06 on the Mini: Caps Lock was on, so everything came out in capitals and the read-back never matched."""
    import hands as hands_mod
    pressed = []
    kb = type("K", (), {"press": lambda self, k: pressed.append(("press", str(k))),
                        "release": lambda self, k: pressed.append(("release", str(k)))})()
    assert hands_mod.caps_lock_off(kb, is_on=lambda: True) is True
    assert [p[0] for p in pressed] == ["press", "release"] and "caps_lock" in pressed[0][1]
    pressed.clear()
    assert hands_mod.caps_lock_off(kb, is_on=lambda: False) is False and pressed == []


def test_the_address_bar_counts_with_edges_autocomplete():
    """Edge adds the rest of a suggested address after what was typed (and may drop https:// or www.): that's
    the address typed, not a failure to retype three times."""
    import actions
    assert actions._same_address("jackson.com/forms", "jackson.com/forms/view-form.xhtml")
    assert actions._same_address("https://www.jackson.com/forms", "jackson.com/forms")
    assert actions._same_address("Jackson.com", "jackson.com")
    assert not actions._same_address("jackson.com/forms", "google.com/search?q=jackson")
    assert not actions._same_address("", "anything")


def test_the_taskbar_is_never_a_popup():
    """2026-10-06: with a File Explorer window in front, the taskbar (also explorer.exe) was taken for a pop-up."""
    import popups
    W = lambda cls: type("W", (), {"ClassName": cls})()
    assert popups.is_shell(W("Shell_TrayWnd")) and popups.is_shell(W("Shell_SecondaryTrayWnd"))
    assert not popups.is_shell(W("#32770"))                          # a real dialog still counts


def test_the_goal_can_come_from_a_file(tmp_path):
    # 2026-10-08: a goal's quotes were stripped between PowerShell layers ("cannot be found that accepts 'notepad,'").
    goal = tmp_path / "goal.txt"
    goal.write_text("Open notepad, type \"hi\" (don't save)", encoding="utf-8")
    import argparse

    import agentcomputeruse
    seen = {}

    class StopHere(Exception):
        pass

    def fake_loop(g, *a, **k):
        seen["goal"] = g
        raise StopHere()
    import loop
    orig = loop.Loop
    loop.Loop = fake_loop  # type: ignore  # test swaps the Loop class for a stub
    try:
        args = argparse.Namespace(goal=None, goal_file=str(goal), seconds=5, go=False, ask_owner=False,
                                  max_steps=3, allow=None, close=False, allow_small_screen=False)
        with pytest.raises(StopHere):
            agentcomputeruse.cmd_run(args)
    finally:
        loop.Loop = orig  # type: ignore
    assert seen["goal"] == "Open notepad, type \"hi\" (don't save)"
