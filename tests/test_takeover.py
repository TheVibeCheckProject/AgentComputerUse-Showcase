"""Take over: the in-control flag, the owner's input mapped to the screen, the task pausing and looking again after a
hand-back. Stand-in mouse and keyboard: nothing really moves or types."""
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hands  # noqa: E402
import loop  # noqa: E402
import takeover  # noqa: E402
from hands import Guard, Hands  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, "DATA", tmp_path)
    monkeypatch.setattr(takeover, "STATE", tmp_path / "takeover.json")
    monkeypatch.setattr(takeover, "_monitor", lambda: (0, 0, 1000, 500))
    monkeypatch.setitem(sys.modules, "display", types.SimpleNamespace(wake=lambda timeout=0: (1000, 500),
                                                                      nudge=lambda *a, **k: None))
    return tmp_path


class FakeMouse:
    def __init__(self):
        self.position = (0, 0)
        self.events = []

    def click(self, button, count):
        self.events.append(("click", button.name, count, self.position))

    def press(self, button):
        self.events.append(("press", button.name, self.position))

    def release(self, button):
        self.events.append(("release", button.name, self.position))

    def scroll(self, dx, dy):
        self.events.append(("scroll", dy, self.position))


class FakeKeyboard:
    def __init__(self):
        self.events = []

    def type(self, text):
        self.events.append(("type", text))

    def press(self, k):
        self.events.append(("press", str(k)))

    def release(self, k):
        self.events.append(("release", str(k)))


@pytest.fixture
def devices(monkeypatch):
    m, k = FakeMouse(), FakeKeyboard()
    monkeypatch.setattr(takeover, "_controllers", lambda: (m, k))
    monkeypatch.setattr(takeover.time, "sleep", lambda s: None)
    return m, k


def test_taking_over_and_handing_back(state):
    assert not takeover.active()
    s = takeover.start("req1")
    assert s["active"] and s["request_id"] == "req1" and s["screen"] == [1000, 500]
    assert takeover.active()
    assert takeover.release() == "req1"
    assert not takeover.active()


def test_it_lets_go_after_being_idle(state, monkeypatch):
    takeover.start()
    now = takeover._now()
    monkeypatch.setattr(takeover, "_now", lambda: now + takeover.IDLE_RELEASE + 1)
    assert takeover.status() == {"active": False, "released": "idle"}
    assert not takeover.STATE.exists()



def test_view_only_pictures_dont_keep_a_forgotten_take_over_alive(state, monkeypatch):
    """2026-10-10: a Take over left open the evening before stayed "in control" for 17 hours because View only's
    pictures refreshed it, and a computer task waited for a hand-back that never came. Only real pictures count."""
    calls = []
    monkeypatch.setattr(takeover, "touch", lambda: calls.append("touch"))
    monkeypatch.setattr(takeover, "_keep_awake", lambda: None)

    class Shot:
        size, rgb = (4, 2), bytes(4 * 2 * 3)

    class FakeMSS:
        monitors = [None, {"left": 0, "top": 0, "width": 4, "height": 2}]
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def grab(self, m): return Shot()

    monkeypatch.setitem(sys.modules, "mss", types.SimpleNamespace(MSS=FakeMSS))
    sys.modules["display"]._dpi_aware = lambda: None
    takeover.frame(view_only=True)
    assert calls == []
    takeover.frame()
    assert calls == ["touch"]

def test_input_only_while_in_control(state, devices):
    with pytest.raises(takeover.TakeoverError, match="take over first"):
        takeover.apply({"type": "tap", "x": 0.5, "y": 0.5})
    assert devices[0].events == []


def test_taps_land_where_the_owner_tapped_on_the_picture(state, devices):
    m, k = devices
    takeover.start()
    takeover.apply({"type": "tap", "x": 0.5, "y": 0.5})
    takeover.apply({"type": "long_press", "x": 0.1, "y": 0.2})
    takeover.apply({"type": "double_tap", "x": 1, "y": 1})
    assert m.events == [("click", "left", 1, (500, 250)), ("click", "right", 1, (100, 100)),
                        ("click", "left", 2, (999, 499))]               # the far edge stays on the screen
    with pytest.raises(takeover.TakeoverError):
        takeover.apply({"type": "tap", "x": 1.5, "y": 0.5})
    with pytest.raises(takeover.TakeoverError):
        takeover.apply({"type": "tap"})


