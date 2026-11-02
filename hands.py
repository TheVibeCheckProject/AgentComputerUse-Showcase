"""The real mouse and keyboard: the only way AgentComputerUse acts.

Every action first passes the guard:
- the kill switch (Ctrl+Alt+End) stops everything;
- if the mouse isn't where the tool last left it, the owner has taken over, so it stops
  instead of clicking or scrolling under their hand;
- a deadline for the whole run.
Dry run: actions are recorded and described, nothing is moved or typed.
Typing goes through pynput, which types any character (PyAutoGUI only types plain English ones).
"""
import math
import random
import threading
import time
from typing import Callable, List, Optional, Tuple

KILL_HOTKEY = "<ctrl>+<alt>+<end>"


def bezier_trajectory(start: Tuple[int, int], end: Tuple[int, int],
                      deviation_scale: float = 0.20, steps: Optional[int] = None) -> List[Tuple[int, int]]:
    """Generates an organic, curved Cubic Bézier path between two points.

    Mimics natural human hand motion: variable acceleration, subtle curvature deviation,
    and micro-jitter during deceleration near the target.
    """
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 3.0:
        return [(x1, y1)]

    # Orthogonal unit vector for lateral curve deviation
    nx, ny = -dy / dist, dx / dist
    # Curve displacement scaled by distance, capped to avoid unnatural wide swings
    dev = random.uniform(-1.0, 1.0) * min(90.0, dist * deviation_scale)

    # Two intermediate control points
    p0 = (float(x0), float(y0))
    p1 = (x0 + dx * 0.28 + dev * nx * random.uniform(0.7, 1.2),
          y0 + dy * 0.28 + dev * ny * random.uniform(0.7, 1.2))
    p2 = (x0 + dx * 0.72 + dev * nx * random.uniform(0.3, 0.7),
          y0 + dy * 0.72 + dev * ny * random.uniform(0.3, 0.7))
    p3 = (float(x1), float(y1))

    num_steps = steps or max(10, min(50, int(dist / 16.0)))
    points: List[Tuple[int, int]] = []

    for i in range(1, num_steps + 1):
        t = i / num_steps
        # Cubic Bézier polynomial: B(t) = (1-t)^3*P0 + 3(1-t)^2*t*P1 + 3(1-t)*t^2*P2 + t^3*P3
        u = 1.0 - t
        px = u**3 * p0[0] + 3.0 * u**2 * t * p1[0] + 3.0 * u * t**2 * p2[0] + t**3 * p3[0]
        py = u**3 * p0[1] + 3.0 * u**2 * t * p1[1] + 3.0 * u * t**2 * p2[1] + t**3 * p3[1]

        # Subtle micro-jitter (sub-pixel drift) in the final 15% deceleration phase
        if 0.85 < t < 1.0:
            jitter = (1.0 - t) / 0.15 * random.uniform(-0.75, 0.75)
            px += jitter
            py += jitter

        points.append((round(px), round(py)))

    # Guarantee landing precisely on the target coordinate
    if not points or points[-1] != (x1, y1):
        points.append((x1, y1))
    return points


def screen_bounds() -> Optional[Tuple[int, int, int, int]]:
    """All screens together (Windows' virtual screen): left, top, right, bottom, the last two inclusive."""
    try:
        import ctypes
        u = ctypes.windll.user32
        left, top, w, h = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))   # SM_X/YVIRTUALSCREEN, SM_CX/CY...
        return (left, top, left + w - 1, top + h - 1) if w > 0 and h > 0 else None
    except Exception:
        return None


def _clamp(p: Tuple[int, int], bounds: Optional[Tuple[int, int, int, int]]) -> Tuple[int, int]:
    if not bounds:
        return p
    left, top, right, bottom = bounds
    return min(max(p[0], left), right), min(max(p[1], top), bottom)


