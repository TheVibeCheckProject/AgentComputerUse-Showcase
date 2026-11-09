"""What a computer-use run is doing right now, for the banner on Take over's screen in DeskAI (the owner's ask,
2026-10-08: "have the task name and what the agent is doing show up in the take over view").

One small JSON file (data/activity.json): the goal, the step, what it's doing in plain words, and when it finished.
`run` writes it as it goes; Take over (the agent's server, or takeover-serve on the laptop) reads it with current().
Reading it never touches the mouse or the run.

Since 2026-10-10 (the owner: "I want to see in plain terms what the bot is actually doing, like if anyone else was
controlling it they would understand") it also keeps a running log of plain sentences (say(): "Opening Notepad",
"Clicking 'Start'", "Typing 'Why did the golfer...'", "Pressing Enter"), shown on the View only screen, and sent with
each progress event so the agent saves the same log on the task card. Typed text is shown, but never a secret: it's
hidden when the field is named like a password, PIN or code, or the text looks like one (secret_like()).
"""
import datetime
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ACU_DATA_DIR") or (ROOT / "data"))
PATH = DATA / "activity.json"
SHOW_FINISHED = 120          # seconds a finished run stays on the banner
LOG_KEEP = 60                # plain-language lines kept for the View only screen
STALE = 600                  # a "running" file this old, or whose process is gone, is a run that died

WORDS = {"launch_app": "opening an app", "type": "typing", "click": "clicking", "press": "pressing keys",
         "scroll": "scrolling", "open_url": "opening a web page", "new_page": "opening a page of its own",
         "switch_window": "switching windows", "wait": "waiting", "done": "finishing up",
         "needs_approval": "asking you to approve", "ask_owner": "asking you", "owner_types": "asking you to type"}


SECRET_FIELD = re.compile(r"pass(word|code|phrase)?|\bpin\b|\bcode\b|otp|one.time|verification|security|"
                          r"\bssn\b|social security|card number|\bcvv\b|\bcvc\b|secret|token", re.IGNORECASE)
FINAL_WORDS = {"done": "Done", "needs_owner": "Needs you", "needs_approval": "Needs your approval",
               "stopped": "Stopped", "failed": "Failed", "out_of_steps": "Ran out of steps"}
FINAL_LEVEL = {"done": "ok", "needs_owner": "warn", "needs_approval": "warn"}       # anything else: error


def secret_like(text: str) -> bool:
    """One word (no spaces) mixing letters with digits or symbols, like a password or a code; or only digits,
    4 or more (a PIN, a one-time code). Sentences, names, plain words and web addresses are not."""
    t = (text or "").strip()
    if not t or " " in t:
        return False
    if t.isdigit():
        return len(t) >= 4
    if re.fullmatch(r"(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?", t) and not re.search(r"\d", t.split("/")[0]):
        return False                                          # a web address or file name, like notepad.exe
    if re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", t):
        return False                                          # an email address: personal, not a secret
    if re.fullmatch(r"[\w() -]+\.(txt|md|docx?|xlsx?|pptx?|pdf|csv|json|py|js|html?|png|jpe?g|zip)", t, re.IGNORECASE):
        return False                                          # a file name (2026-10-10: "Story1.txt" was hidden)
    if re.match(r"([A-Za-z]:\\|\\\\|/|~[\\/])", t):
        return False                                          # a path (C:\Users\...\Story1.txt)
    has_alpha = any(c.isalpha() for c in t)
    has_other = any(c.isdigit() or not c.isalnum() for c in t)
    return has_alpha and has_other and len(t) >= 6


def _keys(keys: str) -> str:
    return "+".join(k.strip().capitalize() if len(k.strip()) > 1 else k.strip().upper()
                    for k in str(keys).split("+"))


def say(fields: dict[str, Any]) -> tuple[str, str] | None:
    """A progress event as one plain sentence and its level ("info", "warn", or "retask" for a retask's own lines:
    saved as one, repeating one, each repeated step; the app colors them, the owner's ask 2026-10-11), or None when
    it says nothing new."""
    plain = _say(fields)
    if plain and fields.get("retask") and plain[1] == "info":
        return plain[0], "retask"
    return plain


