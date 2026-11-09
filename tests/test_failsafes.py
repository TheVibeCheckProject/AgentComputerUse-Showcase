"""Unit tests for advanced fail-safes: oscillation circuit breaker, hook discrimination,
bounded watchdog, and safe abort checkpointing.
"""
import json
import sys
import time
import types
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import actions
import goals
import hands
import loop
import requests_owner
import screen
from circuit_breaker import CircuitBreaker
from hands import Guard, Hands, Stopped
from loop import Loop, Result


# ------------------------------------------------------------------ Circuit Breaker
def test_circuit_breaker_1_step_repeat():
    cb = CircuitBreaker(window_size=6, repeat_threshold=3)
    r1 = cb.record("click", target_id=1, target_name="Submit")
    assert not r1.tripped

    r2 = cb.record("click", target_id=1, target_name="Submit")
    assert not r2.tripped

    r3 = cb.record("click", target_id=1, target_name="Submit")
    assert r3.tripped
    assert r3.cycle_length == 1
    assert r3.repetitions == 3
    assert "repeated action" in r3.message.lower()


def test_circuit_breaker_2_step_oscillation():
    cb = CircuitBreaker(window_size=6)
    # A -> B -> A -> B
    cb.record("click", target_id=1, target_name="Tab1")
    cb.record("click", target_id=2, target_name="Tab2")
    cb.record("click", target_id=1, target_name="Tab1")
    trip = cb.record("click", target_id=2, target_name="Tab2")
    assert trip.tripped
    assert trip.cycle_length == 2
    assert "2-step oscillation" in trip.message.lower()


def test_circuit_breaker_3_step_cycle():
    cb = CircuitBreaker(window_size=6)
    # A -> B -> C -> A -> B -> C
    cb.record("click", target_id=1, target_name="Step1")
    cb.record("click", target_id=2, target_name="Step2")
    cb.record("click", target_id=3, target_name="Step3")
    cb.record("click", target_id=1, target_name="Step1")
    cb.record("click", target_id=2, target_name="Step2")
    trip = cb.record("click", target_id=3, target_name="Step3")
    assert trip.tripped
    assert trip.cycle_length == 3
    assert "3-step cyclic loop" in trip.message.lower()


def test_circuit_breaker_normal_progression_does_not_trip():
    cb = CircuitBreaker(window_size=6)
    for i in range(10):
        trip = cb.record("click", target_id=i, target_name=f"Button_{i}")
        assert not trip.tripped


def test_scrolling_down_a_page_that_moves_is_progress_not_a_loop():
    # 2026-10-08: scrolls were recorded without direction or screen, so three tripped and five aborted the run.
    cb = CircuitBreaker()
    for n in range(8):
        trip = cb.record("scroll", direction="down", screen=f"page-part-{n}")
        assert not trip.tripped


def test_the_same_action_on_a_screen_that_doesnt_change_still_trips():
    cb = CircuitBreaker()
    trips = [cb.record("scroll", direction="down", screen="the bottom").tripped for _ in range(3)]
    assert trips == [False, False, True]


def test_scrolling_down_then_up_on_the_same_screens_is_an_oscillation():
    cb = CircuitBreaker()
    cb.record("scroll", direction="down", screen="top")
    cb.record("scroll", direction="up", screen="lower")
    cb.record("scroll", direction="down", screen="top")
    assert cb.record("scroll", direction="up", screen="lower").tripped


def test_renumbered_ids_of_the_same_named_button_still_count_as_a_repeat():
    cb = CircuitBreaker()
    results = [cb.record("click", target_id=n, target_name="Submit", screen="s").tripped for n in (4, 9, 13)]
    assert results[-1]


# ------------------------------------------------------------------ The owner's hand
class _Mouse:
    def __init__(self):
        self.position = (0, 0)


def test_the_owner_grabbing_the_mouse_mid_move_stops_the_move(monkeypatch):
    # 2026-10-08: a "driving" flag turned this check off during moves, and the owner's grab was dragged along.
    mouse = _Mouse()
    pauses = []

    def owner_grabs_during_the_fifth_pause(s):
        pauses.append(s)
        if len(pauses) == 5:
            mouse.position = (900, 900)
    monkeypatch.setattr(hands.time, "sleep", owner_grabs_during_the_fifth_pause)
    guard = Guard(tolerance=3, mouse=mouse)
    h = Hands(dry_run=True, guard=guard)
    h.dry_run, h.mouse = False, cast(Any, mouse)       # a stand-in for pynput's mouse Controller
    guard.remember((0, 0))
    with pytest.raises(Stopped, match="moved by someone else"):
        h.move(600, 400, seconds=0.01)
    assert mouse.position == (900, 900)             # left where the owner put it


def test_a_move_nobody_interrupts_lands_on_the_target(monkeypatch):
    monkeypatch.setattr(hands.time, "sleep", lambda s: None)
    mouse = _Mouse()
    guard = Guard(tolerance=3, mouse=mouse)
    h = Hands(dry_run=True, guard=guard)
    h.dry_run, h.mouse = False, cast(Any, mouse)       # a stand-in for pynput's mouse Controller
    guard.remember((0, 0))
    h.move(600, 400, seconds=0.01)
    assert mouse.position == (600, 400)
    guard.check()                                    # and the guard agrees nobody else moved it


# ------------------------------------------------------------------ Waiting for the owner, safe pause
def test_requests_owner_bounded_timeout(monkeypatch):
    monkeypatch.setattr(requests_owner, "WAIT", 0.05)
    with pytest.raises(requests_owner.NoAnswer) as exc:
        requests_owner.ask("choice", "Test question", choices=["A", "B"], wait=0.05, poll=0.01)
    assert "no answer" in str(exc.value).lower()


def test_waiting_for_the_owner_sleeps_between_looks(tmp_path, monkeypatch):
    # 2026-10-08: the pause was deleted and the wait spun a CPU core flat out.
    monkeypatch.setattr(requests_owner, "REQUESTS", tmp_path / "requests")
    monkeypatch.setattr(requests_owner, "ANSWERS", tmp_path / "answers")
    started = time.process_time()
    with pytest.raises(requests_owner.NoAnswer):
        requests_owner.ask("choice", "q", choices=["A"], wait=0.6, poll=0.1)
    assert time.process_time() - started < 0.3


def test_the_owner_gets_30_minutes_by_default(monkeypatch):
    # The agent keeps reminding for ~30 minutes (AlwaysOnAgent core/computer_use.py REMINDERS); 3 gave up early.
    import importlib
    monkeypatch.delenv("ACU_OWNER_WAIT_SECONDS", raising=False)
    try:
        assert importlib.reload(requests_owner).WAIT == 30 * 60
        monkeypatch.setenv("ACU_OWNER_WAIT_SECONDS", "90")
        assert importlib.reload(requests_owner).WAIT == 90
    finally:
        monkeypatch.delenv("ACU_OWNER_WAIT_SECONDS", raising=False)
        importlib.reload(requests_owner)


