"""Take over: the owner uses the computer from their phone (the owner's ask: "the screen comes over to me").

The phone shows a live picture of the screen (`frame`); the owner's taps, drags, scrolls, typing and keys are
replayed here with the real mouse and keyboard (`apply`). While they're in control, a computer task doesn't act:
its guard waits (hands.Guard `paused`), and a task waiting on a request gets HANDED_BACK as the answer when
they hand back, so it looks at the screen again instead of acting on an old picture. Used for CAPTCHAs (the tool
never solves them) and anything unusual.

The "in control" flag is a small file, so the agent's server (which serves the phone) and a running task (another
process) see the same thing. It lets go by itself after IDLE_RELEASE with no picture asked for and no input.
"""
import datetime
import io
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ACU_DATA_DIR") or (ROOT / "data"))
STATE = DATA / "takeover.json"
IDLE_RELEASE = 10 * 60                 # nobody looked or tapped for 10 minutes: control goes back to the tool
TOUCH_EVERY = 15                       # seconds between "still here" writes
HANDED_BACK = "__handed_back__"        # the answer a waiting request gets when the owner hands back
MAX_TEXT = 500
SCROLL_MAX = 15                        # notches per scroll
KEYS = {"enter", "backspace", "tab", "esc", "delete", "space", "up", "down", "left", "right", "home", "end",
        "page_up", "page_down"}
COMBOS = {"ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+l", "ctrl+f", "ctrl+t", "ctrl+w",
          "ctrl+r", "ctrl+shift+t", "alt+tab", "alt+left", "alt+right", "shift+tab"}


class TakeoverError(Exception):
    pass


def _now() -> float:
    return time.time()


def _read() -> Dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(state: Dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE)


def status() -> Dict[str, Any]:
    """Whether the owner is in control (and since when, and for which request). Lets go if they've been idle."""
    s = _read()
    if s.get("active") and _now() - float(s.get("last_seen", 0)) > IDLE_RELEASE:
        release()
        return {"active": False, "released": "idle"}
    return {"active": bool(s.get("active")), "since": s.get("since"), "request_id": s.get("request_id")}


def active() -> bool:
    try:
        return status()["active"]
    except Exception:
        return False


def start(request_id: Optional[str] = None) -> Dict[str, Any]:
    """The owner takes control (a task pauses at its next action). Wakes the screen."""
    now = _now()
    _write({"active": True, "since": datetime.datetime.now().isoformat(timespec="seconds"), "last_seen": now,
            "request_id": request_id})
    try:
        # One nudge, not display.wake: wake nudges every 5 s for up to 15 s until the screen is full size, which with
        # the monitor switched off (1024x768) is never. Those nudges ran in the agent's process while a task
        # resumed, and one snapped the task's cursor back mid-move: "the mouse was moved by someone else"
        # (2026-10-08, the first live run).
        import display
        _keep_awake()                        # one-off idle reset (keep_on would hold the display on for good)
        display.nudge()
    except Exception:
        pass                                 # a picture of a dark screen still tells the owner something
    return {**status(), "screen": list(_monitor()[2:])}


def touch() -> None:
    s = _read()
    if s.get("active") and _now() - float(s.get("last_seen", 0)) >= TOUCH_EVERY:
        s["last_seen"] = _now()
        _write(s)


def release() -> Optional[str]:
    """Control goes back to the tool. Returns the request it was taken over for, if any."""
    s = _read()
    try:
        STATE.unlink()
    except OSError:
        pass
    return s.get("request_id")


# ---------------------------------------------------------------------------------------------- the screen
def _monitor() -> Tuple[int, int, int, int]:
    """The main screen: left, top, width, height (real pixels)."""
    import display
    display._dpi_aware()
    import mss
    with mss.MSS() as s:
        m = s.monitors[1]
    return m["left"], m["top"], m["width"], m["height"]


def _keep_awake() -> None:
    try:
        import ctypes
        import display
        ctypes.windll.kernel32.SetThreadExecutionState(display.ES_DISPLAY_REQUIRED | display.ES_SYSTEM_REQUIRED)
    except Exception:
        pass


def activity_now() -> Dict[str, Any]:
    """What a computer-use run is doing (activity.py), for the banner on Take over's screen. Read-only."""
    import activity
    return activity.current()


