"""Fewer round trips: the actions the model chains after one step ("then"), done back to back with the
same checks, plus what the tool reads exactly between steps (text fields, the window title) and which kind
of window is in front. Mixed into Loop. (Split out of loop.py, 2026-10-11.)"""
import time
from typing import Any, ClassVar

import actions
import screen
import task as tasks
from goals import REPORTS, asks_for_information
from rules import LIST_LIMIT, TYPEABLE, _NeedsApproval


class ChainMixin:
    # Attributes provided by Loop (or a sibling mixin) that this mixin uses.
    # Declared here so mypy can see them; the values live on Loop.
    history: Any
    trail: Any
    log: Any
    emit: Any
    goal: Any
    trouble: Any
    done_refusals: Any
    plan: Any
    _current_window: Any
    verify_answer: Any
    look: Any
    check: Any
    act: Any
    _record: Any
    # ------------------------------------------------------------------ fewer round trips
    BROWSERS: ClassVar[set[str]] = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe"}
    CHAINABLE: ClassVar[set[str]] = {"click", "type", "press", "scroll", "launch_app", "new_page", "switch_window", "wait", "done"}
    CHAIN_DESKTOP, CHAIN_WEB = 6, 1
    FAST_TYPING = (0.004, 0.012)       # desktop apps: no site watching for bots, the field is read back anyway

    def _note_fields(self) -> None:
        """Adds what the window's text fields really hold now to the last trail entry, read by the tool, not claimed
        by the model: the task double-check trusts these. Password boxes are left out. (2026-10-08: a chain
        deleted one character instead of half a paragraph, the model said "deleted the second half", and the
        double-check, seeing only claims, agreed.)"""
        if not self.trail:
            return
        fields = self._field_readings()
        if fields:
            self.trail[-1]["did"] += " -> fields now: " + "; ".join(fields[:4])

    def _field_readings(self) -> list[str]:
        """What the window's text fields really hold now, read by the tool (exact, not OCR). Passwords left out.
        Used after each step (the trail) and by the task double-check."""
        try:
            now = self._quick_look()
        except Exception:
            return []
        fields = []
        for i in now.items:
            if i.kind not in TYPEABLE or "password" in (i.name or "").lower():
                continue
            text = actions.read_value(i) if i.control is not None else i.value
            if text is None and i.value is not None:
                text = i.value
            # Never "empty" when it simply couldn't be read: the double-check trusts these readings
            fields.append(f"{i.name or i.kind}: " + (f"{text[:400]!r}" if text is not None else "(couldn't read)"))
        return fields

    def _title(self) -> str:
        """The task's window title now (exact; Notepad's starts with the page's first line)."""
        try:
            return (self.window.Name or "").replace("\u200b", "") if self.window is not None else "(none)"
        except Exception:
            return "(unknown)"

    def _take_plan(self, reply: dict[str, Any]) -> None:
        """The first reply carries the plan (no separate planning request)."""
        if self.plan.get("steps") or not isinstance(reply.get("plan"), list) or not reply["plan"]:
            return
        self.plan = {"steps": [str(s) for s in reply["plan"]][:12], "done_when": str(reply.get("done_when") or "")}
        self.emit("progress", message="plan", plan=self.plan["steps"])
        if self.log:
            self.log.write({"event": "plan", "replan": False, **self.plan})

    @staticmethod
    def _is_dialog(win) -> bool:
        """A standard Windows dialog (Save as, Open, Confirm, message boxes): class #32770."""
        try:
            return win is not None and (win.ClassName or "") == "#32770"
        except Exception:
            return False

    @staticmethod
    def _is_notepad(win) -> bool:
        try:
            return screen.process_name(win.ProcessId).lower() == "notepad.exe"
        except Exception:
            return False

    def _is_browser(self, win) -> bool:
        try:
            return screen.process_name(win.ProcessId).lower() in self.BROWSERS
        except Exception:
            return True                    # unknown: treat it as the web (the careful side)

    def _quick_look(self) -> screen.View:
        """The accessibility list only: no OCR, no waiting (between chained actions, where nothing loads)."""
        win = self._current_window()
        view = screen.look(win, use_ocr=False, wait=False)
        view.items = view.items[:LIST_LIMIT * 2]
        self.window = win
        return view

    def _resolve(self, nxt: dict[str, Any], first: screen.View, now: screen.View) -> dict[str, Any] | None:
        """A chained action with its target as an item of the list NOW: an id from the first list is found again
        (same kind and label), or "kind" + "name" picks the first match. None when it isn't there."""
        out = dict(nxt)
        if out.get("action") not in ("click", "type"):
            out["id"] = None
            return out
        found = None
        if out.get("id") is not None:
            try:
                orig = next(i for i in first.items if i.id == int(out["id"]))
                found = actions.find_matching_item(orig, now.items)
            except (StopIteration, ValueError, TypeError):
                found = None
        else:
            kind = (out.get("kind") or "").strip().lower()
            name = (out.get("name") or "").strip().lower()
            found = next((i for i in now.items if not i.offscreen and (not kind or i.kind.lower() == kind)
                          and (not name or name in (i.name or "").lower())), None) if (kind or name) else None
        if found is None and out["action"] == "type":
            # Typing whose target can't be found (2026-10-08: the model chained "type into 1", a number from the
            # desktop's list, after opening Notepad): the window's ONE text field, if there's exactly one.
            fields = [i for i in now.items if i.kind in TYPEABLE and not i.offscreen]
            # Only an EMPTY one (a page just opened): 2026-10-10, "type the path into 59" (a dialog field that wasn't
            # there) landed in Notepad's page and replaced the story with the path
            if len(fields) == 1 and not self._field_text(fields[0]).strip():
                found = fields[0]
        if found is None:
            return None
        out["id"] = found.id
        return out

    @staticmethod
    def _field_text(item: screen.Item) -> str:
        try:
            text = actions.read_value(item) if item.control is not None else item.value
        except Exception:
            text = None
        return text if text is not None else (item.value or "")

    def _chain(self, reply: dict[str, Any], first: screen.View, n: int) -> dict[str, Any] | None:
        """The actions the model chained after this one ("then"), done back to back without asking it again. Each
        is checked like a normal step and done with the same checks (typing read back, clicks checked for pop-ups);
        the chain stops at the first surprise and the loop looks again. Returns the "done" fields when a chained
        done passes its double-check. Desktop apps chain up to 6; the web 1, and nothing after a page load.
        (2026-10-08: open Notepad, type, retype half, close took 7 requests and 84 s, one action per request.)"""
        chain = reply.get("then")
        if not isinstance(chain, list) or not chain:
            return None
        web = self._is_browser(self.window) if self.window is not None else False
        if web and (reply.get("action") == "open_url" or reply.get("submit")
                    or (reply.get("action") == "press" and "enter" in (reply.get("keys") or "").lower())):
            return None                    # a page is loading: look properly first
        start_dialog = self._is_dialog(self.window)
        for k, nxt in enumerate(chain[:self.CHAIN_WEB if web else self.CHAIN_DESKTOP], 1):
            if not isinstance(nxt, dict) or nxt.get("action") not in self.CHAINABLE:
                break
            a = nxt["action"]
            time.sleep(0.3)
            # A dialog opened by the chain so far (Save as, Confirm, Open): its fields weren't on the list the chain
            # was planned from, so look first. (2026-10-10: after ctrl+s the chain typed the file name into the Save
            # dialog's Search box, pressed Enter (a search), then sent alt+f4, meant for Notepad, to the dialog.)
            if not start_dialog and self._is_dialog(self._current_window()):
                self.history.append({"action": f"chained {a}", "result": "stopped: a dialog opened; look at it before "
                                     "acting in it", "expect": None})
                return None
            if a == "done" and (asks_for_information(self.goal) or REPORTS.search(self.goal or "")):
                # An answer chained before the earlier actions' results were seen is a guess (2026-10-11 batch 6:
                # open_file, then a made-up first heading; the file's real one was in open_file's own result)
                self.history.append({"action": "chained done", "result": "stopped: answer from what the earlier "
                                     "actions showed; look first", "expect": None})
                return None
            if a == "done":
                view = self.look()         # a full read for the double-check
                resolved = dict(nxt, id=None)
            else:
                resolved, view = None, None
                for _ in range(3):         # an app just opened may still be filling in its list
                    view = self._quick_look()
                    resolved = self._resolve(nxt, first, view)
                    if resolved is not None:
                        break
                    time.sleep(0.5)
                if resolved is None:
                    self.history.append({"action": f"chained {a}", "result": "stopped: its target isn't on screen; "
                                         "look again", "expect": None})
                    return None
            assert resolved is not None  # the "done" branch sets a dict; the other returns when None
            if a == "type" and resolved.get("id") is not None:
                target = next((i for i in view.items if i.id == resolved["id"]), None)
                old = " ".join(self._field_text(target).split()) if target is not None else ""
                new = " ".join(str(resolved.get("text") or "").split())
                if old and new[:12].lower() != old[:12].lower():
                    # Typing replaces a field's text: in a chain (no fresh look) it may only rewrite the same text
                    # (keep half, fix a word), never put something different over it (2026-10-10: a file path typed
                    # over a 1,200-character story)
                    assert target is not None  # `old` is non-empty only when a target was found
                    self.history.append({"action": f"chained type into {target.name or target.kind!r}", "result":
                                         "stopped: it would replace the text already there with something different; "
                                         "look again", "expect": None})
                    return None
            problem = self.check(resolved, view)
            label = f"{n}.{k}"
            # Details for the plain-language log (activity.say); agentcomputeruse.emit_run turns typed text into a
            # safe sentence before anything leaves this process
            self.emit("progress", step=label, action=a, id=resolved.get("id"), chained=True,
                      expect=resolved.get("expect"), problem=problem, target=resolved.get("name"),
                      app=resolved.get("app"), keys=resolved.get("keys"), url=resolved.get("url"),
                      window=resolved.get("window"), direction=resolved.get("direction"),
                      text=resolved.get("text") if a == "type" else None)
            did = a + "".join(f" {f}={str(resolved[f])[:200]!r}" for f in ("text", "keys", "app", "name")
                              if resolved.get(f))
            self.trail.append({"n": label, "saw": "(chained: no new look)", "did": did})
            if self.log:
                self.log.step(f"step {label}: {a} (chained)", reply=resolved, problem=problem, window=view.window)
            if problem:
                self.history.append({"action": f"chained {a}", "result": "REFUSED: " + problem, "expect": None})
                return None
            if a == "done":
                refused = self.verify_answer(resolved, view)
                if refused is None:
                    return {"answer": resolved.get("answer") or "", "evidence": resolved.get("evidence") or []}
                self.emit("progress", step=label, message="answer refused", why=refused)
                self.history.append({"action": "done (chained)", "result": "REFUSED: " + refused, "expect": None})
                self.trouble += 1
                self.done_refusals += 1
                return None
            try:
                result = self.act(resolved, view)
                self._record(resolved, view)
            except (_NeedsApproval, tasks.NotMine, actions.NeedsDecision, actions.ActionFailed, tasks.Unexpected) as e:
                self.history.append({"action": f"chained {a}", "result": f"stopped: {e}", "expect": None})
                return None                # the next step looks again; approvals and pop-ups get their own turn
            self.history.append({"action": f"{a} (chained)", "result": result, "expect": resolved.get("expect")})
            self.trail[-1]["did"] += f" -> {result[:160]}"
            self._note_fields()
        return None