def test_safe_abort_checkpoint_writes_json_and_leaves_the_screen_alone(tmp_path, monkeypatch):
    import steplog
    monkeypatch.setattr(steplog, "RUNS", tmp_path)
    h = Hands(dry_run=True)
    loop = Loop("Test checkpoint goal", hands=h)
    loop.edge = MagicMock()
    checkpoint = loop._safe_abort_checkpoint("Testing safe abort")

    assert checkpoint["goal"] == "Test checkpoint goal"
    assert checkpoint["reason"] == "Testing safe abort"
    saved = list(tmp_path.glob("checkpoint_*.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text(encoding="utf-8"))["reason"] == "Testing safe abort"
    # 2026-10-08: it pressed Ctrl+W here, closing the page the owner had just been asked about.
    assert h.log == []


# ------------------------------------------------------------------ Per-Monitor DPI & Actions Fail-Safes
def test_screen_dpi_awareness_call():
    res = screen.init_dpi_awareness()
    assert isinstance(res, bool)


def test_bring_to_front_unminimizes_iconic_window(monkeypatch):
    monkeypatch.setattr(actions, "window_in_front", lambda w: True)
    restored: list = []
    monkeypatch.setattr(actions, "_restore_if_minimized", restored.append)
    mock_win = MagicMock()
    mock_win.NativeWindowHandle = 12345
    mock_hands = MagicMock(dry_run=False)

    actions.bring_to_front(mock_win, mock_hands)
    assert restored == [12345]
    mock_hands.click.assert_not_called()


def test_bring_to_front_never_forces_the_window_over_its_own_dialog(monkeypatch):
    # window_in_front counts the window's own dialog (Save As) as "in front": nothing else may happen then.
    monkeypatch.setattr(actions, "window_in_front", lambda w: True)
    monkeypatch.setattr(actions, "_restore_if_minimized", lambda h: None)
    forced: list = []
    monkeypatch.setattr(actions, "_ask_for_foreground", forced.append)
    actions.bring_to_front(MagicMock(NativeWindowHandle=7), MagicMock(dry_run=False))
    assert forced == []


def test_a_button_is_clicked_once_and_nothing_else_is_pressed(monkeypatch):
    # 2026-10-08: a button that looked the same 0.1 s after the click got Enter as well: "Send" twice.
    from PIL import Image

    import ocr
    monkeypatch.setattr(actions, "bring_to_front", lambda w, h: None)
    monkeypatch.setattr(actions, "alive", lambda i: True)
    monkeypatch.setattr(actions.popups, "covered", lambda c: None)
    monkeypatch.setattr(ocr, "grab", lambda r: (Image.new("RGB", (40, 20)), r), raising=False)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    ctrl = MagicMock()
    ctrl.BoundingRectangle = MagicMock(left=10, top=10, right=90, bottom=40)
    item = screen.Item(1, "Button", "Send", (10, 10, 90, 40), control=ctrl)
    h = MagicMock(dry_run=False)
    actions.click(item, MagicMock(), h)
    h.click.assert_called_once_with(50, 25)
    h.press.assert_not_called()



# ------------------------------------------------------------------ Screen size (2026-10-08: runs without a monitor)
def test_a_monitor_switched_on_mid_run_stops_the_action_and_the_new_size_becomes_normal():
    # No action at the old positions; the loop looks again at the new size and carries on (not the end of the run).
    from hands import ScreenChanged
    sizes = [(1024, 768)]
    guard = Guard()
    guard.lock_screen_size((1024, 768), lambda: sizes[-1])
    guard.check()                                   # same size: carry on
    sizes.append((1920, 1080))
    with pytest.raises(ScreenChanged, match="screen changed size"):
        guard.check()
    guard.check()                                   # 1920x1080 is the size now


def test_the_first_action_waits_for_the_monitor_check_and_stops_if_there_is_none():
    import threading
    gate = threading.Event()
    guard = Guard(sleep=lambda s: None)
    guard.gate = gate

    def check_finishes():
        guard.abort_reason = "the screen stayed at 1024x768: is the monitor on and plugged in?"
        gate.set()
    threading.Timer(0.05, check_finishes).start()
    with pytest.raises(Stopped, match="monitor on"):
        guard.check()


def test_without_a_locked_size_the_guard_ignores_screen_size():
    guard = Guard()
    guard.check()


def test_the_small_screen_is_used_only_when_allowed_and_no_monitor_wakes(monkeypatch):
    import display

    def no_monitor(timeout=25.0, min_width=display.MIN_WIDTH):
        raise TimeoutError("the screen stayed at 1024x768: is the monitor on and plugged in?")
    monkeypatch.setattr(display, "wake", no_monitor)
    monkeypatch.setattr(display, "size", lambda: (1024, 768))
    monkeypatch.setattr(display.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError, match="monitor"):
        display.screen_for_run(allow_small=False)
    assert display.screen_for_run(allow_small=True) == ((1024, 768), True)


def test_a_real_monitor_is_always_preferred(monkeypatch):
    import display
    monkeypatch.setattr(display, "wake", lambda timeout=25.0, min_width=display.MIN_WIDTH: (1920, 1080))
    assert display.screen_for_run(allow_small=True) == ((1920, 1080), False)


def test_a_small_screen_that_keeps_changing_is_refused(monkeypatch):
    import display
    readings = iter([(1024, 768), (1920, 1080), (1920, 1080)])

    def no_monitor(timeout=25.0, min_width=display.MIN_WIDTH):
        raise TimeoutError("no")
    monkeypatch.setattr(display, "wake", no_monitor)
    monkeypatch.setattr(display, "size", lambda: next(readings))
    monkeypatch.setattr(display.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError, match="keeps changing"):
        display.screen_for_run(allow_small=True)


# ------------------------------------------------------------------ Opening an app (2026-10-08: first live run)
class _Win:
    def __init__(self, name, pid):
        self.Name, self.ProcessId = name, pid
        self.NativeWindowHandle = 0


def _launch_setup(monkeypatch, results_show, fronts):
    """fronts: what's in front on each look, as (window title, process name); the last one repeats."""
    monkeypatch.setattr(actions, "open_start_menu", lambda h: "Start button")
    monkeypatch.setattr(actions, "_start_results_show", lambda name, *a, **k: results_show)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    seq = list(fronts)
    procs = {}

    def fg():
        title, proc = seq.pop(0) if len(seq) > 1 else seq[0]
        procs[id(title)] = proc
        w = _Win(title, id(title))
        return w
    monkeypatch.setattr(actions.screen, "foreground", fg)
    monkeypatch.setattr(actions.screen, "process_name", lambda pid: procs.get(pid, ""))
    started = []
    monkeypatch.setattr(actions, "_start_directly", lambda name: started.append(name))
    h = MagicMock(dry_run=False)
    h.log = []
    return h, started


def test_enter_waits_for_the_search_result_and_the_app_opens(monkeypatch):
    h, started = _launch_setup(monkeypatch, True, [("Desktop", "explorer.exe"), ("Untitled - Notepad", "notepad.exe")])
    win = actions.launch_app("notepad", h, timeout=2)
    assert win.Name == "Untitled - Notepad" and started == []
    assert [c.args[0] for c in h.press.call_args_list] == ["enter"]


def test_when_enter_doesnt_open_it_the_start_menu_is_closed_and_the_app_started_by_name(monkeypatch):
    # The search stays in front (Enter did nothing), then after Esc + starting it by name, Notepad comes up.
    import itertools
    clock = itertools.count(0, 0.6)
    monkeypatch.setattr(actions.time, "time", lambda: next(clock))
    h, started = _launch_setup(monkeypatch, False, [("Desktop", "explorer.exe")])
    monkeypatch.setattr(actions.screen, "foreground",
                        lambda: _Win("Untitled - Notepad", 2) if started else _Win("Search", 1))
    monkeypatch.setattr(actions.screen, "process_name", lambda pid: {1: "searchhost.exe", 2: "notepad.exe"}[pid])
    win = actions.launch_app("notepad", h, timeout=5)
    assert started == ["notepad"]
    assert [c.args[0] for c in h.press.call_args_list] == ["enter", "esc"]
    assert win.Name == "Untitled - Notepad"


def test_only_plain_program_names_are_started_directly():
    for bad in ("cmd /c del x", "..\\evil", "C:\\Windows\\x.exe", "notepad & calc", "-s", ""):
        with pytest.raises(actions.ActionFailed):
            actions._start_directly(bad)


# ------------------------------------------------------------------ The run watchdog (2026-10-08: a run outlived its limit)
def test_the_watchdog_ends_a_run_stuck_past_its_time_limit():
    now = [0.0]
    guard = Guard(seconds=60, clock=lambda: now[0])
    assert not guard.overdue()
    now[0] = 60 + Guard.WATCHDOG_GRACE + 1
    assert guard.overdue()


def test_the_watchdog_doesnt_count_time_the_owner_is_answering_or_in_control():
    now = [1000.0]
    in_control = [False]
    guard = Guard(seconds=60, clock=lambda: now[0], paused=lambda: in_control[0])
    now[0] += 60 + Guard.WATCHDOG_GRACE + 1
    guard.waiting_for_owner = True
    assert not guard.overdue()
    guard.waiting_for_owner = False
    in_control[0] = True
    assert not guard.overdue()


def test_the_watchdog_thread_calls_its_stop_once_overdue():
    import threading
    now = [0.0]
    fired = threading.Event()
    guard = Guard(seconds=10, clock=lambda: now[0], sleep=lambda s: None)
    now[0] = 10 + Guard.WATCHDOG_GRACE + 5
    guard.start_watchdog(fired.set, poll=0).join(timeout=2)
    assert fired.is_set()


# ------------------------------------------------------------------ Take over opened mid-move (2026-10-08, third live run)
def test_a_mouse_moved_by_opening_take_over_pauses_the_run_instead_of_ending_it():
    mouse = _Mouse()
    in_control = [False]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if s == 0.4:                       # the look-again pause: the Take over flag has landed meanwhile
            in_control[0] = True
        elif s == 0.5:                     # waiting for the owner: they hand back
            in_control[0] = False
    guard = Guard(mouse=mouse, paused=lambda: in_control[0], sleep=sleep)
    guard.remember((100, 100))
    mouse.position = (400, 300)            # moved the instant Take over opened
    guard.check()                          # no Stopped: it waited and carried on
    assert guard.last_pos is None          # and forgot the old position (the owner moved it)


def test_a_mouse_moved_with_nobody_in_take_over_still_stops_the_run_and_says_where():
    mouse = _Mouse()
    guard = Guard(mouse=mouse, paused=lambda: False, sleep=lambda s: None)
    guard.remember((100, 100))
    mouse.position = (400, 300)
    with pytest.raises(Stopped, match=r"left it at 100,100; it was at 400,300"):
        guard.check()


def test_a_move_never_leaves_the_screen(monkeypatch):
    monkeypatch.setattr(hands.time, "sleep", lambda s: None)
    mouse = _Mouse()
    mouse.position = (1000, 700)
    h = Hands(dry_run=True, guard=Guard(mouse=mouse))
    h.dry_run, h.mouse = False, cast(Any, mouse)       # a stand-in for pynput's mouse Controller
    h.screen_bounds = lambda: (0, 0, 1023, 767)
    seen = []

    class Recorder:
        @property
        def position(self):
            return mouse.position

        @position.setter
        def position(self, v):
            seen.append(tuple(v))
            mouse.position = v
    h.mouse = cast(Any, Recorder())
    h.guard._mouse = h.mouse
    for _ in range(20):                    # the curve is random: try many
        h.move(20, 760, seconds=0.01)
        h.move(1010, 20, seconds=0.01)
    assert seen and all(0 <= x <= 1023 and 0 <= y <= 767 for x, y in seen)


# ------------------------------------------------------------------ A person at the computer (2026-10-08, fourth live run)
def _watching_guard(mouse, paused=lambda: False):
    guard = Guard(mouse=mouse, paused=paused, sleep=lambda s: None)
    guard.watching_humans = True             # as if the Windows mouse listener were running
    return guard


def test_cursor_drift_with_no_real_input_doesnt_stop_the_run():
    # The live case: left at 168,93, found at 163,108, and nobody touched the mouse.
    mouse = _Mouse()
    guard = _watching_guard(mouse)
    guard.remember((168, 93))
    mouse.position = (163, 108)
    guard.check()
    assert guard.last_pos == (163, 108)      # carries on from where it is


def test_a_real_mouse_click_or_scroll_stops_the_run():
    for event in (lambda g: g._on_human_click(10, 10, "left", True, False),
                  lambda g: g._on_human_scroll(10, 10, 0, -1, False)):
        guard = _watching_guard(_Mouse())
        event(guard)
        with pytest.raises(Stopped, match="computer's own mouse"):
            guard.check()


def test_a_real_hand_moving_the_mouse_stops_it_but_sensor_jitter_and_injected_moves_dont():
    now = [0.0]
    guard = _watching_guard(_Mouse())
    guard.clock = lambda: now[0]
    guard.deadline = 1e9
    guard._on_human_move(100, 100, False)
    guard._on_human_move(101, 101, False)    # jitter: 2 px
    for x in range(100, 600, 50):
        guard._on_human_move(x, 300, True)   # the tool's own or Take over's moves: injected
    guard.check()
    guard._on_human_move(140, 100, False)    # a bump: 40 px, under the owner's 50 px rule
    guard.check()
    guard._on_human_move(140, 140, False)    # 56 px from where it began: a hand
    with pytest.raises(Stopped):
        guard.check()


def test_without_the_listener_the_position_check_also_allows_50_px():
    # The owner's rule (2026-10-08): taken over means more than 50 px in any direction.
    mouse = _Mouse()
    guard = Guard(mouse=mouse, paused=lambda: False, sleep=lambda s: None)
    guard.remember((100, 100))
    mouse.position = (130, 135)              # 46 px
    guard.check()
    mouse.position = (100, 160)              # 60 px from where the tool left it
    with pytest.raises(Stopped):
        guard.check()


def test_real_input_while_the_owner_is_in_take_over_just_waits():
    in_control = [True]

    def sleep(s):
        in_control[0] = False                # they hand back
    guard = Guard(mouse=_Mouse(), paused=lambda: in_control[0], sleep=sleep)
    guard.watching_humans = True
    guard._on_human_click(1, 1, "left", True, False)
    guard.check()                            # waited, then carried on
    assert not guard.human.is_set()


def test_a_click_lands_on_its_target_even_if_the_cursor_drifted(monkeypatch):
    monkeypatch.setattr(hands.time, "sleep", lambda s: None)
    presses = []

    class DriftingMouse:
        def __init__(self):
            self._pos, self.drifted = (0, 0), False

        @property
        def position(self):
            if self._pos == (168, 93) and not self.drifted:
                self.drifted = True
                self._pos = (163, 108)       # Windows nudges it once, after the move
            return self._pos

        @position.setter
        def position(self, v):
            self._pos = tuple(v)

        def press(self, b):
            presses.append(self._pos)

        def release(self, b):
            pass
    m = DriftingMouse()
    guard = Guard(mouse=m)
    guard.watching_humans = True
    h = Hands(dry_run=True, guard=guard)
    h.dry_run, h.mouse = False, cast(Any, m)
    h.click(168, 93)
    assert presses == [(168, 93)]


# ------------------------------------------------------------------ Fifth live run (2026-10-08)
def test_an_app_that_opens_behind_the_search_is_found_claimed_and_brought_forward(monkeypatch):
    # Notepad opened behind the Start search, which stayed on top: 30 s of waiting, then "didn't come to the front".
    old, new = _Win("Program Manager", 1), _Win("Untitled - Notepad", 2)
    old.NativeWindowHandle, new.NativeWindowHandle = 100, 200
    windows = [old]
    procs = {1: "explorer.exe", 2: "notepad.exe", 3: "searchhost.exe"}
    search = _Win("Search", 3)
    monkeypatch.setattr(actions, "_top_windows", lambda: list(windows))
    monkeypatch.setattr(actions, "open_start_menu", lambda h: "Start button")
    monkeypatch.setattr(actions, "_start_results_show", lambda name, *a, **k: True)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    monkeypatch.setattr(actions.screen, "foreground", lambda: search)        # the search never leaves the front
    monkeypatch.setattr(actions.screen, "process_name", lambda pid: procs[pid])
    fronted, started = [], []
    monkeypatch.setattr(actions, "bring_to_front", lambda w, h: fronted.append(w))
    monkeypatch.setattr(actions, "_start_directly", lambda name: started.append(name))
    h = MagicMock(dry_run=False)
    h.log = []
    h.press.side_effect = lambda k: windows.append(new) if k == "enter" else None   # Enter opens it, behind
    win = actions.launch_app("notepad", h, timeout=5)
    assert win is new and fronted == [new] and started == []                   # no second copy
    assert [c.args[0] for c in h.press.call_args_list] == ["enter", "esc"]     # the search was closed


def test_questions_and_tasks_are_told_apart():
    assert loop.asks_for_information("Find the current temperature in Springfield, Illinois")
    assert loop.asks_for_information("what's on my calendar")
    assert loop.asks_for_information("The weather in Paris?")
    assert not loop.asks_for_information("Open notepad, type a paragraph, then close notepad without saving")
    assert not loop.asks_for_information("Launch calculator and add 2 and 2")


def _view(*texts):
    return screen.View("Program Manager", "explorer.exe", (0, 0, 1024, 768), None,
                       [screen.Item(i + 1, "Text", t, (0, 0, 10, 10)) for i, t in enumerate(texts)])


def test_a_task_can_finish_without_quotes_but_a_question_cant():
    task = Loop("Open notepad, type hello, then close it", hands=Hands(dry_run=True))
    assert task.check({"action": "done", "answer": "typed hello and closed it", "evidence": []}, _view("Recycle Bin")) is None
    q = Loop("Find the weather in Springfield", hands=Hands(dry_run=True))
    assert "evidence" in (q.check({"action": "done", "answer": "71", "evidence": []}, _view("Recycle Bin")) or "")


def test_a_task_done_wrong_is_caught_from_its_steps():
    # The live case: "delete the second half" deleted everything; the trail showed "0 characters".
    lp = Loop("Open notepad, type a paragraph, delete the second half, close notepad without saving",
              hands=Hands(dry_run=True))
    lp.trail = [{"n": 6, "saw": "Notepad is open", "did": "type text='I am an AI assistant...'"},
                {"n": 7, "saw": "The text is typed", "did": "press keys='ctrl+shift+home'"},
                {"n": 8, "saw": "ctrl+shift+home selected the text", "did": "press keys='delete'"},
                {"n": 9, "saw": "The text was deleted, leaving 0 characters in Notepad", "did": "click on 'Close'"}]
    seen = []

    class Brain:
        requests = local_requests = 0

        def plan(self, msgs):
            seen.append(msgs[1]["content"])
            return {"supported": False, "why": "everything was deleted, not the second half"}
    lp.brain = Brain()
    why = lp.verify_answer({"action": "done", "answer": "done"}, _view("Recycle Bin"))
    assert why and "not done right" in why
    assert "0 characters" in seen[0] and "second half" in seen[0]


def test_a_task_done_right_passes_its_double_check():
    lp = Loop("Open notepad, type hello, then close it", hands=Hands(dry_run=True))
    lp.trail = [{"n": 1, "saw": "desktop", "did": "launch_app app='notepad'"}]

    class Brain:
        requests = local_requests = 0

        def plan(self, msgs):
            return {"supported": True, "why": "every step was seen"}
    lp.brain = Brain()
    assert lp.verify_answer({"action": "done", "answer": "typed hello and closed it"}, _view("Recycle Bin")) is None


def test_the_model_is_told_to_edit_text_by_retyping_it_whole():
    assert "COMPLETE new text" in loop.SYSTEM and "ctrl+shift+home" in loop.SYSTEM


def test_computer_use_never_waits_on_the_overloaded_flash_models():
    # 2026-10-08: every live run lost minutes to 3.7/3.6 Flash (503s, 45 s timeouts); the chat runs Flash-Lite first.
    import brain
    for models in (brain.PLAN_MODELS, brain.STEP_MODELS):
        assert models[0] == "gemini:gemini-3.5-flash-lite"
        assert not [m for m in models if "flash" in m and "lite" not in m]


# ------------------------------------------------------------------ Fewer round trips (2026-10-08: 7 requests, 84 s)
class _ChainBrain:
    """Replies in order; counts requests."""
    requests = local_requests = 0

    def __init__(self, replies, verdict=True):
        self.replies, self.verdict, self.calls = list(replies), verdict, 0

    def step(self, msgs):
        self.calls += 1
        return self.replies.pop(0)

    def plan(self, msgs):
        self.calls += 1
        return {"supported": self.verdict, "why": "checked"}


def _notepad_view(text=""):
    return screen.View("Untitled - Notepad", "notepad.exe", (0, 0, 1024, 768), None,
                       [screen.Item(3, "Button", "Close", (990, 0, 1020, 30)),
                        screen.Item(46, "Document", "Text editor", (0, 60, 1024, 700), value=text)])


def test_one_reply_can_carry_the_plan_and_the_whole_notepad_job(monkeypatch):
    goal = "Open notepad, type hello world, delete the second half, close notepad without saving"
    reply = {"previous_ok": None, "observation": "desktop", "action": "launch_app", "app": "notepad",
             "plan": ["open notepad", "type", "retype half", "close"], "done_when": "Notepad is closed",
             "then": [{"action": "type", "kind": "Document", "text": "hello world"},
                      {"action": "type", "kind": "Document", "text": "hello"},
                      {"action": "click", "kind": "Button", "name": "Close"},
                      {"action": "done", "answer": "typed, halved, closed without saving"}]}
    brain = _ChainBrain([reply])
    lp = Loop(goal, hands=Hands(dry_run=True), brain=brain)  # type: ignore[arg-type]  # test double, not a Brain
    acted: list = []
    desktop = screen.View("Program Manager", "explorer.exe", (0, 0, 1024, 768), None, [])
    monkeypatch.setattr(lp, "look", lambda: desktop if not acted else _notepad_view())
    monkeypatch.setattr(lp, "_quick_look", lambda: _notepad_view())
    monkeypatch.setattr(lp, "_is_browser", lambda w: False)
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)          # the Notepad it opened is its own
    monkeypatch.setattr(lp, "_windows_text", lambda: "")
    monkeypatch.setattr(lp, "_wait_while_owner_in_control", lambda: None)
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)

    def act(r, view):
        acted.append((r["action"], r.get("id"), r.get("text")))
        lp.window = object()
        return f"did {r['action']}"
    monkeypatch.setattr(lp, "act", act)
    res = lp.run()
    assert res.status == "done"
    assert acted == [("launch_app", None, None), ("type", 46, "hello world"), ("type", 46, "hello"),
                     ("click", 3, None)]
    assert brain.calls == 2                          # one step request + the double-check (was 7)
    assert lp.plan["done_when"] == "Notepad is closed"