def test_drag_scroll_text_and_keys(state, devices):
    m, k = devices
    takeover.start()
    takeover.apply({"type": "drag", "x": 0, "y": 0, "x2": 0.5, "y2": 0.5})
    assert m.events[0] == ("press", "left", (0, 0)) and m.events[-1] == ("release", "left", (500, 250))
    m.events.clear()
    assert takeover.apply({"type": "scroll", "dy": 99}) == "scrolled +15"          # capped
    takeover.apply({"type": "text", "text": "Hëllo"})
    takeover.apply({"type": "key", "key": "Enter"})
    takeover.apply({"type": "key", "key": "ctrl+a"})
    assert ("type", "Hëllo") in k.events
    assert [e for e in k.events if e[0] == "press"][0][1].endswith("enter")
    with pytest.raises(takeover.TakeoverError, match="unknown key"):
        takeover.apply({"type": "key", "key": "win+r"})                 # not on the list
    with pytest.raises(takeover.TakeoverError):
        takeover.apply({"type": "text", "text": "x" * (takeover.MAX_TEXT + 1)})


def test_the_guard_waits_while_the_owner_is_in_control():
    """The task doesn't stop because the owner moved the mouse during Take over; it waits, and the wait doesn't
    count against its time limit."""
    t = {"now": 0.0}
    flag = {"on": True}

    def sleep(s):
        t["now"] += s
        if t["now"] >= 3:
            flag["on"] = False

    mouse = types.SimpleNamespace(position=(400, 400))
    g = Guard(seconds=10, mouse=mouse, clock=lambda: t["now"], paused=lambda: flag["on"], sleep=sleep)
    g.remember((10, 10))                          # the owner moved the mouse far away meanwhile
    g.check()                                     # no Stopped
    assert g.deadline == 13 and g.last_pos is None


def test_a_hand_back_makes_the_task_look_again(state, monkeypatch):
    import requests_owner
    monkeypatch.setattr(requests_owner, "screenshot", lambda rect=None, highlight=None: None)
    lp = loop.Loop("sign in", Hands(dry_run=True), brain=types.SimpleNamespace(requests=0, local_requests=0),
                   asker=lambda *a: takeover.HANDED_BACK)
    with pytest.raises(loop._HandedBack):
        lp._ask("choice", "A site wants a human check", ["I solved it"])
    calls = iter([True, True, False])
    monkeypatch.setattr(takeover, "active", lambda: next(calls))
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    with pytest.raises(loop._HandedBack):
        lp._wait_while_owner_in_control()


def test_taking_over_nudges_the_screen_once_instead_of_waiting_for_a_monitor(state, monkeypatch):
    # 2026-10-08: start() ran display.wake, which nudged every 5 s for 15 s with the monitor off, and one nudge
    # yanked a resumed task's cursor ("the mouse was moved by someone else").
    nudges, waits = [], []
    monkeypatch.setitem(sys.modules, "display", types.SimpleNamespace(
        wake=lambda *a, **k: waits.append(1), nudge=lambda *a, **k: nudges.append(1)))
    monkeypatch.setattr(takeover, "_monitor", lambda: (0, 0, 1024, 768))
    takeover.start()
    assert nudges == [1] and waits == []


def test_a_nudge_never_moves_the_mouse_back_over_someone_elses_move(monkeypatch):
    import display
    monkeypatch.setattr(display.time, "sleep", lambda s: None)

    class Mouse:
        def __init__(self):
            self._pos, self.sets = (100, 100), 0

        @property
        def position(self):
            return self._pos

        @position.setter
        def position(self, v):
            self.sets += 1
            self._pos = (500, 300) if self.sets == 1 else v   # a task moved it right after the nudge's +1

    m = Mouse()
    display.nudge(m)
    assert m.position == (500, 300)

    class Still:
        position = (100, 100)
    s = Still()
    display.nudge(s)
    assert s.position == (100, 100)          # nobody else moved it: back where it started


def test_view_only_frames_need_no_control_and_real_ones_still_do(state, monkeypatch):
    # 2026-10-08: DeskAI's "View only" box. Watching must never take control (it stopped a running task).
    import socket
    import threading
    import urllib.error
    import urllib.request
    import takeover_serve
    monkeypatch.setattr(takeover, "frame", lambda *a, **k: (b"JPEG", {"width": 1024, "height": 768}))
    monkeypatch.setattr(takeover_serve, "resolve_host", lambda h: h)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    threading.Thread(target=takeover_serve.serve, args=("127.0.0.1", port),
                     kwargs={"emit": lambda *a, **k: None, "fixed_token": "test-key-123"}, daemon=True).start()

    def get(path):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Token": "test-key-123"})
        for _ in range(50):
            try:
                with urllib.request.urlopen(req, timeout=2) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
            except OSError:
                time.sleep(0.05)              # the server is still starting
        raise AssertionError("server never answered")

    assert not takeover.active()
    assert get("/frame")[0] == 409            # real Take over: control first
    assert get("/frame?view=1") == (200, b"JPEG")
    assert not takeover.active()              # watching took nothing over
