"""The screen has to be on and at its real size before the tool looks.

The Mini's monitor sleeps when idle, and many monitors disconnect while asleep, so Windows falls back to
1024x768 (seen 2026-10-05). Before a task: ask Windows to keep the display on (the signal video players use),
nudge the mouse to wake it, and wait until the resolution is back and steady. After the task, let go: the
monitor sleeps as usual.
"""
import ctypes
import time
from typing import Tuple

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
MIN_WIDTH = 1280                 # anything smaller is Windows' no-monitor fallback (1024x768)


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # real pixels, not scaled ones
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def size() -> Tuple[int, int]:
    _dpi_aware()
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def keep_on() -> None:
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)


def let_sleep() -> None:
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def nudge(mouse=None) -> None:
    """A one-pixel move and back: input wakes a sleeping monitor; it ends where it started. It moves back only if
    nobody else moved the mouse in between: 2026-10-08, a nudge put back a position read before a task's move and
    yanked the task's cursor (it stopped: "the mouse was moved by someone else")."""
    if mouse is None:
        from pynput.mouse import Controller
        mouse = Controller()
    x, y = mouse.position
    mouse.position = (x + 1, y)
    time.sleep(0.05)
    if tuple(mouse.position) == (x + 1, y):
        mouse.position = (x, y)


def wake(timeout: float = 25.0, min_width: int = MIN_WIDTH) -> Tuple[int, int]:
    """Keeps the display on, wakes it, and waits until the screen is at least min_width wide and steady over
    two readings. Raises TimeoutError if it doesn't come back (the monitor may be off or unplugged)."""
    keep_on()
    end = time.time() + timeout
    nudged_at = 0.0
    prev = None
    while time.time() < end:
        if time.time() - nudged_at > 5:
            nudge()
            nudged_at = time.time()
        now = size()
        if now[0] >= min_width and now == prev:
            return now
        prev = now
        time.sleep(1.0)
    raise TimeoutError(f"the screen stayed at {size()[0]}x{size()[1]}: is the monitor on and plugged in?")


def screen_for_run(allow_small: bool = False, timeout: float = 25.0) -> Tuple[Tuple[int, int], bool]:
    """(size, small) for a run: the real monitor, woken, whenever there is one. Only when it doesn't come back and
    the owner allowed it (`run --allow-small-screen`), Windows' no-monitor screen (1024x768) as it is, provided it
    holds steady. small=True then. The run's guard stops it if the size changes after this (a monitor switched on
    mid-task moves everything)."""
    try:
        return wake(timeout=timeout), False
    except TimeoutError:
        if not allow_small:
            raise
    first = size()
    time.sleep(1.0)
    later = size()
    if later != first:
        raise TimeoutError(f"the screen keeps changing size ({first[0]}x{first[1]} -> {later[0]}x{later[1]})")
    return first, True