def test_a_chain_stops_at_the_first_surprise_and_the_loop_looks_again(monkeypatch):
    lp = Loop("Open notepad and type hi", hands=Hands(dry_run=True))
    lp.window = object()
    monkeypatch.setattr(lp, "_is_browser", lambda w: False)
    monkeypatch.setattr(lp, "_quick_look", lambda: _notepad_view())
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    acted: list = []

    def act(r, view):
        acted.append(r["action"])
        raise actions.ActionFailed("typing into 'Text editor' didn't stick")
    monkeypatch.setattr(lp, "act", act)
    reply = {"action": "launch_app", "then": [{"action": "type", "kind": "Document", "text": "hi"},
                                              {"action": "click", "kind": "Button", "name": "Close"}]}
    assert lp._chain(reply, _notepad_view(), 1) is None
    assert acted == ["type"]                         # the click never happened
    assert "didn't stick" in lp.history[-1]["result"]


def test_a_chain_target_that_isnt_there_stops_the_chain(monkeypatch):
    lp = Loop("Open notepad and type hi", hands=Hands(dry_run=True))
    lp.window = object()
    monkeypatch.setattr(lp, "_is_browser", lambda w: False)
    empty = screen.View("x", "x.exe", (0, 0, 10, 10), None, [])
    monkeypatch.setattr(lp, "_quick_look", lambda: empty)
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setattr(lp, "act", lambda r, v: pytest.fail("nothing may be done without its target"))
    assert lp._chain({"action": "launch_app", "then": [{"action": "type", "kind": "Document", "text": "hi"}]},
                     empty, 1) is None