def _say(fields: dict[str, Any]) -> tuple[str, str] | None:
    if isinstance(fields.get("plan"), list):
        return "Plan: " + " \u2192 ".join(str(s) for s in fields["plan"][:6]), "info"
    msg = fields.get("message")
    if msg:
        msg = str(msg)
        if msg == "circuit breaker tripped":
            return f"Going in circles, trying something else ({fields.get('warning') or 'same thing again'})", "warn"
        if msg == "answer refused":
            return f"Its answer didn't hold up, so it keeps going ({fields.get('why') or 'not on screen'})", "warn"
        if msg.startswith("that didn't work:"):
            return "That didn't work: " + msg.split(":", 1)[1].strip(), "warn"
        if msg.startswith("retyping:"):
            return "Typed text didn't come out right, so it's clearing it and typing again (" + \
                msg.split(":", 1)[1].strip() + ")", "warn"
        if msg.startswith("repeating an earlier run stopped"):
            return msg[:1].upper() + msg[1:], "warn"
        if msg.startswith("stuck"):
            return "Stuck: " + msg.split(":", 1)[-1].strip(), "warn"
        return msg[:1].upper() + msg[1:], "info"
    a = fields.get("action")
    if not a:
        return None
    target = " ".join(str(fields.get("target") or "").split())[:60]
    if a == "launch_app":
        text = f"Opening {fields.get('app') or 'an app'}"
    elif a == "click":
        text = f"Clicking \u201c{target}\u201d" if target else "Clicking"
    elif a == "type":
        typed = " ".join(str(fields.get("text") or "").split())
        if SECRET_FIELD.search(target) or secret_like(typed):
            shown = "something private (hidden)"
        elif typed:
            cut = typed if len(typed) <= 60 else typed[:60].rsplit(" ", 1)[0] + "\u2026"     # at a word boundary
            shown = "\u201c" + cut + "\u201d"
        else:
            shown = "text"
        text = f"Typing {shown}" + (f" into \u201c{target}\u201d" if target else "")
    elif a == "press":
        text = f"Pressing {_keys(fields['keys'])}" if fields.get("keys") else "Pressing keys"
    elif a == "scroll":
        text = f"Scrolling {fields.get('direction') or 'down'}"
    elif a == "open_url":
        url = str(fields.get("url") or "")
        text = f"Opening {urlparse(url).netloc or url or 'a web page'}"
    elif a == "save_as":
        text = f"Saving as {fields.get('path') or 'a file'}"
    elif a == "open_file":
        text = f"Opening the file {fields.get('path') or ''}".rstrip()
    elif a == "new_page":
        text = "Opening a new page of its own"
    elif a == "switch_window":
        text = f"Switching to \u201c{fields.get('window')}\u201d" if fields.get("window") else "Switching windows"
    elif a == "wait":
        text = "Waiting for the screen to settle"
    elif a == "done":
        text = "Checking the task is really done"
    elif a == "needs_approval":
        text = "Asking you to approve before it goes on"
    elif a == "ask_owner":
        text = "Asking you a question"
    elif a == "owner_types":
        text = "Asking you to type something (it never sees what you type)"
    else:
        text = a.replace("_", " ").capitalize()
    if fields.get("problem"):
        return f"{text}: not allowed ({str(fields['problem'])[:120]})", "warn"
    return text, "info"


# What kind of step a line is, so the app can color "What it did" by it (the owner, 2026-10-11: "basically all
# white text"): opening something, typing, clicking or keys, saving, or the tool's own housekeeping (plans,
# checks, waiting, minimizing windows). "" for anything else (warnings and retask lines have their own level).
KIND_OF_ACTION = {"open_url": "open", "launch_app": "open", "open_file": "open", "new_page": "open",
                  "switch_window": "open", "type": "type", "owner_types": "type", "click": "click", "press": "click",
                  "scroll": "click", "needs_approval": "click", "save_as": "save", "wait": "plan", "done": "plan",
                  "ask_owner": "plan"}