def fitts_duration(distance: float) -> float:
    """Computes a realistic human movement time based on Fitts's Law."""
    if distance < 10.0:
        return random.uniform(0.12, 0.18)
    # Logarithmic scaling with distance: fast over short hops, steady human sweep over long hops
    d = 0.14 + 0.09 * math.log2(1.0 + distance / 35.0) + random.uniform(-0.02, 0.03)
    return max(0.16, min(0.58, d))


def caps_lock_off(keyboard, is_on: Callable[[], bool] = None) -> bool:
    """Turns Caps Lock off if it's on; True if it did. With it on, every letter typed came out in capitals and the
    read-back never matched, so the address was typed three times over (the Mini, 2026-10-06)."""
    try:
        if is_on is None:
            import ctypes
            is_on = lambda: bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
        if not is_on():
            return False
        from pynput.keyboard import Key
        keyboard.press(Key.caps_lock)
        keyboard.release(Key.caps_lock)
        time.sleep(0.05)
        return True
    except Exception:
        return False


class Stopped(Exception):
    """The run has to stop: kill switch, the owner moved the mouse, or the time ran out."""


class ScreenChanged(Exception):
    """The screen changed size (a monitor came on or went off): every position known is wrong. Not the end of the
    run: the loop looks again at the new size and carries on (2026-10-08, the owner: check the monitor while the
    run works, not before it)."""

    def __init__(self, before: Tuple[int, int], now: Tuple[int, int]):
        super().__init__(f"the screen changed size ({before[0]}x{before[1]} -> {now[0]}x{now[1]})")
        self.before, self.now = before, now