def test_the_web_chains_at_most_one_and_never_after_a_page_load(monkeypatch):
    lp = Loop("Find the weather in Paris", hands=Hands(dry_run=True))
    lp.window = object()
    monkeypatch.setattr(lp, "_is_browser", lambda w: True)
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)
    monkeypatch.setattr(lp, "_quick_look", lambda: _notepad_view())
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    acted: list = []
    def _act(r, v):
        acted.append(r["action"])
        return "ok"
    monkeypatch.setattr(lp, "act", _act)
    two = [{"action": "click", "id": 3}, {"action": "click", "id": 3}]
    lp._chain({"action": "type", "id": 46, "text": "x", "then": two}, _notepad_view(), 1)
    assert acted == ["click"]                        # one, not two
    acted.clear()
    lp._chain({"action": "open_url", "url": "x.com", "then": two}, _notepad_view(), 1)
    lp._chain({"action": "type", "id": 46, "text": "x", "submit": True, "then": two}, _notepad_view(), 1)
    assert acted == []                               # a page is loading: look first


def test_desktop_apps_type_fast_and_the_web_at_a_human_pace(monkeypatch):
    lp = Loop("type", hands=Hands(dry_run=True))
    lp.window = object()
    seen = []
    monkeypatch.setattr(loop.tasks, "check_may_type_here", lambda t, w: None)
    monkeypatch.setattr(actions, "type_into", lambda *a, **k: seen.append(lp.hands.typing_delay))
    human = lp.hands.typing_delay
    item_view = _notepad_view()
    monkeypatch.setattr(lp, "_is_browser", lambda w: False)
    lp.act({"action": "type", "id": 46, "text": "hi"}, item_view)
    monkeypatch.setattr(lp, "_is_browser", lambda w: True)
    lp.act({"action": "type", "id": 46, "text": "hi"}, item_view)
    assert seen == [Loop.FAST_TYPING, human] and lp.hands.typing_delay == human


# ------------------------------------------------------------------ Seventh live run (2026-10-08, 20261008-040144)
def _focused_notepad(text=""):
    v = _notepad_view(text)
    v.items[1].focused = True
    return v


def test_selection_and_delete_keys_are_refused_while_a_text_field_has_the_keyboard():
    lp = Loop("Open notepad, type, delete the second half", hands=Hands(dry_run=True))
    for keys in ("ctrl+shift+home", "ctrl+shift+end", "shift+left", "backspace", "delete", "ctrl+x"):
        problem = lp.check({"action": "press", "keys": keys}, _focused_notepad("hello world"))
        assert problem and "COMPLETE new text" in problem, keys
    assert lp.check({"action": "press", "keys": "enter"}, _focused_notepad("x")) is None
    assert lp.check({"action": "press", "keys": "ctrl+s"}, _focused_notepad("x")) is None
    assert lp.check({"action": "press", "keys": "delete"}, _notepad_view("x")) is None    # no field has the keyboard


def test_the_trail_records_what_the_field_really_holds(monkeypatch):
    lp = Loop("Open notepad and type", hands=Hands(dry_run=True))
    lp.trail = [{"n": 2, "saw": "Notepad", "did": "press keys='backspace'"}]
    monkeypatch.setattr(lp, "_quick_look", lambda: _notepad_view("I love helping people with their daily tasks"))
    lp._note_fields()
    assert "fields now: Text editor: 'I love helping people with their daily tasks'" in lp.trail[-1]["did"]


def test_the_task_double_check_is_told_to_trust_the_field_readings():
    lp = Loop("Open notepad, type, delete the second half, close", hands=Hands(dry_run=True))
    lp.trail = [{"n": 2, "saw": "typed", "did": "press keys='backspace' -> fields now: Text editor: 'almost all of it'"}]
    seen = []

    class Brain:
        requests = local_requests = 0

        def check(self, msgs):
            seen.append(msgs)
            return {"supported": False, "why": "only one character was deleted"}
    lp.brain = Brain()
    assert lp.verify_answer({"action": "done", "answer": "deleted the second half"}, _view()) is not None
    assert "fields now" in seen[0][0]["content"] and "almost all of it" in seen[0][1]["content"]


def test_a_chained_type_aimed_at_a_number_from_the_old_list_finds_the_only_text_field():
    # The model chained "type into 1", the desktop's number, after opening Notepad.
    lp = Loop("Open notepad and type hi", hands=Hands(dry_run=True))
    desktop = screen.View("Program Manager", "explorer.exe", (0, 0, 1024, 768), None,
                          [screen.Item(1, "ListItem", "Recycle Bin", (0, 0, 50, 50))])
    resolved = lp._resolve({"action": "type", "id": 1, "text": "hi"}, desktop, _notepad_view())
    assert resolved is not None and resolved["id"] == 46
    two_fields = _notepad_view()
    two_fields.items.append(screen.Item(47, "Edit", "Find", (0, 0, 10, 10)))
    assert lp._resolve({"action": "type", "id": 1, "text": "hi"}, desktop, two_fields) is None   # ambiguous: stop


def test_double_checks_ask_for_a_short_reply(monkeypatch):
    import brain as brain_mod
    seen: dict = {}
    b = brain_mod.Brain(llm=object())
    monkeypatch.setattr(b, "ask", lambda msgs, models, max_tokens=700: seen.update(t=max_tokens) or {})
    b.check([])
    assert seen["t"] <= 250


# ------------------------------------------------------------------ Eighth live run (2026-10-08, 20261008-040926)
def test_notepad_opened_with_old_text_gets_a_page_of_its_own(monkeypatch):
    # Windows 11 Notepad brought back the last run's unsaved text, and each run halved it again.
    lp = Loop("Open notepad and type hi", hands=Hands(dry_run=True))
    lp.hands.dry_run = False                         # the launch itself is faked below
    w = _Win("Untitled - Notepad", 2)
    w.NativeWindowHandle = 200
    monkeypatch.setitem(sys.modules, "uiautomation",
                        types.SimpleNamespace(GetRootControl=lambda: types.SimpleNamespace(GetChildren=list)))
    monkeypatch.setattr(actions, "launch_app", lambda name, h: w)
    monkeypatch.setattr(lp, "_is_notepad", lambda win: True)
    monkeypatch.setattr(actions, "bring_to_front", lambda win, h: None)
    blanks = []
    def _blank_page(task, win, h):
        blanks.append(win)
        return "Untitled"
    monkeypatch.setattr(loop.tasks, "ensure_blank_page", _blank_page)
    result = lp.act({"action": "launch_app", "app": "notepad"}, _view())
    assert blanks == [w] and "own empty page" in result


def test_two_refused_finishes_hand_the_run_to_the_owner(monkeypatch):
    done = {"action": "done", "answer": "halved and closed", "observation": "desktop"}
    brain = _ChainBrain([done, done, done], verdict=False)
    lp = Loop("Open notepad, type, delete the second half, close", hands=Hands(dry_run=True), brain=brain)  # type: ignore[arg-type]  # test double, not a Brain
    monkeypatch.setattr(lp, "look", lambda: _view())
    monkeypatch.setattr(lp, "_windows_text", lambda: "")
    monkeypatch.setattr(lp, "_wait_while_owner_in_control", lambda: None)
    monkeypatch.setattr(lp, "unstick", lambda v: None)
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    res = lp.run()
    assert res.status == "needs_owner" and "didn't agree" in (res.question or "")
    assert brain.calls <= 5                          # two tries and two checks, then it stops


def test_the_model_is_told_how_to_really_discard_notepad_text():
    assert "ctrl+w" in loop.SYSTEM and "Don't save" in loop.SYSTEM


def test_an_app_opened_once_isnt_reopened_to_check(monkeypatch):
    # Ninth live run: Notepad closed, then reopened "to verify", and the restored text was halved again.
    lp = Loop("Open notepad, type, delete the second half, close notepad", hands=Hands(dry_run=True))
    assert lp.check({"action": "launch_app", "app": "notepad"}, _view()) is None
    lp.launched.add("notepad")
    problem = lp.check({"action": "launch_app", "app": "Notepad "}, _view())
    assert problem and "don't reopen it to check" in problem and "reply done" in problem
    assert lp.check({"action": "launch_app", "app": "calc"}, _view()) is None


def test_a_launch_that_failed_can_be_tried_again(monkeypatch):
    lp = Loop("Open notepad", hands=Hands(dry_run=True))
    monkeypatch.setitem(sys.modules, "uiautomation",
                        types.SimpleNamespace(GetRootControl=lambda: types.SimpleNamespace(GetChildren=list)))

    def fails(name, h):
        raise actions.ActionFailed("'notepad' didn't open")
    monkeypatch.setattr(actions, "launch_app", fails)
    with pytest.raises(actions.ActionFailed):
        lp.act({"action": "launch_app", "app": "notepad"}, _view())
    assert lp.check({"action": "launch_app", "app": "notepad"}, _view()) is None


