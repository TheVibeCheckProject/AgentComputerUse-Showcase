"""Phase 2: the loop. A goal in, one checked step at a time, accuracy first.

Each step: look (accessibility + OCR, one numbered list) -> one model request for ONE action -> checks ->
act with the real mouse and keyboard -> the next look confirms the step's expected result (the model reports
it in the same request, so confirming costs nothing extra).

Accuracy rules (the owner: "not worried about speed, more worried about accuracy"):
- grounded actions only: item numbers must be in the list just read; typing only into fields;
- risky clicks (submit, send, pay, buy, delete, post, install, sign up...) always stop for the owner's
  approval, whatever the model says;
- only touches what the task made (task.py); the owner's windows are look-only;
- an answer needs evidence: exact quotes that are really on screen, or it's rejected;
- the stuck ladder: look again -> its own rules (pop-ups) -> the local model's seeing hints (checked, never
  decisions) -> a Flash re-plan -> the owner.
"""
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import actions
import browser
import circuit_breaker
import goals
import popups
import screen
import task as tasks
from brain import Brain, BrainError
from chain import ChainMixin
from files import FilesMixin
from goals import (  # noqa: F401
    QUESTION_START,
    REPORTS,
    SAVE_NAME,
    _norm,
    _same_text,
    asks_for_information,
    goal_subjects,
    known_folder,
    saved_file,
    saved_file_note,
    saved_file_problem,
    unlist,
)
from hands import Hands, ScreenChanged, Stopped
from owner import OwnerMixin
from prompt import SYSTEM
from replay import RetaskMixin

# Re-exported for the code and tests that still say loop.TYPE_PASSWORD etc. (they moved to rules.py in the split)
from rules import (  # noqa: F401
    ACTIONS,
    AUTH_ALT_PATTERNS,
    CONSOLE_PROCESSES,
    EDITING_KEYS,
    LENGTH_SLACK,
    LIST_LIMIT,
    PHONE_PROMPT_PATTERNS,
    RISKY_CLICK,
    SIGNIN_URL,
    TAPPED_YES,
    TYPE_PASSWORD,
    TYPEABLE,
    _HandedBack,
    _NeedsApproval,
    _Paused,
)
from verify import VerifyMixin


@dataclass
class Result:
    status: str                        # done | needs_approval | needs_owner | failed | stopped | out_of_steps
    answer: str | None = None
    evidence: list[str] = field(default_factory=list)
    question: str | None = None
    steps: int = 0
    requests: int = 0
    local_requests: int = 0
    run: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def screen_fingerprint(view: screen.View) -> str:
    """What's on screen, in short: the address and every item's kind, name and value. Positions are left out
    so a pixel of re-layout isn't "a different screen"; a scroll that reveals new items is."""
    import hashlib
    parts = [view.window or "", view.url or ""] + [f"{i.kind}|{i.name}|{i.value or ''}" for i in view.items]
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def _minimized(w) -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsIconic(w.NativeWindowHandle))  # type: ignore[attr-defined]  # Windows-only API; mypy runs on Linux
    except Exception:
        return False


def _desktop():
    """The desktop itself, for a task with no window of its own and nothing on the screen (after minimize-all)."""
    try:
        import ctypes

        import uiautomation as auto
        hwnd = ctypes.windll.user32.FindWindowW("Progman", None)  # type: ignore[attr-defined]  # Windows-only API; mypy runs on Linux
        return auto.ControlFromHandle(hwnd) if hwnd else None
    except Exception:
        return None