class Guard:
    def __init__(self, seconds: float = 300.0, tolerance: int = 3, mouse=None, clock: Callable[[], float] = time.time,
                 paused: Optional[Callable[[], bool]] = None, sleep: Callable[[float], None] = time.sleep):
        self.paused = paused                # the owner is in control (takeover.active): wait instead of acting
        self.sleep = sleep
        self.deadline = clock() + seconds
        self.tolerance = tolerance          # pixels a click may be off its target before it re-aims (not a takeover)
        self.clock = clock
        self.killed = threading.Event()
        self.last_pos: Optional[Tuple[int, int]] = None
        self._mouse = mouse
        self._listener = None
        # The screen size the run started at, and how to read it now (display.size). A monitor switched on or off
        # mid-run changes the size and moves everything on screen: every position the tool knows is then wrong,
        # so it stops instead of clicking there (2026-10-08, when runs on the 1024x768 no-monitor screen began).
        self.screen_size: Optional[Callable[[], Tuple[int, int]]] = None
        self.expected_size: Optional[Tuple[int, int]] = None
        self.human = threading.Event()      # someone used the computer's own mouse (see start_human_watch)
        self.watching_humans = False
        self.abort_reason: Optional[str] = None   # set from another thread (no monitor): the next action stops
        self.gate: Optional[threading.Event] = None   # set: actions may start (the monitor check is done)

    # ------------------------------------------------------------------ the watchdog
    # The time limit is only checked when the tool acts. A run stuck elsewhere (a slow model, a loop that never
    # acts) outlived it: 2026-10-08 one was still alive minutes after its runner gave up, and could have woken up
    # and moved the mouse later. The watchdog ends the whole process GRACE seconds after the deadline. Time the
    # owner spends in control or answering a request doesn't count (both push the deadline back afterwards).
    WATCHDOG_GRACE = 90.0
    waiting_for_owner = False

    def overdue(self) -> bool:
        if self.waiting_for_owner or self._owner_in_control():
            return False
        return self.clock() > self.deadline + self.WATCHDOG_GRACE

    def start_watchdog(self, on_overdue: Callable[[], None], poll: float = 5.0) -> threading.Thread:
        def watch():
            while True:
                self.sleep(poll)
                if self.overdue():
                    on_overdue()
                    return
        t = threading.Thread(target=watch, name="RunWatchdog", daemon=True)
        t.start()
        return t

    def lock_screen_size(self, size: Tuple[int, int], read: Callable[[], Tuple[int, int]]) -> None:
        self.expected_size, self.screen_size = tuple(size), read

    def _screen_changed(self) -> Optional[Tuple[int, int]]:
        if self.screen_size is None or self.expected_size is None:
            return None
        try:
            now = tuple(self.screen_size())
        except Exception:
            return None
        return now if now != self.expected_size else None

    # ------------------------------------------------------------------ a person at the computer
    # Whether someone is using the computer's own mouse, from Windows itself: every mouse event says whether a device
    # made it or a program injected it (LLMHF_INJECTED). The tool's own moves (SetCursorPos) and Take over's (from
    # the phone) never count as a person. 2026-10-08, fourth live run: the cursor drifted 5,15 px by itself between
    # the tool putting it on Notepad's title bar and clicking, and the old position check took that for the owner.
    # The position check stays as the fallback when the listener can't start.
    # The owner's rule (2026-10-08): taken over means the mouse went more than 50 px, in any direction. A bump or a
    # resting mouse's jitter stays under that; a hand reaching for something goes well past it.
    TAKEOVER_PX = 50
    HUMAN_IDLE = 2.0           # seconds without real movement: the next movement starts counting afresh

    def _on_human_move(self, x: int, y: int, injected: bool = False) -> None:
        if injected:
            return
        now = self.clock()
        origin = getattr(self, "_human_origin", None)          # (x, y) where this movement began, last event time
        if origin is None or now - origin[2] > self.HUMAN_IDLE:
            self._human_origin = (x, y, now)
            return
        self._human_origin = (origin[0], origin[1], now)       # a slow drag keeps adding up
        if math.hypot(x - origin[0], y - origin[1]) > self.TAKEOVER_PX:
            self.human.set()

    def _on_human_click(self, x: int, y: int, button=None, pressed: bool = True, injected: bool = False) -> None:
        if not injected and pressed:
            self.human.set()

    def _on_human_scroll(self, x: int, y: int, dx: int = 0, dy: int = 0, injected: bool = False) -> None:
        if not injected:
            self.human.set()

    def start_human_watch(self) -> bool:
        """Listens to the mouse at the Windows level. True when it's running (then cursor drift alone never stops a
        run); False when it couldn't start (the position check is used instead)."""
        try:
            from pynput import mouse
            self._mouse_listener = mouse.Listener(on_move=self._on_human_move, on_click=self._on_human_click,
                                                  on_scroll=self._on_human_scroll)
            self._mouse_listener.daemon = True
            self._mouse_listener.start()
            self.watching_humans = True
        except Exception:
            self.watching_humans = False
        return self.watching_humans

    def start_kill_switch(self) -> None:
        from pynput import keyboard
        self._listener = keyboard.GlobalHotKeys({KILL_HOTKEY: self.killed.set})
        self._listener.daemon = True
        self._listener.start()
        self.start_human_watch()

    def stop_kill_switch(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        listener = getattr(self, "_mouse_listener", None)
        if listener is not None:
            listener.stop()
            self._mouse_listener = None
        self.watching_humans = False

    def remember(self, pos: Tuple[int, int]) -> None:
        self.last_pos = (int(pos[0]), int(pos[1]))

    def _owner_in_control(self) -> bool:
        try:
            return bool(self.paused and self.paused())
        except Exception:
            return False

    def check(self) -> None:
        if self._owner_in_control():
            # Take over: the owner is using the computer from their phone. Wait (not counted against the time
            # limit), then forget where the mouse was: they moved it, and that isn't a reason to stop.
            started = self.clock()
            while self._owner_in_control() and not self.killed.is_set():
                self.sleep(0.5)
            self.deadline += self.clock() - started
            self.last_pos = None
            self.human.clear()              # whatever they did while in control isn't a reason to stop
        if self.killed.is_set():
            raise Stopped("the kill switch (Ctrl+Alt+End) was pressed")
        if self.gate is not None and not self.gate.is_set():
            started = self.clock()
            self.gate.wait(30)                       # the monitor check (15 s at most) runs alongside the thinking
            self.deadline += self.clock() - started
        if self.abort_reason:
            raise Stopped(self.abort_reason)
        changed = self._screen_changed()
        if changed:
            # Every position the tool knows moved: never act on them. The size it changed to is the new normal; the
            # loop looks again (a monitor that woke up mid-run, or one switched on or off).
            before = self.expected_size
            self.expected_size = tuple(changed)
            self.last_pos = None
            raise ScreenChanged(before, tuple(changed))
        if self.clock() > self.deadline:
            raise Stopped("the time limit for this run was reached")
        if self.human.is_set():
            self.sleep(0.4)                 # Take over's flag may land a moment after the owner's first input
            if self._owner_in_control():
                return self.check()
            raise Stopped("someone used the computer's own mouse: the owner has taken over")
        if self.watching_humans:
            # Windows reports real input above, so a cursor that moved without any is drift (Windows or a program),
            # not a person: carry on from where it is now.
            if self._mouse is not None and self.last_pos is not None:
                self.remember(self._mouse.position)
            return
        if self.last_pos is not None and self._mouse is not None:
            x, y = self._mouse.position
            if math.hypot(x - self.last_pos[0], y - self.last_pos[1]) > self.TAKEOVER_PX:
                # Opening Take over in DeskAI takes control (there's no watch-only mode) and can move the mouse a
                # moment before its "in control" flag is seen here. Look again: if the owner is now in control,
                # wait for them to hand back instead of ending the run (2026-10-08, third live run, the owner
                # watching from Take over).
                self.sleep(0.4)
                if self._owner_in_control():
                    return self.check()
                # Both positions in the message: the step log then shows what really happened.
                raise Stopped(f"the mouse was moved by someone else: the owner has taken over (the tool left it at "
                              f"{self.last_pos[0]},{self.last_pos[1]}; it was at {x},{y})")


class Hands:
    """Mouse and keyboard with the guard in front of every action."""

    def __init__(self, guard: Optional[Guard] = None, dry_run: bool = False, typing_delay=(0.035, 0.09)):
        self.dry_run = dry_run
        self.typing_delay = typing_delay
        self.log: List[str] = []
        if dry_run:
            self.mouse = self.keyboard = None
        else:
            from pynput.keyboard import Controller as Keyboard
            from pynput.mouse import Controller as Mouse
            self.mouse, self.keyboard = Mouse(), Keyboard()
        self.screen_bounds: Optional[Callable[[], Optional[Tuple[int, int, int, int]]]] = (
            None if dry_run else screen_bounds)
        self.guard = guard or Guard(mouse=self.mouse)
        if self.guard._mouse is None:
            self.guard._mouse = self.mouse

    # ------------------------------------------------------------------ mouse
    def _bounds(self) -> Optional[Tuple[int, int, int, int]]:
        try:
            return self.screen_bounds() if self.screen_bounds else None
        except Exception:
            return None

    def move(self, x: int, y: int, seconds: Optional[float] = None) -> None:
        self.guard.check()
        self.log.append(f"move to {x},{y}")
        if self.dry_run:
            return
        sx, sy = self.mouse.position
        dist = math.hypot(x - sx, y - sy)
        if dist < 2.5:
            self.mouse.position = (x, y)
            self.guard.remember((x, y))
            return

        duration = seconds if seconds is not None else fitts_duration(dist)
        # Every point stays on the screen: a curve near an edge bulges past it (up to 90 px), Windows then pulls the
        # cursor back by itself, and the guard reads that as someone else's hand (suspected 2026-10-08, third live
        # run: 1024x768 screen, the Start button on its bottom edge).
        bounds = self._bounds()
        trajectory = [_clamp(p, bounds) for p in bezier_trajectory((sx, sy), (x, y))]
        x, y = _clamp((x, y), bounds)
        interval = max(0.005, duration / len(trajectory))

        # Each step remembers where the mouse really landed, then the check after the pause compares against it:
        # a hand on the mouse during the pause is caught mid-move. (2026-10-08: a "driving" flag skipped this
        # check during moves and the owner's grab was dragged along to the target.)
        for px, py in trajectory:
            self.mouse.position = (px, py)
            self.guard.remember(self.mouse.position)
            time.sleep(interval)
            self.guard.check()
        self.mouse.position = (x, y)
        self.guard.remember(self.mouse.position)


    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.move(x, y)
        self.guard.check()
        self.log.append(f"{'double-' if double else ''}{button} click at {x},{y}")
        if self.dry_run:
            return
        from pynput.mouse import Button
        b = {"left": Button.left, "right": Button.right, "middle": Button.middle}[button]

        # Natural micro-pause before physical click
        time.sleep(random.uniform(0.02, 0.05))
        # The click goes exactly where it was aimed: if the cursor drifted after the move (2026-10-08, 5,15 px on a
        # title bar), put it back first, or the press lands on whatever is next to the target.
        tx, ty = _clamp((x, y), self._bounds())
        cx, cy = self.mouse.position
        if abs(cx - tx) > self.guard.tolerance or abs(cy - ty) > self.guard.tolerance:
            self.mouse.position = (tx, ty)
            self.guard.remember(self.mouse.position)
            self.guard.check()

        if double:
            self.mouse.press(b)
            time.sleep(random.uniform(0.045, 0.085))
            self.mouse.release(b)
            time.sleep(random.uniform(0.065, 0.110))
            self.mouse.press(b)
            time.sleep(random.uniform(0.045, 0.085))
            self.mouse.release(b)
        else:
            self.mouse.press(b)
            # Physical dwell time (holding down mouse button)
            dwell = random.uniform(0.065, 0.135)
            time.sleep(dwell)
            self.mouse.release(b)

        # Micro-pause after release
        time.sleep(random.uniform(0.03, 0.07))
        self.guard.remember(self.mouse.position)

    def scroll(self, x: int, y: int, notches: int) -> None:
        """Turn the real wheel over a point: negative notches scroll down."""
        self.move(x, y, seconds=0.2)
        self.guard.check()
        self.log.append(f"scroll {notches:+d} at {x},{y}")
        if self.dry_run:
            return
        step = -1 if notches < 0 else 1
        for _ in range(abs(notches)):
            self.guard.check()
            self.mouse.scroll(0, step)
            time.sleep(random.uniform(0.03, 0.07))
        self.guard.remember(self.mouse.position)

    # ------------------------------------------------------------------ keyboard
    def type_text(self, text: str) -> None:
        """Real keystrokes at a human pace. Callers check the right field has the keyboard first."""
        self.guard.check()
        self.log.append(f"type {len(text)} characters")
        if self.dry_run:
            return
        if caps_lock_off(self.keyboard):
            self.log.append("turned Caps Lock off")
        # One Enter per line break: pynput types "\r" and "\n" each as Enter, so a model's "\r\n" made blank lines
        # between every line (2026-10-11 batch 8: a grocery list saved with doubled lines after three retypes)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for ch in text:
            self.guard.check()
            self.keyboard.type(ch)
            time.sleep(random.uniform(*self.typing_delay))

    def press(self, combo: str) -> None:
        """A key or a combination: 'enter', 'ctrl+l', 'alt+f4', 'win'."""
        from pynput.keyboard import Key
        self.guard.check()
        self.log.append(f"press {combo}")
        if self.dry_run:
            return
        keys = []
        for part in combo.lower().split("+"):
            part = part.strip()
            name = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd", "windows": "cmd",
                    "enter": "enter", "return": "enter", "esc": "esc", "escape": "esc", "tab": "tab",
                    "backspace": "backspace", "delete": "delete", "del": "delete", "space": "space",
                    "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end",
                    "pageup": "page_up", "pagedown": "page_down"}.get(part)
            if name:
                keys.append(getattr(Key, name))
            elif len(part) >= 2 and part[0] == "f" and part[1:].isdigit():
                keys.append(getattr(Key, part))
            elif len(part) == 1:
                keys.append(part)
            else:
                raise ValueError(f"unknown key: {part!r}")
        for k in keys:
            self.keyboard.press(k)
            time.sleep(0.03)
        for k in reversed(keys):
            self.keyboard.release(k)
            time.sleep(0.02)