def frame(max_width: int = 1280, quality: int = 60, view_only: bool = False) -> Tuple[bytes, Dict[str, Any]]:
    """A JPEG of the main screen with the mouse pointer ringed, at most max_width wide.

    view_only (DeskAI's "View only"): just the picture. It must not count as the owner being in control: until
    2026-10-10 every picture refreshed last_seen, so watching in View only kept a Take over left open the evening
    before alive for 17 hours, and a computer task waited all that time for the owner to "hand back"."""
    import display
    display._dpi_aware()
    import mss
    from PIL import Image, ImageDraw
    _keep_awake()
    if not view_only:
        touch()
    with mss.MSS() as s:
        m = s.monitors[1]
        shot = s.grab(m)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    try:
        from pynput.mouse import Controller
        x, y = Controller().position
        x, y = x - m["left"], y - m["top"]
        d = ImageDraw.Draw(img)
        for r, w, c in ((14, 4, (0, 0, 0)), (12, 3, (255, 214, 10))):
            d.ellipse((x - r, y - r, x + r, y + r), outline=c, width=w)
    except Exception:
        pass
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), {"width": m["width"], "height": m["height"], "image_width": img.width,
                            "image_height": img.height}


# ---------------------------------------------------------------------------------------------- the owner's input
def _controllers():
    from pynput.keyboard import Controller as Keyboard
    from pynput.mouse import Controller as Mouse
    return Mouse(), Keyboard()


def _point(ev: Dict[str, Any], xk: str = "x", yk: str = "y") -> Tuple[int, int]:
    """A spot on the picture (0..1 across and down) as a screen position."""
    try:
        fx, fy = float(ev[xk]), float(ev[yk])
    except (KeyError, TypeError, ValueError):
        raise TakeoverError(f"{xk} and {yk} are needed (0 to 1)")
    if not (0 <= fx <= 1 and 0 <= fy <= 1):
        raise TakeoverError(f"{xk} and {yk} must be between 0 and 1")
    left, top, width, height = _monitor()
    return left + min(width - 1, round(fx * width)), top + min(height - 1, round(fy * height))


def _key(name: str):
    from pynput.keyboard import Key
    return {"esc": Key.esc, "page_up": Key.page_up, "page_down": Key.page_down}.get(name) or getattr(Key, name)


def apply(ev: Dict[str, Any]) -> str:
    """One input from the owner's phone, done with the real mouse or keyboard. Only while they're in control."""
    if not active():
        raise TakeoverError("take over first")
    touch()
    kind = (ev or {}).get("type")
    mouse, keyboard = _controllers()
    from pynput.mouse import Button
    if kind in ("tap", "double_tap", "long_press"):
        mouse.position = _point(ev)
        time.sleep(0.05)
        if kind == "long_press":
            mouse.click(Button.right, 1)
        else:
            mouse.click(Button.left, 2 if kind == "double_tap" else 1)
        return f"{kind} at {mouse.position}"
    if kind == "drag":
        (x1, y1), (x2, y2) = _point(ev, "x", "y"), _point(ev, "x2", "y2")
        mouse.position = (x1, y1)
        mouse.press(Button.left)
        steps = 12
        for i in range(1, steps + 1):
            mouse.position = (round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps))
            time.sleep(0.02)
        mouse.release(Button.left)
        return f"dragged to {(x2, y2)}"
    if kind == "scroll":
        try:
            dy = int(ev.get("dy", 0))
        except (TypeError, ValueError):
            raise TakeoverError("dy must be a whole number")
        dy = max(-SCROLL_MAX, min(SCROLL_MAX, dy))
        if "x" in ev:
            mouse.position = _point(ev)
        mouse.scroll(0, dy)
        return f"scrolled {dy:+d}"
    if kind == "text":
        text = str(ev.get("text") or "")
        if not text or len(text) > MAX_TEXT:
            raise TakeoverError(f"text: 1 to {MAX_TEXT} characters")
        from hands import caps_lock_off
        caps_lock_off(keyboard)                   # the phone sends exactly what was typed: no capitals added here
        keyboard.type(text)
        return f"typed {len(text)} characters"
    if kind == "key":
        name = str(ev.get("key") or "").lower().replace(" ", "")
        if name in KEYS:
            k = _key(name)
            keyboard.press(k)
            keyboard.release(k)
            return f"pressed {name}"
        if name in COMBOS:
            *mods, last = name.split("+")
            held = [_key(m) for m in mods]
            for k in held:
                keyboard.press(k)
            try:
                k = _key(last) if last in KEYS or last in ("tab", "left", "right") else last
                keyboard.press(k)
                keyboard.release(k)
            finally:
                for k in reversed(held):
                    keyboard.release(k)
            return f"pressed {name}"
        raise TakeoverError(f"unknown key {name!r}")
    raise TakeoverError(f"unknown input {kind!r}")