def test_a_blank_page_that_cant_be_confirmed_never_fails_the_launch(monkeypatch):
    # Tenth live run: the launch failed on the blank-page check, the model tried to open Notepad again (refused), stuck.
    lp = Loop("Open notepad and type hi", hands=Hands(dry_run=True))
    lp.hands.dry_run = False
    w = _Win("Untitled - Notepad", 2)
    w.NativeWindowHandle = 200
    monkeypatch.setitem(sys.modules, "uiautomation",
                        types.SimpleNamespace(GetRootControl=lambda: types.SimpleNamespace(GetChildren=list)))
    monkeypatch.setattr(actions, "launch_app", lambda name, h: w)
    monkeypatch.setattr(actions, "bring_to_front", lambda win, h: None)
    monkeypatch.setattr(lp, "_is_notepad", lambda win: True)

    def unconfirmed(task, win, h):
        raise loop.tasks.Unexpected("a new, empty Notepad page after Ctrl+N")
    monkeypatch.setattr(loop.tasks, "ensure_blank_page", unconfirmed)
    result = lp.act({"action": "launch_app", "app": "notepad"}, _view())
    assert "use new_page" in result and lp.window is w and "notepad" in lp.launched


def test_the_slow_local_model_isnt_used_when_stuck_unless_asked(monkeypatch):
    lp = Loop("Open notepad", hands=Hands(dry_run=True))
    monkeypatch.delenv("ACU_LOCAL_LOOK", raising=False)
    monkeypatch.setattr(actions, "clear_popups", lambda w, h: [])
    monkeypatch.setattr(lp, "_screenshot", lambda: pytest.fail("no screenshot for the local model"))
    monkeypatch.setattr(lp, "look", lambda: _view())
    replans = []
    monkeypatch.setattr(lp, "make_plan", lambda v, replan=False: replans.append(replan))
    lp.unstick(_view())
    assert replans == [True]


def test_the_trail_with_field_readings_is_written_to_the_run_log():
    written = []

    class Log:
        run_id = "r1"

        def write(self, rec):
            written.append(rec)

        def end(self, ok, **kw):
            written.append({"event": "end", **kw})
    lp = Loop("Open notepad", hands=Hands(dry_run=True))
    lp.log = Log()
    lp.trail = [{"n": 2, "saw": "Notepad", "did": "type -> fields now: Text editor: 'I am an AI'"}]
    lp._finish(Result("x"), "done")
    trail = next(r for r in written if r["event"] == "trail")
    assert "fields now: Text editor: 'I am an AI'" in trail["steps"][0]["did"]


# ------------------------------------------------------------------ Eleventh live run (2026-10-08): Notepad's text unread
class _DocControl:
    """Windows 11 Notepad's text area: no Value pattern, only the Text pattern."""
    def __init__(self, text):
        self.text = text
        self.SetFocus = lambda: None

    def GetValuePattern(self):
        raise RuntimeError("no Value pattern")

    def GetTextPattern(self):
        doc = types.SimpleNamespace(GetText=lambda n: self.text)
        return types.SimpleNamespace(DocumentRange=doc)


def test_notepads_text_is_read_through_the_text_pattern():
    item = screen.Item(46, "Document", "Text editor", (0, 0, 10, 10), control=_DocControl("I am an AI"))
    assert actions.read_value(item) == "I am an AI"


def test_an_unreadable_field_is_never_reported_as_empty(monkeypatch):
    class Unreadable:
        def GetValuePattern(self):
            raise RuntimeError()

        def GetTextPattern(self):
            raise RuntimeError()
    lp = Loop("Open notepad", hands=Hands(dry_run=True))
    lp.trail = [{"n": 1, "saw": "x", "did": "type"}]
    view = screen.View("Notepad", "notepad.exe", (0, 0, 10, 10), None,
                       [screen.Item(46, "Document", "Text editor", (0, 0, 10, 10), control=Unreadable())])
    monkeypatch.setattr(lp, "_quick_look", lambda: view)
    lp._note_fields()
    assert "Text editor: (couldn't read)" in lp.trail[-1]["did"]
    lp.trail = [{"n": 2, "saw": "x", "did": "type"}]
    view.items[0].control = _DocControl("I am an AI assistant")
    lp._note_fields()
    assert "Text editor: 'I am an AI assistant'" in lp.trail[-1]["did"]


def test_notepad_typing_is_now_read_back_and_line_endings_dont_matter(monkeypatch):
    monkeypatch.setattr(actions, "click", lambda item, win, h: None)
    monkeypatch.setattr(actions, "window_in_front", lambda w: True)
    monkeypatch.setattr(actions, "_field_has_keyboard", lambda item: True)
    monkeypatch.setattr(actions, "alive", lambda item: True)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    doc = _DocControl("")
    item = screen.Item(46, "Document", "Text editor", (0, 0, 10, 10), control=doc)
    h = MagicMock(dry_run=False)
    h.type_text.side_effect = lambda t: setattr(doc, "text", t.replace("\n", "\r"))
    actions.type_into(item, "line one\nline two", None, h)          # read back through the Text pattern: matches
    h.type_text.side_effect = lambda t: setattr(doc, "text", t[: len(t) // 2])
    with pytest.raises(actions.ActionFailed, match="didn't stick"):
        actions.type_into(item, "this text gets cut off", None, h)   # a wrong result is caught now


def test_the_task_double_check_judges_the_result_not_the_method():
    # Twelfth live run: the first half was left (done right), refused twice for "replacing instead of deleting".
    lp = Loop("Open notepad, type, delete the second half, close", hands=Hands(dry_run=True))
    lp.trail = [{"n": 1, "saw": "x", "did": "type -> fields now: Text editor: 'First half.'"}]
    seen = []

    class Brain:
        requests = local_requests = 0

        def check(self, msgs):
            seen.append(msgs[0]["content"])
            return {"supported": True, "why": "the first half is left"}
    lp.brain = Brain()
    assert lp.verify_answer({"action": "done", "answer": "halved"}, _view()) is None
    assert "RESULT, not the method" in seen[0] and "IS the second half deleted" in seen[0]


# ---------------------------------------------------------------- 2026-10-10: the Story1.txt run
def test_slow_typing_is_waited_for_not_deleted_and_typed_again(monkeypatch):
    """The owner watched a 983-character story typed, deleted and typed again: it was read back 0.3 s after typing,
    while the keystrokes were still landing."""
    monkeypatch.setattr(actions, "click", lambda item, win, h: None)
    monkeypatch.setattr(actions, "window_in_front", lambda w: True)
    monkeypatch.setattr(actions, "_field_has_keyboard", lambda item: True)
    monkeypatch.setattr(actions, "alive", lambda item: True)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    story = "Once upon a time in a small, quiet village there lived an old clockmaker."
    reads =["", story[:10], story[:40], story]                 # empty before; then still arriving; complete

    class Growing(_DocControl):
        def GetTextPattern(self):
            class R:
                def GetText(self, n):
                    return reads.pop(0) if reads else story
            return MagicMock(DocumentRange=R())
    item = screen.Item(46, "Document", "Text editor", (0, 0, 10, 10), control=Growing(""))
    h = MagicMock(dry_run=False)
    actions.type_into(item, story, None, h)
    assert h.type_text.call_count == 1                          # typed once
    assert ("ctrl+a",) not in [c.args for c in h.press.call_args_list]


class _DialogWin:
    def __init__(self, cls):
        self.ClassName = cls


def test_a_dialog_opened_by_the_chain_stops_it_before_acting_in_it(monkeypatch):
    """The run's chain: ctrl+s, type Story1.txt (it landed in the Save dialog's Search box), enter, alt+f4 (it
    closed the Save dialog). Now the chain stops when the dialog appears, and the model looks at it."""
    lp = Loop("Open Notepad, type a story, save it as Story1.txt to the desktop", hands=Hands(dry_run=True))
    lp.window = _DialogWin("Notepad")
    acted: list = []
    monkeypatch.setattr(lp, "_is_browser", lambda w: False)
    monkeypatch.setattr(lp, "_quick_look", lambda: _notepad_view("story"))
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)
    monkeypatch.setattr(lp, "_current_window", lambda: _DialogWin("#32770") if acted else _DialogWin("Notepad"))
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)

    def act(r, view):
        acted.append((r["action"], r.get("keys")))
        return "did it"
    monkeypatch.setattr(lp, "act", act)
    reply = {"action": "type", "id": 46, "text": "story",
             "then": [{"action": "press", "keys": "ctrl+s"}, {"action": "type", "id": 17, "text": "Story1.txt"},
                      {"action": "press", "keys": "enter"}, {"action": "press", "keys": "alt+f4"}]}
    assert lp._chain(reply, _notepad_view("story"), 1) is None
    assert acted == [("press", "ctrl+s")]
    assert "a dialog opened" in lp.history[-1]["result"]


def test_a_task_that_saves_a_file_is_checked_on_disk(tmp_path, monkeypatch):
    desk, docs = tmp_path / "Desktop", tmp_path / "Documents"
    desk.mkdir(); docs.mkdir()
    monkeypatch.setattr(goals, "known_folder", lambda name: str(tmp_path / name))
    goal = "Open Notepad, type a 200 word story, save it as Story1.txt to the desktop, and close Notepad."
    start = time.time()
    (docs / "Story1.txt").write_text("x")                     # the wrong place: the goal said the desktop
    problem = loop.saved_file_problem(goal, start)
    assert problem and "'Story1.txt'" in problem and "Desktop" in problem
    (desk / "Story1.txt").write_text("word " * 200)
    assert loop.saved_file_problem(goal, start) is None
    assert loop.saved_file_problem("Open Notepad, type hi, close without saving", start) is None
    assert loop.saved_file_problem("save the notes as notes.md", start) is not None   # Desktop/Documents/Downloads


def test_a_file_name_is_shown_in_the_narration():
    import activity
    assert not activity.secret_like("Story1.txt") and not activity.secret_like("report-2026.pdf")


# ---------------------------------------------------------------- 2026-10-10: the second Story1.txt run (task-097)
def test_a_chained_type_never_puts_different_text_over_a_story(monkeypatch):
    """It had saved fine, didn't notice, and chained 'type the path into 59' (a dialog field that wasn't there): the
    path went into Notepad's page and replaced the story."""
    lp = Loop("Open Notepad, type a story, save it as Story1.txt to the desktop", hands=Hands(dry_run=True))
    lp.window = object()
    story = "Once upon a time, in a small village nestled between rolling green hills, there lived a girl."
    monkeypatch.setattr(lp, "_is_browser", lambda w: False)
    monkeypatch.setattr(lp, "_quick_look", lambda: _notepad_view(story))
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    monkeypatch.setattr(lp, "act", lambda r, v: pytest.fail("the story must not be typed over"))
    for target in ({"id": 59}, {"id": 46}):                      # a number that isn't there; the page itself
        reply = {"action": "press", "keys": "ctrl+s",
                 "then": [{"action": "type", **target, "text": r"C:\Users\owner\Desktop\Story1.txt", "submit": True}]}
        assert lp._chain(reply, _notepad_view(story), 3) is None