class Loop(FilesMixin, VerifyMixin, OwnerMixin, ChainMixin, RetaskMixin):
    def __init__(self, goal: str, hands: Hands, brain: Brain | None = None, max_steps: int = 25,
                 emit=lambda event, **f: None, log=None, allowed=(), asker=None):
        self.goal = goal
        self.asker = asker                      # asks the owner through DeskAI and waits (requests_owner.ask)
        self.hands = hands
        self.brain = brain or Brain()
        self.max_steps = max_steps
        self.emit = emit
        self.log = log
        self.task = tasks.Task(goal, allowed=set(allowed))
        self.history: list[dict[str, Any]] = []
        self.seen_text: list[str] = []         # everything read this task: evidence must come from here
        self.window: Any | None = None          # the window the task is working in
        self.edge: Any | None = None
        self.plan: dict[str, Any] = {}
        self.trouble = 0                        # failed or unconfirmed steps in a row
        self.hints: list[str] = []
        self.owner_instructions: list[str] = []
        self.circuit_breaker = circuit_breaker.CircuitBreaker()
        self._autofilled: set = set()          # sites whose saved password was typed this run
        self.trail: list[dict[str, Any]] = []   # each step: what the screen showed, what was done (verify_task)
        self.done_refusals = 0                   # "done" refused by the double-check: twice and the owner decides
        self.launched: set = set()               # apps opened in this run (never reopened to "check")
        self.recorded: list[dict[str, Any]] = []  # the steps that worked, for a retask (replay.py)
        self.retyped = False                     # typing read back wrong at some point: not a clean run
        self.replay_state: str | None = None  # a retask: running | off_course | to_reading_point | done

    def _safe_abort_checkpoint(self, reason: str) -> dict[str, Any]:
        """Saves where the task stopped (goal, reason, page, last steps, plan) to data/runs/checkpoint_<time>.json.
        The screen is left exactly as it is: 2026-10-08, this used to press Ctrl+W on every pause, closing the
        page the owner had just been asked about (and Take over needs it open). History holds no secrets: passwords
        and codes never go into it."""
        import steplog
        ts = int(time.time())
        try:
            url = self._url()
        except Exception:
            url = None
        data = {"timestamp": ts, "goal": self.goal, "reason": reason, "url": url,
                "history": self.history[-10:], "plan": self.plan}
        try:
            steplog.RUNS.mkdir(parents=True, exist_ok=True)
            (steplog.RUNS / f"checkpoint_{ts}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                                                encoding="utf-8")
        except Exception:
            pass
        return data

    # ------------------------------------------------------------------ looking

    def _own_window(self):
        """The task's own window to work in (the last one it used, its Edge, or any it opened), or None."""
        import uiautomation as auto
        for w in (self.window, self.edge):
            try:
                if w is not None and self.task.owns_window(w) and w.BoundingRectangle.width() > 0:
                    return w
            except Exception:
                continue
        for handle in list(self.task.windows):
            try:
                w = auto.ControlFromHandle(handle)
                if w is not None and w.BoundingRectangle.width() > 0:
                    return w
            except Exception:
                continue
        return None

    def _current_window(self):
        """The window to look at: the window in front when it's the task's own (or a dialog its window opened), the
        Start menu, or anything while the task has no window yet. Once the task has a window of its own, another
        window in front (one of the owner's) is never looked at or acted in: the task's own comes back to the
        front. (2026-10-11, the owner: "a tab should not stop a run... it needs to read the Pinterest URL and know
        it's not right". A Pinterest page left in front of the task's Edge was looked at instead, every step was
        refused as "not yours", and a sign-in box on it was answered as if it were the task's pop-up.)"""
        import uiautomation as auto
        fg = screen.foreground()
        if fg is not None and _minimized(fg):
            # Windows still calls a minimized window "in front" after minimize-all, but it isn't on the screen
            # (2026-10-11 batch 5: an earlier task's "README.md - Notepad" was read as the screen, the model took
            # its README for the one asked for, and every click was refused as "not yours")
            fg = None
        if fg is not None and screen.process_name(fg.ProcessId).lower() not in CONSOLE_PROCESSES:
            own = None
            try:
                foreign = (self.task.windows and not self.task.owns_window(fg)
                           and screen.process_name(fg.ProcessId).lower() not in actions.START_PROCESSES)
            except Exception:
                foreign = False
            if foreign:
                own = self._own_window()
            if own is None:
                return fg
            try:
                actions.bring_to_front(own, self.hands)
            except Exception:
                pass
            self.emit("progress", message=f"another window was in front ({(fg.Name or '')[:50]}): "
                                          f"back to the task's own window")
            return own
        if self.window is not None and (not _minimized(self.window) or self.task.owns_window(self.window)):
            try:
                _ = self.window.BoundingRectangle      # raises if the window has closed
                return self.window
            except Exception:
                pass
        for w in auto.GetRootControl().GetChildren():
            try:
                if (w.Name or "").strip() and w.ClassName != "Shell_TrayWnd" and not _minimized(w) and \
                        screen.process_name(w.ProcessId).lower() not in CONSOLE_PROCESSES:
                    return w
            except Exception:
                continue
        return _desktop() or screen.foreground()

    def look(self) -> screen.View:
        win = self._current_window()
        view = screen.look(win)
        view.items = view.items[:LIST_LIMIT * 2]
        self.window = win
        for i in view.items:
            if i.name:
                self.seen_text.append(i.name)
            if i.value:
                self.seen_text.append(i.value)
        return view

    def _windows_text(self) -> str:
        import uiautomation as auto
        titles = []
        for w in auto.GetRootControl().GetChildren():
            try:
                t = (w.Name or "").replace("\u200b", "").strip()
                if t and w.ClassName != "Shell_TrayWnd" and t != "Program Manager" and \
                        screen.process_name(w.ProcessId).lower() not in CONSOLE_PROCESSES:
                    mine = " (yours)" if self.task.owns_window(w) else ""
                    titles.append(f"- {t[:80]}{mine}")
            except Exception:
                continue
        return "\n".join(titles[:15])

    # ------------------------------------------------------------------ asking
    def _messages(self, view: screen.View) -> list[dict[str, str]]:
        hist = "\n".join(f"{n}. {h['action']} -> {h['result']}" + (f" (expected: {h['expect']})" if h.get("expect") else "")
                         for n, h in enumerate(self.history[-8:], max(1, len(self.history) - 7)))
        plan = "\n".join(f"- {s}" for s in self.plan.get("steps", [])) or "(no plan yet)"
        user = (f"GOAL: {self.goal}\n\nPLAN:\n{plan}\n\nDONE WHEN: {self.plan.get('done_when', '')}\n\n"
                f"STEPS SO FAR:\n{hist or '(none yet)'}\n\nOPEN WINDOWS:\n{self._windows_text()}\n\n"
                f"CURRENT LIST:\n{view.text(limit=LIST_LIMIT)}")
        on_disk = goals.saved_file_note(self.goal, getattr(self, "started_at", 0.0))
        if on_disk:
            user += "\n\n" + on_disk                # 2026-10-10: saved fine, didn't notice, typed the path over the story
        if not self.plan.get("steps"):
            user += ("\n\nThis is the FIRST step: also give \"plan\" and \"done_when\". If the whole goal is obvious "
                     "(e.g. open an app, type, close it), give every action now: the first as the action, the rest in "
                     "\"then\".")
        if not self.task.windows and not self.task.tab_ids:
            user += ("\n\nNOTE: you have no window of your own yet, and the window in front is the owner's "
                     "(look-only). Start with open_url (web) or launch_app (an app).")
        if self.owner_instructions:
            user += "\n\nWHAT THE OWNER SAID (follow it):\n" + "\n".join(self.owner_instructions[-8:])
        if self.hints:
            user += "\n\nHINTS FROM A SECOND LOOK (may be wrong; the list wins):\n" + "\n".join(self.hints)
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

    def make_plan(self, view: screen.View, replan: bool = False) -> None:
        why = "The previous approach isn't working; make a better plan from what's on screen now.\n" if replan else ""
        msgs = [{"role": "system", "content": "You plan tasks on a Windows computer used through the screen, "
                 "mouse and keyboard. For web pages the task opens its OWN Edge window (an open_url step); it never uses the "
                 "owner's windows, which are look-only. Reply with JSON: {\"steps\": [short steps in "
                 "plain words], \"done_when\": \"what the screen shows when the goal is achieved\"}. Plan only "
                 "what's needed; never plan to submit, send, pay or post without the owner's approval."},
                {"role": "user", "content": f"{why}GOAL: {self.goal}\n\nOPEN WINDOWS:\n{self._windows_text()}\n\n"
                                            f"SCREEN NOW:\n{view.text(limit=60)}"}]
        self.plan = self.brain.plan(msgs)
        self.emit("progress", message="plan" if not replan else "new plan", plan=self.plan.get("steps", []))
        if self.log:
            self.log.write({"event": "plan", "replan": replan, **self.plan})

    # ------------------------------------------------------------------ checks
    def check(self, reply: dict[str, Any], view: screen.View) -> str | None:
        """What's wrong with the model's chosen action, or None. Deterministic: the model can't overrule it."""
        a = reply.get("action")
        if a not in ACTIONS:
            return f"unknown action {a!r}"
        problem = self._save_guard(reply, view, a)
        if problem:
            return problem
        if a in ("click", "type", "needs_approval", "owner_types"):
            rid = reply.get("id")
            try:
                n = int(rid) if rid is not None else None
            except (TypeError, ValueError):
                return "this action needs an item number from the current list"
            if n is None:
                return "this action needs an item number from the current list"
            item = next((i for i in view.items if i.id == n), None)
            if item is None:
                return f"item {n} isn't in the current list"
            if a in ("click", "type", "owner_types") and self.window is not None and not self._may_touch(self.window):
                return ("this window isn't yours, it's look-only: use open_url for the web (your own Edge), "
                        "launch_app for an app, or new_page in an app window you opened")
            if a in ("type", "owner_types") and item.kind not in TYPEABLE:
                return f"item {n} is a {item.kind}, not a field you can type into"
            if a == "type" and not (reply.get("text") or "").strip():
                return "type needs the text to type"
            if a == "type" and item.kind in TYPEABLE and not reply.get("submit") and \
                    _same_text(self._field_text(item), reply.get("text") or ""):
                # 2026-10-11 batch 8: OCR read Notepad's cursor after "Bread" as "Breadl", the model retyped a
                # correct list, and the retype went wrong. The tool's own reading of the field is exact.
                return ("this field already holds exactly that text (read by the tool, exact; the screen's OCR can "
                        "misread the cursor as l or |): don't type it again, go on with the next part of the goal")
            # "a 200 word story": models can't count their own words (2026-10-10, task-100 wrote 167). Counted here,
            # before it's typed, so a short one is rewritten instead of found out at the end.
            want = re.search(r"\b(\d{2,5})[- ]words?\b", self.goal or "", re.IGNORECASE)
            if a == "type" and want and item.kind == "Document":
                n_words, need = len((reply.get("text") or "").split()), int(want.group(1))
                if n_words < LENGTH_SLACK * need:
                    return (f"your text has {n_words} words; the goal asks for {need}: write it again, at least "
                            f"{need} words, then type it")
        if a == "done":
            evidence = [unlist(e) for e in (reply.get("evidence") or []) if isinstance(e, str) and e.strip()]
            if not (reply.get("answer") or "").strip():
                if asks_for_information(self.goal) or REPORTS.search(self.goal or ""):
                    return "done needs the answer"
                # A task that only does something: its steps are its report (2026-10-11 batch 9: a saved note
                # cost a refused step over a missing summary line)
                reply["answer"] = f"Done: {self.goal.strip()}"
            if not asks_for_information(self.goal):
                return None                     # a task: judged from its steps by the double-check (verify_task)
            if not evidence:
                return "done needs evidence: exact quotes copied from the list"
            # 2026-10-05: "76" and "°" passed as evidence, but the page was Chicago's weather, and "Springfield"
            # was only in the address the tool had typed itself. Evidence must be real phrases on the CURRENT
            # page, not text the task typed, and one of them must name what the goal asks about.
            short = [e for e in evidence if len(e.strip()) < 8]
            if short:
                return f"evidence must be whole phrases from the list (at least 8 characters), not {short[0]!r}"
            # The page, plus what the tool itself read exactly during the run (open_file's "it starts ... read from
            # the disk"): "read it, then close it" leaves nothing on the screen to quote (2026-10-11)
            page = _norm(" \n ".join(self._page_text(view) + self._tool_readings()))
            missing = [e for e in evidence if _norm(e) not in page]
            if missing:
                return (f"this evidence isn't on the current page (text you typed doesn't count; copy it exactly "
                        f"from the list): {missing[0][:80]!r}")
            subjects = goal_subjects(self.goal)
            # the most specific name (the first: "Springfield" in "Springfield, Illinois"); a line that only
            # shares the state ("Chicago, Illinois") isn't about the goal
            # spaces ignored: OCR read "Story1.txt" as "Story 1 .txt" and a right answer was refused (2026-10-11)
            if subjects and not any(subjects[0].replace(" ", "") in _norm(e).replace(" ", "") for e in evidence):
                return (f"the evidence must show it's about {subjects[0].title()} (quote the line on the page that "
                        f"names it); if the page is about something else, keep going")
        if a == "press" and self.window is not None and not self._may_touch(self.window):
            # 2026-10-10: keys go to the window in front, and press wasn't checked like click and type: a run
            # pressed Ctrl+W in a Notepad tab it didn't make (an earlier try's). Keys only go to its own windows.
            return ("this window isn't yours, it's look-only: keys only go to windows and pages you opened (use "
                    "launch_app, open_url or new_page for your own)")
        if a == "press" and EDITING_KEYS.search(reply.get("keys") or "") and \
                any(i.focused and i.kind in TYPEABLE for i in view.items):
            # 2026-10-08: twice "delete the second half" went wrong with selection keys (everything deleted, then
            # one character). Text is changed by typing the whole new text, which is read back.
            return ("don't edit text with selection or delete keys: use type with the COMPLETE new text for that "
                    "field (it replaces everything and is read back)")
        if a == "launch_app" and (reply.get("app") or "").strip().lower() in self.launched:
            # 2026-10-08 (ninth live run): after closing Notepad the model reopened it "to verify", Windows brought the
            # unsaved text back, and the model halved it again. Checking is the tool's job (the trail and the
            # double-check); an app opened once in a run isn't opened again.
            return (f"{reply.get('app')} was already opened in this run: don't reopen it to check, the tool's record "
                    "of every step is the check. If it's still open use switch_window; if every part of the goal is "
                    "done, reply done")
        if a == "open_url" and not re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}", (reply.get("url") or "").strip(), re.IGNORECASE):
            return "open_url needs a web address"
        return None

    # ------------------------------------------------------------------ acting
    def act(self, reply: dict[str, Any], view: screen.View) -> str:
        a = reply["action"]
        h = self.hands
        item = None
        if reply.get("id") is not None and a in ("click", "type"):
            item = next(i for i in view.items if i.id == int(reply["id"]))
        win = self.window
        if a == "click":
            assert item is not None  # check() guarantees a target id for click/type
            if RISKY_CLICK.search(item.name or ""):
                raise _NeedsApproval(item, f"clicking {item.name!r}")
            answered = actions.click(item, win, h)
            return f"clicked {item.kind} {item.name!r}" + (f" (answered a pop-up: {answered})" if answered else "")
        if a == "type":
            assert item is not None  # check() guarantees a target id for click/type
            tasks.check_may_type_here(self.task, win)
            address = item.name == screen.EDGE_ADDRESS_BAR
            # Human pace only where a site could be watching for bots; desktop apps get fast typing (read back
            # all the same). 2026-10-08: a 92-character paragraph took about 6 s in Notepad.
            pace = h.typing_delay
            if win is not None and not self._is_browser(win):
                h.typing_delay = self.FAST_TYPING
            try:
                actions.type_into(item, reply["text"], win, h, address=address)
                text_typed = reply.get("text", "").strip()
                if "@" in text_typed or "email" in (item.name or "").lower() or "user" in (item.name or "").lower():
                    self._last_typed_user = text_typed
            finally:
                h.typing_delay = pace
            if reply.get("submit"):
                if address:
                    h.press("delete")          # Edge's autocomplete, selected after the typed address
                h.press("enter")
                time.sleep(1.5)
                return f"typed into {item.name or item.kind!r} and pressed Enter"
            return f"typed into {item.name or item.kind!r} (not submitted: Enter wasn't pressed)"
        if a == "press":
            actions.press(reply.get("keys") or "enter", win, h)
            return f"pressed {reply.get('keys') or 'enter'}"
        if a == "scroll":
            assert win is not None  # check() guarantees a window for scroll
            r = win.BoundingRectangle
            h.scroll((r.left + r.right) // 2, (r.top + r.bottom) // 2, 5 if reply.get("direction") == "up" else -5)
            return f"scrolled {reply.get('direction') or 'down'}"
        if a == "open_url":
            url = reply["url"].strip()
            if self.edge is None or not self._alive(self.edge):
                self.edge = browser.open_edge("about:blank")
                self.task.opened(self.edge)
                time.sleep(1.5)
                actions.clear_popups(self.edge, h)
            actions.open_url(url if url.startswith("http") else "https://" + url, self.edge, h)
            self.window = self.edge
            return f"opened {url}"
        if a == "launch_app":
            import uiautomation as auto
            before = {w.NativeWindowHandle for w in auto.GetRootControl().GetChildren()}
            w = actions.launch_app(reply.get("app") or "", h)
            self.launched.add((reply.get("app") or "").strip().lower())
            if w is not None and w.NativeWindowHandle not in before:
                self.task.opened(w)
            self.window = w
            extra = ""
            if w is not None and not h.dry_run and self._is_notepad(w):
                # Windows 11 Notepad brings back unsaved text from before (even a new window shows it). The task works
                # on an empty page of its own, never on text it didn't type. 2026-10-08: four runs in a row each
                # halved the text the previous run had "closed without saving".
                # Never a reason to fail the launch itself: a failed "open notepad" made the model open it again
                # (refused) and the run got stuck (2026-10-08, tenth live run).
                try:
                    actions.bring_to_front(w, h)
                    tab = tasks.ensure_blank_page(self.task, w, h)
                    extra = f"; working on its own empty page ({tab})"
                    # A moment for a just-opened Notepad to settle before a long text goes in (2026-10-10: in both
                    # Story1.txt runs the first pass of the story read back wrong and was typed again)
                    time.sleep(1.0)
                except Exception as e:
                    extra = (f"; Notepad is open but its own empty page isn't confirmed ({type(e).__name__}): use "
                             "new_page before typing")
            return (f"opened {reply.get('app')}" + (" (a new window)" if self.task.owns_window(w) else " (an existing window)")
                    + extra)
        if a == "save_as":
            return self._save_as(reply, win, h)
        if a == "open_file":
            return self._open_file(reply, win, h)
        if a == "new_page":
            tab = tasks.ensure_blank_page(self.task, win, h)
            return f"a new empty page of its own ({tab})"
        if a == "switch_window":
            import uiautomation as auto
            want = (reply.get("window") or "").lower()
            target = next((w for w in auto.GetRootControl().GetChildren() if want and want in (w.Name or "").lower()), None)
            if target is None:
                raise actions.ActionFailed(f"no window titled like {want!r}")
            actions.bring_to_front(target, h)
            self.window = target
            return f"switched to {target.Name[:60]!r}"
        if a == "wait":
            time.sleep(2.0)
            return "waited"
        raise actions.ActionFailed(f"can't act on {a!r}")

    def _may_touch(self, win) -> bool:
        """Clicking or typing: only in windows the task opened, pages it made, or what the owner named.
        (A dry run on 2026-10-05: the step model chose to click a button in the owner's code editor.)"""
        try:
            tasks.check_may_type_here(self.task, win)
            return True
        except tasks.NotMine:
            return False
        except Exception:
            return False

    @staticmethod
    def _alive(w) -> bool:
        try:
            _ = w.BoundingRectangle                # raises if the window has closed
            return bool(w.Name)
        except Exception:
            return False

    # ------------------------------------------------------------------ the stuck ladder
    def unstick(self, view: screen.View) -> None:
        """Called after 2 failed or unconfirmed steps in a row."""
        self.emit("progress", message="stuck: looking again")
        try:
            # Only ever the task's own window's pop-ups (2026-10-11: a "Continue with Google" box on the owner's
            # Pinterest page was answered as the task's)
            if self.window is None or (self.task.windows and not self.task.owns_window(self.window)):
                raise LookupError("not the task's window")
            answered = actions.clear_popups(self.window, self.hands)          # its own rules
            if answered:
                self.hints.append(f"(its own check) answered a pop-up: {', '.join(answered)}")
        except actions.NeedsDecision:
            raise
        except Exception:
            pass
        # The local model's second look is off unless ACU_LOCAL_LOOK=1: on the Mini's CPU (no GPU) it took 25 s to
        # 2 minutes per question, its answers are only hints, and a Flash-Lite re-plan reads the same list
        # (2026-10-08, the owner: "the local model is slow on the Mini").
        png = self._screenshot() if os.environ.get("ACU_LOCAL_LOOK") == "1" else None
        if png:
            self.emit("progress", message="stuck: asking the local model for a second look")
            q1 = self.brain.local_look("Is there a pop-up or dialog box on top asking the user something? "
                                       "Answer yes or no, then quote its question in a few words.", png)
            q2 = self.brain.local_look("In one sentence, what is the main thing shown on this screen?", png)
            # Checked: a "yes, a pop-up" the tool can't find is marked unconfirmed
            if q1:
                found = popups.find_any(self.window) if self.window is not None else None
                note = "" if (q1.lower().startswith("yes") == bool(found)) else " (UNCONFIRMED by the list)"
                self.hints.append(f"pop-up? {q1[:120]}{note}")
            if q2:
                self.hints.append(f"screen: {q2[:160]}")
        self.emit("progress", message="stuck: re-planning")
        self.make_plan(self.look(), replan=True)

    def _screenshot(self) -> bytes | None:
        if self.window is None:
            return None
        try:
            import ocr
            r = self.window.BoundingRectangle
            img, _ = ocr.grab((r.left, r.top, r.right, r.bottom))
            img.thumbnail((1280, 1280))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            return None

    # ------------------------------------------------------------------ the run
    def preview(self) -> dict[str, Any]:
        """The plan and first step the model would take, with no mouse or keyboard (a dry run)."""
        view = self.look()
        reply = self.brain.step(self._messages(view))
        self._take_plan(reply)
        return {"plan": self.plan, "first_step": reply, "problem": self.check(reply, view),
                "window": view.window, "items": len(view.items), "requests": self.brain.requests}

    def _on_retype(self, detail: str) -> None:
        """Typed text didn't read back right and will be cleared and typed again: logged and narrated."""
        self.retyped = True
        if self.log:
            self.log.write({"event": "retype", "detail": detail})
        self.emit("progress", message=f"retyping: {detail}")

    def run(self) -> Result:
        self.started_at = time.time()          # files the goal saves must be newer (saved_file_problem)
        if not self.hands.dry_run:
            actions.minimize_all()             # nothing of the owner's covers the task's own windows (the owner's rule)
            self.emit("progress", message="minimized every window, so the task's own windows are in front")
        actions.ON_RETYPE = self._on_retype
        # A screenshot only when typing read back wrong, before it's retyped: data/runs/<id>/snap-*
        actions.ON_SNAPSHOT = self.log.snapshot if self.log else None
        res = Result("out_of_steps")
        ladder_used = 0
        try:
            # The same task done cleanly before: its steps again without the AI (retasks.py)
            done = self._try_retask()
            if done is not None:
                if "_finish" in done:
                    return self._finish(res, done["_finish"], question=done.get("question"))
                return self._finish(res, "done", **done)
            done = self._try_recipe()
            if done is not None:
                return self._finish(res, "done", **done)

            # No separate planning call: the first step's reply carries the plan (2026-10-08: a whole request and a
            # whole screen read spent before anything happened).
            for n in range(1, self.max_steps + 1):
                try:
                    res.steps = n
                    if self.done_refusals >= 2:
                        # Twice "done" and twice refused: redoing the job again isn't the answer (2026-10-08: a run
                        # went round and round until it was killed). The owner decides.
                        last = self.history[-1]["result"] if self.history else ""
                        return self._finish(res, "needs_owner", question="I finished twice but the double-check "
                                            f"didn't agree: {last[:200]}")
                    self._wait_while_owner_in_control()
                    view = self.look()

                    phone_prompt = self.detect_phone_prompt(view)
                    if phone_prompt:
                        offered = self._signin_options(view, password=False)
                        ans = self._ask("choice", phone_prompt, choices=[TAPPED_YES] + list(offered)[:2])
                        if ans is None:
                            return self._finish(res, "needs_owner", question=phone_prompt)
                        try:
                            picked = self._pick(ans, offered)
                        except (tasks.NotMine, actions.ActionFailed) as e:
                            picked = None
                            self.history.append({"action": f"the owner's choice {ans!r}", "result": f"FAILED: {e}", "expect": None})
                        if picked:
                            self.history.append({"action": f"the owner chose {ans!r}", "result": picked, "expect": None})
                            time.sleep(self.task.pause)
                            continue
                        if ans.strip().lower() == TAPPED_YES.lower():
                            self.emit("progress", message="waiting for sign-in to complete on phone...")
                            start_url = self._url() or ""
                            for _ in range(22):
                                time.sleep(2.0)
                                cur_url = self._url() or ""
                                if cur_url and cur_url != start_url and "signin" not in cur_url.lower():
                                    break
                            self.history.append({"action": "phone prompt", "result": "the owner tapped Yes; continuing", "expect": None})
                            self.trouble = 0
                            continue

                    reply = self.brain.step(self._messages(view))
                    self._take_plan(reply)
                    if self.history and reply.get("previous_ok") is False:
                        self.history[-1]["result"] += " -- NOT confirmed on screen"
                        self.trouble += 1
                    problem = self.check(reply, view)
                    a = reply.get("action")

                    target_name = None
                    if reply.get("id") is not None:
                        try:
                            it = next((i for i in view.items if i.id == int(reply["id"])), None)
                            if it:
                                target_name = it.name
                        except (ValueError, TypeError):
                            pass
                    trip = self.circuit_breaker.record(
                        action=a or "",
                        target_id=reply.get("id"),
                        target_name=target_name,
                        text=reply.get("text"),
                        keys=reply.get("keys"),
                        direction=reply.get("direction"),
                        screen=screen_fingerprint(view),
                    )
                    if trip.tripped:
                        self.emit("progress", message="circuit breaker tripped", warning=trip.message)
                        self.hints.append(trip.message)
                        self.trouble += 1
                        if self.circuit_breaker.consecutive_trips >= 3:
                            self._safe_abort_checkpoint(trip.message)
                            return self._finish(res, "needs_owner", question=trip.message)

                    self.emit("progress", step=n, action=a, id=reply.get("id"), observation=reply.get("observation"),
                              expect=reply.get("expect"), problem=problem, target=target_name or reply.get("name"),
                              app=reply.get("app"), keys=reply.get("keys"), url=reply.get("url"),
                              window=reply.get("window"), direction=reply.get("direction"),
                              text=reply.get("text") if a == "type" else None, path=reply.get("path"))
                    # The trail a task's "done" is judged by (verify_task): what the screen showed, then what was
                    # done. A typed secret never reaches it (owner_types carries no text).
                    did = a or "?"
                    if target_name:
                        did += f" on {target_name!r}"
                    for k in ("text", "keys", "app", "url", "window"):
                        if reply.get(k) and a != "owner_types":
                            did += f" {k}={str(reply[k])[:200]!r}"
                    self.trail.append({"n": n, "saw": (reply.get("observation") or "")[:300],
                                       "did": did + (f" (refused: {problem[:120]})" if problem else "")})

                    if self.log:
                        self.log.step(f"step {n}: {a}", reply=reply, problem=problem, window=view.window)
                    if problem:
                        self.history.append({"action": json.dumps({k: reply.get(k) for k in ("action", "id", "text", "url")}),
                                             "result": "REFUSED: " + problem, "expect": None})
                        self.trouble += 1
                    elif a == "done":
                        refused = self.verify_answer(reply, view)
                        if refused is None:
                            return self._finish(res, "done", answer=reply["answer"], evidence=reply.get("evidence") or [])
                        self.emit("progress", step=n, message="answer refused", why=refused)
                        self.history.append({"action": "done (" + (reply.get("answer") or "")[:60] + ")",
                                             "result": "REFUSED: " + refused, "expect": None})
                        self.trouble += 1
                        self.done_refusals += 1
                    elif a == "fail":
                        return self._finish(res, "failed", detail={"reason": reply.get("reason")})
                    elif a == "ask_owner":
                        q = reply.get("question") or reply.get("reason") or "What should I do?"
                        # On a sign-in page the owner picks: their password (typed from the app straight into the
                        # box) or one of the page's other ways to sign in ('Try another way')
                        offered = self._signin_options(view)
                        ans = self._ask("choice", q, choices=list(offered)) if offered else self._ask("text", q)
                        if ans is None:
                            return self._finish(res, "needs_owner", question=q)
                        try:
                            picked = self._pick(ans, offered)
                        except (tasks.NotMine, actions.ActionFailed) as e:
                            picked = None
                            self.history.append({"action": f"the owner's choice {ans!r}", "result": f"FAILED: {e}", "expect": None})
                            self.trouble += 1
                        if picked:
                            self.history.append({"action": f"the owner chose {ans!r}", "result": picked, "expect": None})
                            time.sleep(self.task.pause)
                        elif not offered:
                            self._owner_said(q, ans)
                    elif a == "owner_types":
                        item = next(i for i in view.items if i.id == int(reply["id"]))
                        try:
                            typed = self._owner_types(item, reply.get("question") or f"Type it into {item.name or 'this box'}")
                        except (tasks.NotMine, actions.ActionFailed) as e:
                            typed = None
                            self.history.append({"action": "owner_types", "result": f"FAILED: {e}", "expect": None})
                            self.trouble += 1
                        if typed is None and self.asker is None:
                            return self._finish(res, "needs_owner", question=reply.get("question") or "a password or code")
                        if typed:
                            time.sleep(self.task.pause)
                    elif a == "needs_approval":
                        target = next((i for i in view.items if i.id == int(reply["id"])), None)
                        outcome = self._approval(target, reply.get("reason") or
                                                 f"clicking {(target.name if target else 'unknown')!r}", view)
                        if outcome != "approved":
                            return self._finish(res, outcome, question=reply.get("reason"),
                                                detail={"item": target.describe() if target else None, "window": view.window})
                    else:
                        try:
                            result = self.act(reply, view)
                            self._record(reply, view)
                            self.history.append({"action": a, "result": result, "expect": reply.get("expect")})
                            self.trail[-1]["did"] += f" -> {result[:160]}"
                            self._note_fields()
                            if reply.get("previous_ok") is not False:
                                self.trouble = 0
                            finished = self._chain(reply, view, n)
                            if finished is not None:
                                return self._finish(res, "done", **finished)
                        except _NeedsApproval as e:
                            outcome = self._approval(e.item, e.why, view)
                            if outcome != "approved":
                                return self._finish(res, outcome, question=e.why,
                                                    detail={"item": e.item.describe(), "window": view.window})
                        except tasks.NotMine as e:
                            self.history.append({"action": a, "result": f"REFUSED: {e}", "expect": None})
                            self.trouble += 1
                        except actions.NeedsDecision as e:
                            picked = self._popup_choice(e)
                            if picked is None:
                                return self._finish(res, "needs_owner", question=str(e),
                                                    detail={"popup_text": e.popup.text[:300], "choices": e.popup.choices})
                            self.history.append({"action": "pop-up", "result": f"the owner chose {picked!r}", "expect": None})
                        except (actions.ActionFailed, tasks.Unexpected) as e:
                            self.history.append({"action": a, "result": f"FAILED: {e}", "expect": None})
                            # Why, for the owner too (2026-10-11: four failed open_url's left no reason anywhere)
                            self.emit("progress", message=f"that didn't work: {e}")
                            if self.log:
                                self.log.write({"event": "failed", "action": a, "why": str(e)[:300]})
                            self.trouble += 1
                        time.sleep(self.task.pause)
                    if self.trouble >= 2:
                        if ladder_used >= 2:
                            last = self.history[-1]["result"] if self.history else ""
                            ans = self._ask("text", f"I'm stuck on: {self.goal[:120]}. Last: {last[:160]}. What should I do?")
                            if ans is None:
                                return self._finish(res, "needs_owner", question="stuck after looking again and re-planning: "
                                                    + last)
                            self._owner_said("I'm stuck. What should I do?", ans)
                            ladder_used = 0
                            continue
                        ladder_used += 1
                        self.unstuck = True                    # not a clean run (no retask)
                        try:
                            self.unstick(view)
                        except actions.NeedsDecision as e:
                            # A pop-up found while getting unstuck: ask, as during a step (it used to end the task)
                            picked = self._popup_choice(e)
                            if picked is None:
                                return self._finish(res, "needs_owner", question=str(e),
                                                    detail={"popup_text": e.popup.text[:300], "choices": e.popup.choices})
                            self.history.append({"action": "pop-up", "result": f"the owner chose {picked!r}", "expect": None})
                        self.trouble = 0
                except ScreenChanged as e:
                    # A monitor came on (or went off): every position moved. Nothing was done at the old positions;
                    # the next step looks again at the new size.
                    self.emit("progress", message=f"{e}: looking again at the new size")
                    self.history.append({"action": "the screen", "result": f"{e} (a monitor came on or off): "
                                         "everything moved; look again before acting", "expect": None})
                except _HandedBack:
                    # The owner used Take over and handed back: the screen may be different now, so the next
                    # step looks again instead of acting on the old picture
                    self.history.append({"action": "the owner took over the computer from their phone",
                                         "result": "they handed back; the screen may have changed: look again "
                                                   "before acting", "expect": None})
                    self.owner_instructions.append("The owner used Take over and handed back: check what's on "
                                                   "screen now; they may have done the step themselves.")
                    self.trouble = 0
            return self._finish(res, "out_of_steps")
        except Stopped as e:
            return self._finish(res, "stopped", detail={"reason": str(e)})
        except actions.NeedsDecision as e:
            return self._finish(res, "needs_owner", question=str(e),
                                detail={"popup_text": e.popup.text[:300], "choices": e.popup.choices})
        except BrainError as e:
            return self._finish(res, "failed", detail={"reason": f"the models: {e}"})
        except _Paused as e:
            self._safe_abort_checkpoint(str(e))
            return self._finish(res, "needs_owner", question=str(e))


    def _try_recipe(self) -> dict[str, Any] | None:
        """A saved recipe for this goal (data/recipes/*.json), replayed without the models. It works in its own
        Edge like any step, and its answer must pass the same evidence check as the model's (`check` on "done"),
        or the normal loop takes over from where the recipe left off. Kill switch, owner and time limit still
        stop it (Stopped is never swallowed). Returns the "done" fields, or None. Not in a dry run: a recipe
        needs to read real pages."""
        import recipes
        if self.hands.dry_run:
            return None
        try:
            match = recipes.find_matching_recipe(self.goal)
        except Exception:
            return None
        if not match:
            return None
        recipe, params = match
        try:
            if self.edge is None or not self._alive(self.edge):
                self.edge = browser.open_edge("about:blank")
                self.task.opened(self.edge)
                time.sleep(1.5)
                actions.clear_popups(self.edge, self.hands)
            self.window = self.edge
            outcome = recipes.execute_recipe(recipe, params, self.edge, self.hands, self.task, emit=self.emit)
        except Stopped:
            raise
        except Exception as e:
            outcome = None
            self.emit("progress", message=f"recipe {recipe.name!r} failed ({type(e).__name__}); the AI takes over")
        if outcome:
            view = self.look()
            problem = self.check({"action": "done", "answer": outcome["answer"], "evidence": outcome["evidence"]},
                                 view)
            if problem is None:
                return {"answer": outcome["answer"], "evidence": outcome["evidence"],
                        "detail": {"recipe": recipe.name}}
            self.emit("progress", message=f"recipe {recipe.name!r} answer refused: {problem}")
        self.history.append({"action": f"saved recipe {recipe.name!r}",
                             "result": "didn't finish; carry on from the current page", "expect": None})
        return None

    def _finish(self, res: Result, status: str, **kw) -> Result:
        res.status = status
        for k, v in kw.items():
            setattr(res, k, v)
        self.offer_to_save_password(status)              # a password the owner typed: offered only if this worked
        self._maybe_save_retask(status, res)
        res.requests, res.local_requests = self.brain.requests, self.brain.local_requests
        if self.log:
            res.run = self.log.run_id
            # The trail (what each step showed, did, and what the text fields really held) goes in the run's log
            # too: it was only in memory, so a run's result couldn't be checked afterwards (2026-10-08).
            self.log.write({"event": "trail", "steps": self.trail[-30:]})
            self.log.end(status == "done", status=status, answer=res.answer, question=res.question,
                         requests=res.requests, local_requests=res.local_requests)
        return res