HOUSEKEEPING = ("minimized every window", "plan:", "screen on at", "no monitor", "stuck", "looking again",
                "saved as a retask", "this task was done before")


def kind_of(fields: dict[str, Any]) -> str:
    if isinstance(fields.get("plan"), list):
        return "plan"
    a = fields.get("action")
    if a:
        return KIND_OF_ACTION.get(a, "")
    msg = str(fields.get("message") or "").lower()
    return "plan" if msg.startswith(HOUSEKEEPING) else ""


def _append(state: dict[str, Any], step: Any, text: str, level: str, kind: str = "") -> None:
    log = state.get("log") or []
    log.append({"t": datetime.datetime.now().strftime("%H:%M:%S"), "step": step, "text": text[:240], "level": level,
                "kind": kind})
    state["log"] = log[-LOG_KEEP:]


def _write(state: dict[str, Any]) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PATH)
    except OSError:
        pass                                  # the banner is a nicety: never let it break a run


def _read() -> dict[str, Any]:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def start(goal: str) -> None:
    _write({"goal": goal[:300], "status": "running", "step": None, "doing": "starting", "pid": os.getpid(),
            "started": datetime.datetime.now().isoformat(timespec="seconds"), "updated": time.time()})


def describe(fields: dict[str, Any]) -> str | None:
    """A progress event in plain words, or None when it says nothing new."""
    if isinstance(fields.get("plan"), list):
        return "planned: " + "; ".join(str(s) for s in fields["plan"][:5])
    if fields.get("message"):
        return str(fields["message"])
    a = fields.get("action")
    if not a:
        return None
    words = WORDS.get(a, a)
    expect = fields.get("expect")
    text = words + (f": {expect}" if expect else "")
    if fields.get("problem"):
        text += " (refused)"
    return text + (" (chained)" if fields.get("chained") else "")


def note(event: str, fields: dict[str, Any]) -> None:
    if event != "progress":
        return
    doing = describe(fields)
    if doing is None:
        return
    state = _read() or {"status": "running", "pid": os.getpid()}
    if fields.get("step") is not None:
        state["step"] = fields["step"]
    plain = (fields["say"], fields.get("level") or "info") if fields.get("say") else say(fields)
    if plain:
        text, level = plain
        _append(state, fields.get("step"), text, level, kind=fields.get("kind") or kind_of(fields))
        doing = text
    state.update({"doing": doing[:200], "updated": time.time()})
    _write(state)


def finish(status: str, answer: str | None = None, question: str | None = None) -> None:
    state = _read() or {}
    detail = answer or question
    words = {"done": "done", "needs_owner": "needs you", "needs_approval": "needs your approval",
             "stopped": "stopped", "failed": "failed", "out_of_steps": "ran out of steps"}.get(status, status)
    state.update({"status": status, "doing": words + (f": {detail[:180]}" if detail else ""),
                  "updated": time.time()})
    _append(state, None, FINAL_WORDS.get(status, status.capitalize()) + (f": {detail[:200]}" if detail else ""),
            FINAL_LEVEL.get(status, "error"))
    _write(state)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only API; mypy runs on Linux
        handle = k32.OpenProcess(0x1000, False, int(pid))     # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except AttributeError:
        # Not Windows (tests). os.kill(pid, 0) is safe on POSIX only: on Windows it would END the process.
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False


def current(now: float | None = None) -> dict[str, Any]:
    """For the banner: {"running": bool, "goal", "step", "doing", "status"}; {"running": False} when there's
    nothing to show (no run, a finished one older than SHOW_FINISHED, or a run whose process died)."""
    s = _read()
    if not s:
        return {"running": False}
    now = time.time() if now is None else now
    age = now - float(s.get("updated") or 0)
    if s.get("status") == "running":
        if age > STALE or not _alive(s.get("pid")):
            return {"running": False}
    elif age > SHOW_FINISHED:
        return {"running": False}
    return {"running": s.get("status") == "running", "goal": s.get("goal"), "step": s.get("step"),
            "doing": s.get("doing"), "status": s.get("status"), "log": s.get("log") or []}