def test_the_model_hears_when_the_file_is_saved_and_how_many_words(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "known_folder", lambda name: str(tmp_path / name))
    (tmp_path / "Desktop").mkdir()
    goal = "Open Notepad, type a 200 word story, save it as Story1.txt to the desktop, and close Notepad."
    start = time.time()
    assert "not saved in Desktop yet" in loop.saved_file_note(goal, start)
    f = tmp_path / "Desktop" / "Story1.txt"
    f.write_text(r"C:\Users\owner\Desktop\Story1.txt")            # what the second run left there
    assert "only 1 words" in (loop.saved_file_problem(goal, start) or "")
    f.write_text("word " * 190)
    assert loop.saved_file_problem(goal, start) is None
    assert "saving is DONE" in loop.saved_file_note(goal, start) and "190 words" in loop.saved_file_note(goal, start)


def test_a_refused_done_isnt_accepted_again_with_nothing_done_since(monkeypatch):
    lp = Loop("Open notepad, type hi, close it", hands=Hands(dry_run=True), brain=_ChainBrain([]))  # type: ignore[arg-type]  # test double, not a Brain
    lp.trail = [{"n": 1, "saw": "x", "did": "type -> fields now: Text editor: 'hi'"}]
    monkeypatch.setattr(lp, "_field_readings", list)
    monkeypatch.setattr(lp, "_page_text", lambda view: [])
    lp.brain.verdict = False
    result = lp.verify_task({"answer": "done"}, _view())
    assert result is not None and result.startswith("double-check")
    lp.brain.verdict = True                                     # a kinder second opinion...
    lp.trail.append({"n": 2, "saw": "x", "did": "done"})
    result = lp.verify_task({"answer": "done"}, _view())
    assert result is not None and result.startswith("nothing was done since")
    lp.trail.append({"n": 3, "saw": "x", "did": "press keys='alt+f4'"})
    assert lp.verify_task({"answer": "done"}, _view()) is None      # ...counts once something was done


def test_a_path_is_shown_in_the_narration():
    import activity
    assert not activity.secret_like(r"C:\Users\owner\Desktop\Story1.txt")


def test_a_retype_is_reported_with_what_differed(monkeypatch):
    """The owner saw the page go empty and the story start again, with nothing in the log saying why."""
    monkeypatch.setattr(actions, "click", lambda item, win, h: None)
    monkeypatch.setattr(actions, "window_in_front", lambda w: True)
    monkeypatch.setattr(actions, "_field_has_keyboard", lambda item: True)
    monkeypatch.setattr(actions, "alive", lambda item: True)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    told: list = []
    monkeypatch.setattr(actions, "ON_RETYPE", told.append)
    doc = _DocControl("")
    item = screen.Item(46, "Document", "Text editor", (0, 0, 10, 10), control=doc)
    tries = iter(["Once upon a tim in a village", "Once upon a time in a village"])  # a dropped key, then right
    h = MagicMock(dry_run=False)
    h.type_text.side_effect = lambda t: setattr(doc, "text", next(tries))
    actions.type_into(item, "Once upon a time in a village", None, h)
    assert h.type_text.call_count == 2 and len(told) == 1
    assert told[0].startswith("attempt 1:") and "character 16" in told[0] and "'e upon a tim in" in told[0]
    import activity
    said = activity.say({"message": "retyping: " + told[0]})
    assert said is not None
    text, level = said
    assert level == "warn" and text.startswith("Typed text didn't come out right")


def test_screenshots_only_when_typing_goes_wrong(monkeypatch, tmp_path):
    """The owner: no constant screenshots; the one before a retype stays (it shows what went wrong)."""
    import steplog
    monkeypatch.setattr(steplog, "RUNS", tmp_path)
    log = steplog.StepLog("x", run_id="r")

    class Img:
        def convert(self, mode):
            return self

        def save(self, path, quality=None):
            pass
    monkeypatch.setitem(sys.modules, "ocr", types.SimpleNamespace(grab=lambda: (Img(), None)))
    monkeypatch.setattr(actions, "ON_SNAPSHOT", log.snapshot)
    monkeypatch.setattr(actions, "click", lambda item, win, h: None)
    monkeypatch.setattr(actions, "window_in_front", lambda w: True)
    monkeypatch.setattr(actions, "_field_has_keyboard", lambda item: True)
    monkeypatch.setattr(actions, "alive", lambda item: True)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    story = "word " * 120
    doc = _DocControl("")
    item = screen.Item(46, "Document", "Text editor", (0, 0, 10, 10), control=doc)
    h = MagicMock(dry_run=False)
    tries = iter([story[:300], story])                          # wrong once, then right
    h.type_text.side_effect = lambda t: setattr(doc, "text", next(tries))
    actions.type_into(item, story, None, h)
    labels = [json.loads(l)["label"] for l in (tmp_path / "r" / "log.jsonl").read_text().splitlines()
              if json.loads(l).get("event") == "snapshot"]
    assert labels == ["attempt 1: read back wrong, about to clear and type again"]


# ---------------------------------------------------------------- 2026-10-10: the third Story1.txt run (task-099)
def _save_dialog(file_name):
    return screen.View("Save as", "notepad.exe", (0, 0, 1024, 768), None,
                       [screen.Item(17, "Edit", "Search Box", (700, 10, 900, 30), value=""),
                        screen.Item(58, "Edit", "File name:", (100, 600, 600, 620), value=file_name),
                        screen.Item(59, "Button", "Save", (800, 700, 880, 730)),
                        screen.Item(60, "Button", "Cancel", (900, 700, 980, 730))])


def test_save_isnt_clicked_or_approved_until_the_file_name_is_right(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "known_folder", lambda name: str(tmp_path / name))
    lp = Loop("Open Notepad, type a 200 word story, save it as Story1.txt to the desktop, and close Notepad.",
              hands=Hands(dry_run=True))
    lp.window = types.SimpleNamespace(ClassName="#32770")
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)
    for a in ("needs_approval", "click"):
        problem = lp.check({"action": a, "id": 59, "reason": "overwrite Story1"},
                           _save_dialog("Once upon a time in a small, quiet"))
        assert problem and "'File name' box holds 'Once upon a time" in problem and "item 58" in problem
    full = str(tmp_path / "Desktop" / "Story1.txt")
    assert lp.check({"action": "click", "id": 59}, _save_dialog(full)) is None
    assert lp.check({"action": "click", "id": 59}, _save_dialog("Story1")) is None


def test_the_app_cant_be_closed_before_the_file_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "known_folder", lambda name: str(tmp_path / name))
    (tmp_path / "Desktop").mkdir()
    lp = Loop("Open Notepad, type a 200 word story, save it as Story1.txt to the desktop, and close Notepad.",
              hands=Hands(dry_run=True))
    lp.started_at = time.time()
    lp.window = types.SimpleNamespace(ClassName="Notepad")
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)
    view = _notepad_view("a story")
    assert "isn't saved yet" in (lp.check({"action": "press", "keys": "alt+f4"}, view) or "")
    assert "isn't saved yet" in (lp.check({"action": "click", "id": 3}, view) or "")  # the Close button
    (tmp_path / "Desktop" / "Story1.txt").write_text("word " * 200)
    assert lp.check({"action": "press", "keys": "alt+f4"}, view) is None
    lp.window = types.SimpleNamespace(ClassName="#32770")                            # a dialog's Close: fine
    (tmp_path / "Desktop" / "Story1.txt").unlink()
    assert lp.check({"action": "press", "keys": "alt+f4"}, view) is None


# ---------------------------------------------------------------- 2026-10-10: save_as, the tool does the Save dialog
def test_save_as_is_one_action_the_tool_does_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "known_folder", lambda name: str(tmp_path / name))
    lp = Loop("Open Notepad, type a 200 word story, save it as Story1.txt to the desktop, and close Notepad.",
              hands=Hands(dry_run=True))
    lp.window = object()
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)
    monkeypatch.setattr(loop.tasks, "check_may_type_here", lambda task, win: None)
    assert "save_as" in loop.ACTIONS and "save_as" not in Loop.CHAINABLE      # its own step: approvals work
    assert lp.check({"action": "save_as", "path": "Story1.txt"}, _notepad_view("story")) is None
    assert lp.act({"action": "save_as", "path": "Story1.txt"}, _notepad_view("story")) == \
        f"would save as {tmp_path / 'Desktop' / 'Story1.txt'}"                # a bare name goes where the goal says
    assert '"action": "save_as"' in loop.SYSTEM and "Never press ctrl+s" in loop.SYSTEM
    import activity
    assert activity.say({"action": "save_as", "path": r"C:\Users\owner\Desktop\Story1.txt"}) == \
        (r"Saving as C:\Users\owner\Desktop\Story1.txt", "info")


def test_only_a_clearly_short_story_is_sent_back_before_typing(monkeypatch):
    """"A 200 word story" means about 200 (the owner: "it doesn't have to be perfect"): 167 is fine, 60 isn't."""
    lp = Loop("Open Notepad, type a 200 word story, save it as Story1.txt to the desktop", hands=Hands(dry_run=True))
    lp.window = object()
    monkeypatch.setattr(lp, "_may_touch", lambda w: True)
    view = _notepad_view("")
    problem = lp.check({"action": "type", "id": 46, "text": "word " * 60}, view)
    assert problem and "60 words" in problem and "200" in problem
    assert lp.check({"action": "type", "id": 46, "text": "word " * 167}, view) is None     # about right is right
    plain = Loop("Open Notepad and type hello", hands=Hands(dry_run=True))
    plain.window = object()
    monkeypatch.setattr(plain, "_may_touch", lambda w: True)
    assert plain.check({"action": "type", "id": 46, "text": "hello"}, view) is None



def test_old_runs_are_deleted_but_the_newest_are_kept(monkeypatch, tmp_path):
    """The owner: the images mustn't add up. Older than 14 days goes, the newest 30 always stay."""
    import os

    import steplog
    monkeypatch.setattr(steplog, "RUNS", tmp_path)
    now = time.time()
    for i in range(40):
        d = tmp_path / f"run-{i:02d}"
        d.mkdir()
        age = (40 - i) * 86400                                     # run-00 is 40 days old, run-39 one day
        os.utime(d, (now - age, now - age))
    assert steplog.prune(now) == 10                               # the 10 oldest: over 14 days and beyond the newest 30
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left[0] == "run-10" and len(left) == 30
    for d in tmp_path.iterdir():                                  # all old, but the newest 30 stay
        os.utime(d, (now - 100 * 86400, now - 100 * 86400))
    assert steplog.prune(now) == 0


def test_old_needs_you_files_are_deleted(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(requests_owner, "REQUESTS", tmp_path / "requests")
    monkeypatch.setattr(requests_owner, "ANSWERS", tmp_path / "answers")
    (tmp_path / "requests").mkdir(); (tmp_path / "answers").mkdir()
    now = time.time()
    old, new = tmp_path / "requests" / "a.png", tmp_path / "requests" / "b.png"
    old.write_bytes(b"x"); new.write_bytes(b"x")
    os.utime(old, (now - 20 * 86400, now - 20 * 86400))
    assert requests_owner.prune(now) == 1 and not old.exists() and new.exists()


# ---------------------------------------------------------------- 2026-10-11: the owner's tab in front of the task
class _Win2:
    def __init__(self, handle, name, cls="Chrome_WidgetWin_1", pid=1):
        self.NativeWindowHandle, self.Name, self.ClassName, self.ProcessId = handle, name, cls, pid
        self.BoundingRectangle = types.SimpleNamespace(width=lambda: 800, left=0, top=0, right=800, bottom=600)


def test_another_window_in_front_never_takes_the_task_over(monkeypatch):
    """The owner: "a tab should not stop a run... read the Pinterest URL and know it's not right"."""
    lp = Loop("Download Form W-9 from irs.gov", hands=Hands(dry_run=True))
    own = _Win2(111, "about:blank - Microsoft Edge")
    theirs = _Win2(222, "Pinterest Login - Personal - Microsoft Edge")
    lp.task.opened(own)
    lp.edge = lp.window = own
    monkeypatch.setattr(loop.screen, "foreground", lambda: theirs)
    monkeypatch.setattr(loop.screen, "process_name", lambda pid: "msedge.exe")
    monkeypatch.setattr(loop.tasks, "root_owner", lambda h: 0)
    fronted = []
    monkeypatch.setattr(actions, "bring_to_front", lambda w, h: fronted.append(w.NativeWindowHandle))
    assert lp._current_window() is own and fronted == [111]
    # its own window in front: used as is; no window of its own yet: whatever is in front
    monkeypatch.setattr(loop.screen, "foreground", lambda: own)
    assert lp._current_window() is own
    fresh = Loop("x", hands=Hands(dry_run=True))
    monkeypatch.setattr(loop.screen, "foreground", lambda: theirs)
    assert fresh._current_window() is theirs


def test_pop_ups_are_only_answered_in_the_tasks_own_windows(monkeypatch):
    lp = Loop("Download Form W-9 from irs.gov", hands=Hands(dry_run=True))
    lp.task.opened(_Win2(111, "mine"))
    lp.window = _Win2(222, "Pinterest Login - Personal - Microsoft Edge")
    monkeypatch.setattr(loop.tasks, "root_owner", lambda h: 0)
    monkeypatch.setattr(actions, "clear_popups", lambda w, h: pytest.fail("answered a pop-up in the owner's window"))
    monkeypatch.setattr(lp, "make_plan", lambda view, replan=False: None)
    monkeypatch.setattr(lp, "look", lambda: None)
    lp.unstick(_view())


def test_the_tools_edge_window_is_only_ever_its_own(monkeypatch):
    """A Google sign-in window the owner's Pinterest tab opened while the tool started Edge became "the task's own"
    and the run typed into the owner's Edge (2026-10-11)."""
    import browser
    root: list = []
    monkeypatch.setitem(sys.modules, "uiautomation", types.SimpleNamespace(
        GetRootControl=lambda: types.SimpleNamespace(GetChildren=lambda: list(root))))
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(browser.time, "sleep", lambda s: None)
    theirs = types.SimpleNamespace(ClassName=browser.EDGE_CLASS, NativeWindowHandle=5, Name="Sign in - Google Accounts - Personal", ProcessId=100)
    mine = types.SimpleNamespace(ClassName=browser.EDGE_CLASS, NativeWindowHandle=6, Name="about:blank - Microsoft Edge", ProcessId=200)
    calls = {"n": 0}

    def pids(profile):
        calls["n"] += 1
        if calls["n"] == 1:
            root.append(theirs)                                    # the owner's window appears first
        elif calls["n"] == 2:
            root.append(mine)
        return {200}
    monkeypatch.setattr(browser, "profile_pids", pids)
    assert browser.open_edge() is mine


# ---------------------------------------------------------------- 2026-10-11: focus, blind clicks, minimize all
def test_a_title_bar_click_never_lands_on_a_window_covering_the_task(monkeypatch):
    """With the owner's maximized Pinterest window over the task's Edge, the "title bar" click hit their tab."""
    win = types.SimpleNamespace(NativeWindowHandle=111, BoundingRectangle=types.SimpleNamespace(
        left=0, top=0, right=1000, bottom=800, width=lambda: 1000))
    h = MagicMock(dry_run=False)
    monkeypatch.setattr(actions, "_restore_if_minimized", lambda hwnd: None)
    monkeypatch.setattr(actions, "window_in_front", lambda w: False)
    monkeypatch.setattr(actions, "_ask_for_foreground", lambda hwnd: None)
    monkeypatch.setattr(actions, "_point_is_on", lambda hwnd, x, y: False)          # another window is there
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    with pytest.raises(actions.ActionFailed, match="covering the task's own window"):
        actions.bring_to_front(win, h)
    h.click.assert_not_called()
    monkeypatch.setattr(actions, "_point_is_on", lambda hwnd, x, y: True)           # its own title bar: fine
    with pytest.raises(actions.ActionFailed):
        actions.bring_to_front(win, h)
    h.click.assert_called_once()


def test_every_real_run_starts_by_minimizing_everything(monkeypatch, _never_minimize_the_real_windows):
    """The owner: "it needs to minimize everything and open its own window every time it runs a new task"."""
    lp = Loop("Open Notepad", hands=Hands(dry_run=True))
    lp.hands.dry_run = False
    monkeypatch.setattr(lp, "_try_recipe", lambda: (_ for _ in ()).throw(RuntimeError("stop here")))
    with pytest.raises(RuntimeError):
        lp.run()
    assert _never_minimize_the_real_windows == [1]


def test_a_failed_step_says_why_in_the_narration():
    import activity
    assert activity.say({"message": "that didn't work: another window is covering the task's own window"}) == \
        ("That didn't work: another window is covering the task's own window", "warn")


# ---------------------------------------------------------------- 2026-10-11: open_file
def test_open_file_is_one_action_and_finds_a_bare_name(tmp_path, monkeypatch):
    """Asked to open Story1.txt and read it, the model clicked Open four times and closed Notepad before reading."""
    monkeypatch.setattr(goals, "known_folder", lambda name: str(tmp_path / name))
    (tmp_path / "Desktop").mkdir()
    story = tmp_path / "Desktop" / "Story1.txt"
    story.write_text("Once upon a time in a small village. More text.", encoding="utf-8")
    lp = Loop("Open Story1.txt from the desktop in Notepad and tell me its first sentence", hands=Hands(dry_run=True))
    assert "open_file" in loop.ACTIONS and "open_file" not in Loop.CHAINABLE and '"action": "open_file"' in loop.SYSTEM
    assert lp.act({"action": "open_file", "path": "Story1.txt"}, _notepad_view()) == f"would open {story}"
    with pytest.raises(actions.ActionFailed, match="no file named"):
        lp.act({"action": "open_file", "path": "Nope.txt"}, _notepad_view())
    import activity
    assert activity.say({"action": "open_file", "path": str(story)}) == (f"Opening the file {story}", "info")


def test_what_the_tool_read_exactly_counts_as_evidence_after_the_window_closed(monkeypatch):
    """ "Read it, then close it": the answer's evidence is gone from the screen, but the tool read it from disk."""
    lp = Loop("Open Story1.txt and tell me its first sentence, then close Notepad", hands=Hands(dry_run=True))
    lp.history.append({"action": "open_file", "expect": None, "result": "opened C:\\x\\Story1.txt; it starts (read "
                       "from the disk, exact): 'Once upon a time in a small village nestled between two hills.'"})
    desktop = screen.View("Program Manager", "explorer.exe", (0, 0, 1024, 768), None, [])
    monkeypatch.setattr(lp, "_page_text", lambda view: [])
    reply = {"action": "done", "answer": "The first sentence is: Once upon a time in a small village nestled between two hills.",
             "evidence": ["Once upon a time in a small village nestled between two hills."]}
    assert "isn't on the current page" not in (lp.check(reply, desktop) or "")


def test_done_is_refused_while_an_app_the_goal_says_to_close_is_still_open(monkeypatch):
    """task-108 said "closed Notepad" and was accepted; the Notepad it opened stood open."""
    lp = Loop("Open Story1.txt in Notepad, tell me its first sentence, then close Notepad without saving anything.",
              hands=Hands(dry_run=True))
    note = types.SimpleNamespace(NativeWindowHandle=77, Name="Story1.txt - Notepad")
    lp.task.opened(note)
    root = [note]
    monkeypatch.setitem(sys.modules, "uiautomation", types.SimpleNamespace(
        GetRootControl=lambda: types.SimpleNamespace(GetChildren=lambda: list(root))))
    assert "still open" in (lp._still_open_problem() or "")
    root.clear()
    assert lp._still_open_problem() is None
    assert Loop("Open Notepad and type hi", hands=Hands(dry_run=True))._still_open_problem() is None


def test_buttons_and_places_are_never_the_subject_of_an_answer():
    """ "click the page's Choose File button" made "choose" the subject and refused the right answer twice."""
    subjects = loop.goal_subjects("Go to the page, click the page's Choose File button and choose Story1.txt from my "
                                  "Desktop. Do not click Submit. Tell me the file name the page shows.")
    assert not {"choose", "file", "desktop", "submit", "do"} & set(subjects)
    assert loop.goal_subjects("Find the current temperature in Springfield, Illinois") == ["springfield", "illinois"]


def test_ocr_spaces_in_a_name_dont_refuse_a_right_answer(monkeypatch):
    """OCR read "Story1.txt" as "Story 1 .txt"; the subject check refused the right answer."""
    lp = Loop("Choose Story1.txt on the page and tell me the file name it shows", hands=Hands(dry_run=True))
    view = screen.View("W3Schools", "msedge.exe", (0, 0, 1000, 800), None, [])
    monkeypatch.setattr(lp, "_page_text", lambda v: ["Select a file: Story 1 .txt"])
    reply = {"action": "done", "answer": "It shows Story1.txt", "evidence": ["Select a file: Story 1 .txt"]}
    assert lp.check(reply, view) is None


# ---------------------------------------------------------------- 2026-10-11: the untuned batch's failures
def test_typing_follows_the_keyboard_to_the_box_a_click_opened(monkeypatch):
    """Wikipedia's and ssa.gov's search: clicking the box opens ANOTHER box; "the keyboard isn't on 'Search'"."""
    monkeypatch.setattr(actions, "click", lambda item, win, h: None)
    monkeypatch.setattr(actions, "window_in_front", lambda w: True)
    monkeypatch.setattr(actions, "_field_has_keyboard", lambda item: False)
    monkeypatch.setattr(actions, "alive", lambda item: True)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    overlay = _DocControl("")
    moved = screen.Item(-1, "Edit", "Search Wikipedia", (0, 0, 10, 10), control=overlay)
    monkeypatch.setattr(actions, "_text_field_with_keyboard", lambda win: moved)
    clicked = screen.Item(9, "Button", "Search Wikipedia", (0, 0, 10, 10), control=_DocControl(""))
    h = MagicMock(dry_run=False)
    h.type_text.side_effect = lambda t: setattr(overlay, "text", t)
    actions.type_into(clicked, "Golden Gate Bridge", None, h)
    assert overlay.text == "Golden Gate Bridge" and h.type_text.call_count == 1
    # a password never follows the keyboard elsewhere
    monkeypatch.setattr(actions, "popups", types.SimpleNamespace(find=lambda w, c: None))
    with pytest.raises(actions.ActionFailed):
        actions.type_into(clicked, "secret", None, h, secret=True)


def test_a_quote_copied_with_the_lists_formatting_still_counts():
    assert loop.unlist("Button 'Choose File Story1.txt'") == "Choose File Story1.txt"
    assert loop.unlist("[12] Text “Today, High: 92°”") == "Today, High: 92°"
    assert loop.unlist("Today, High: 92° Low: 65°") == "Today, High: 92° Low: 65°"


def test_a_field_the_click_missed_is_focused_directly(monkeypatch):
    """Wikipedia at ~920 px: the click hit empty page, the keyboard stayed on the Document, typing failed 4 times."""
    monkeypatch.setattr(actions, "click", lambda item, win, h: None)
    monkeypatch.setattr(actions, "window_in_front", lambda w: True)
    monkeypatch.setattr(actions, "alive", lambda item: True)
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    box = _DocControl("")
    state = {"focused": False}
    box.SetFocus = lambda: state.update(focused=True)
    monkeypatch.setattr(actions, "_field_has_keyboard", lambda item: state["focused"])
    field = screen.Item(91, "Edit", "Search Wikipedia", (0, 0, 10, 10), control=box)
    h = MagicMock(dry_run=False)
    h.type_text.side_effect = lambda t: setattr(box, "text", t)
    actions.type_into(field, "Springfield, Illinois", None, h)
    assert box.text == "Springfield, Illinois" and h.type_text.call_count == 1


def test_the_tools_edge_opens_maximized():
    import browser
    assert "--start-maximized" in browser.launch_args("about:blank")


def test_a_forward_slash_path_is_opened_in_windows_form(tmp_path, monkeypatch):
    """The Open window rejected "C:/Projects/..." with an error box (2026-10-11 batch 3)."""
    f = tmp_path / "pic.png"
    f.write_bytes(b"x")
    lp = Loop("x", hands=Hands(dry_run=True))
    got = lp._find_file(str(f).replace("\\", "/"))
    assert "/" not in got and got == str(f)


def test_a_text_file_with_no_app_opens_in_notepad(tmp_path, monkeypatch):
    """.md had no app on the Mini: Windows asked "How do you want to open this file?" (2026-10-11 batch 4)."""
    f = tmp_path / "README.md"
    f.write_text("# Title\n", encoding="utf-8")
    monkeypatch.setattr(Loop, "_has_app", staticmethod(lambda p: False))
    cmd = Loop._opener(str(f))
    assert cmd is not None and cmd[0].lower().endswith("notepad.exe") and cmd[1] == str(f)


def test_the_app_the_task_names_wins(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(Loop, "_has_app", staticmethod(lambda p: True))
    assert Loop._opener(str(f)) is None                       # its usual app
    opener = Loop._opener(str(f), "notepad")
    assert opener is not None and opener[0].lower().endswith("notepad.exe")


def test_a_binary_file_with_no_app_is_refused(tmp_path, monkeypatch):
    f = tmp_path / "blob.xyz"
    f.write_bytes(bytes([0, 1, 2, 255]))
    monkeypatch.setattr(Loop, "_has_app", staticmethod(lambda p: False))
    with pytest.raises(actions.ActionFailed):
        Loop._opener(str(f))


def test_a_minimized_window_is_not_the_screen(monkeypatch):
    """After minimize-all, Windows still calls an earlier task's minimized Notepad "in front": it was read as the
    screen and its README taken for the one asked for (2026-10-11 batch 5)."""
    old = MagicMock(Name="README.md - Notepad", NativeWindowHandle=111, ProcessId=1)
    desk = MagicMock(Name="Program Manager", NativeWindowHandle=222)
    monkeypatch.setattr(screen, "foreground", lambda: old)
    monkeypatch.setattr(loop, "_minimized", lambda w: w is old)
    monkeypatch.setattr(loop, "_desktop", lambda: desk)
    root = MagicMock()
    root.GetChildren.return_value = [old]
    import uiautomation as auto
    monkeypatch.setattr(auto, "GetRootControl", lambda: root)
    lp = Loop("x", hands=Hands(dry_run=True))
    assert lp._current_window() is desk


def test_a_chained_answer_to_a_question_is_a_guess(monkeypatch):
    """open_file, then a chained done with a made-up heading (2026-10-11 batch 6): stop and look first."""
    lp = Loop("Open C:/x/TOOLS.md and tell me its first heading.", hands=Hands(dry_run=True))
    lp.window = MagicMock()
    monkeypatch.setattr(lp, "_is_browser", lambda w: False)
    monkeypatch.setattr(lp, "_is_dialog", lambda w: False)
    monkeypatch.setattr(lp, "_current_window", lambda: lp.window)
    reply = {"action": "open_file", "then": [{"action": "done", "answer": "# Made up", "evidence": ["# Made up"]}]}
    assert lp._chain(reply, MagicMock(items=[]), 1) is None
    assert "look first" in lp.history[-1]["result"]


def test_a_corrected_answer_is_checked_afresh(monkeypatch):
    """The repeat guard refuses the SAME claim with nothing done since, not a corrected one (2026-10-11 batch 6)."""
    lp = Loop("Open notepad and type hi", hands=Hands(dry_run=True))
    lp._last_refusal = (0, "double-check: wrong heading", "# made up")
    monkeypatch.setattr(goals, "saved_file_problem", lambda *a: None)
    monkeypatch.setattr(lp, "_still_open_problem", lambda: None)
    monkeypatch.setattr(lp, "_double_check", lambda msgs: {"supported": True, "why": "ok"})
    monkeypatch.setattr(lp, "_title", lambda: "")
    monkeypatch.setattr(lp, "_field_readings", list)
    monkeypatch.setattr(lp, "_page_text", lambda v: [])
    result = lp.verify_task({"answer": "# Made up"}, MagicMock())
    assert result is not None and result.startswith("nothing was done")
    assert lp.verify_task({"answer": "# Tools: the real one"}, MagicMock()) is None


def test_a_windows_line_break_is_one_enter():
    """pynput types "\r" and "\n" each as Enter: "\r\n" made blank lines (2026-10-11 batch 8)."""
    h = Hands(dry_run=True)
    h.dry_run = False
    typed: list = []
    h.keyboard = MagicMock()
    h.keyboard.type.side_effect = typed.append
    h.typing_delay = (0, 0)
    with patch("hands.caps_lock_off", return_value=False):
        h.type_text("1. Milk\r\n2. Eggs\r3. Bread")
    assert "".join(typed) == "1. Milk\n2. Eggs\n3. Bread"


def test_retyping_what_the_field_already_holds_is_refused():
    """OCR read the cursor as "Breadl"; the field (read exactly) held the right list (2026-10-11 batch 8)."""
    lp = Loop("Open Notepad and write a list", hands=Hands(dry_run=True))
    item = screen.Item(id=5, kind="Document", name="Text editor", rect=(0, 0, 100, 100), value="1. Milk\r2. Eggs")
    view = MagicMock(items=[item])
    lp.window = None
    problem = lp.check({"action": "type", "id": 5, "text": "1. Milk\r\n2. Eggs"}, view)
    assert problem and "already holds exactly" in problem
    assert lp.check({"action": "type", "id": 5, "text": "1. Milk\n2. Eggs\n3. Bread"}, view) is None


def test_a_task_done_without_a_summary_is_fine():
    """A task that only does something needs no summary line; a question still needs its answer (batch 9)."""
    lp = Loop("Open Notepad, write a note, and save it as Note.txt on the Desktop.", hands=Hands(dry_run=True))
    reply = {"action": "done"}
    assert lp.check(reply, MagicMock(items=[])) is None and reply["answer"].startswith("Done:")
    lq = Loop("Open C:/x/a.md and tell me its first heading.", hands=Hands(dry_run=True))
    assert lq.check({"action": "done"}, MagicMock(items=[])) == "done needs the answer"
