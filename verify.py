"""The double-checks before a run counts as done: a task judged from its steps and the screen now
(verify_task), an answer given a second opinion (verify_answer). Mixed into Loop. (Split out of loop.py,
2026-10-11.)"""
from typing import Any

import goals
import screen
from brain import BrainError
from goals import SAVE_NAME, _norm, asks_for_information
from rules import LIST_LIMIT


class VerifyMixin:
    # Attributes provided by Loop (or a sibling mixin) that this mixin uses.
    # Declared here so mypy can see them; the values live on Loop.
    log: Any
    goal: Any
    trail: Any
    brain: Any
    plan: Any
    _tool_readings: Any
    _title: Any
    _still_open_problem: Any
    _field_readings: Any

    @staticmethod
    def _page_text(view: screen.View) -> list[str]:
        """What the page itself says: every item's label, but not field contents (text the task may have typed,
        like the address bar)."""
        return [i.name for i in view.items if i.name and i.kind not in ("Edit", "ComboBox")]

    def _double_check(self, msgs) -> dict[str, Any]:
        """A yes/no verdict and one sentence: a short reply (2026-10-08: with a plan's 900-token allowance one took
        12 s)."""
        check = getattr(self.brain, "check", None)
        return check(msgs) if check else self.brain.plan(msgs)

    def verify_task(self, reply: dict[str, Any], view: screen.View) -> str | None:
        """A task's "done", double-checked: every part of the goal must have been done, and done right, judged from
        what the screen showed after each step (the trail) and the screen now. Returns why it's refused, or None.
        (2026-10-08: "delete the second half" deleted everything, the trail said "0 characters", and the run still
        called itself done.)"""
        acted = sum(1 for t in self.trail if not str(t.get("did", "")).startswith("done"))
        last = getattr(self, "_last_refusal", None)
        claim = " ".join(str(reply.get("answer") or "").lower().split())
        if last and last[0] == acted and last[2] == claim:
            # 2026-10-10: refused ("the story was overwritten with the path"), then the same claim with nothing done
            # in between passed a second, kinder check. Only the SAME claim: a corrected answer is checked afresh
            # (2026-10-11 batch 6: a guessed heading was refused, the right one then refused as "nothing done")
            return f"nothing was done since this was refused: {last[1]}"
        problem = goals.saved_file_problem(self.goal, getattr(self, "started_at", 0.0)) or self._still_open_problem()
        if problem:
            self._last_refusal = (acted, problem, claim)
            if self.log:
                self.log.write({"event": "verify", "kind": "file", "supported": False, "why": problem})
            return f"not done: {problem}"
        trail = "\n".join(f"{t['n']}. screen: {t['saw']} | then: {t['did']}" for t in self.trail[-20:])
        msgs = [{"role": "system", "content": "You check that a task on a computer was really carried out. Be strict "
                 "about the RESULT, not the method: every part of the goal must have the outcome asked for (e.g. "
                 "'delete the second half' means exactly the first half is left; deleting everything is wrong). How it "
                 "got there doesn't matter: this tool changes text by typing the whole new text, so 'the text was "
                 "replaced with its first half' IS the second half deleted. The steps' screens were seen at the time, "
                 "so a result that's gone from the screen now (a window that was closed) can still be confirmed by "
                 "them. 'fields now' is what the text fields really held after that step, read by the tool ('(couldn't read)' means unknown, not empty): trust it "
                 "over every claim and judge the text yourself (is the remaining text really the first half?). "
                 "'SCREEN NOW' is read by OCR and can get single characters wrong (O/Q, l/I/1, a stray _ or |): never "
                 "refuse over such a small difference; 'FIELDS NOW' and the window title are exact and win (2026-10-10: "
                 "OCR read 'AlwaysOnAgent' as 'AlwaysQnAgen_t' and a correct task was refused). "
                 "Lengths and counts the owner gives ('a 200 word story') are approximate: about right is right "
                 "(the tool already checked it's at least 70%); never refuse over length alone. "
                 "Reply with JSON: {\"supported\": true|false, \"why\": \"one sentence\"}"},
                {"role": "user", "content": f"GOAL: {self.goal}\nDONE WHEN: {self.plan.get('done_when') or '(not given)'}"
                 f"\nCLAIMED: {reply.get('answer')}\n\nSTEPS (what the screen showed, then what was done):\n{trail}"
                 f"\n\nWINDOW TITLE NOW: {self._title()}\nFIELDS NOW (exact, read by the tool):\n"
                 + ("\n".join(f"- {f}" for f in self._field_readings()[:4]) or "- (no text fields)")
                 + (f"\n\n{goals.saved_file_note(self.goal, getattr(self, 'started_at', 0.0))} (read from the disk: exact)"
                    if SAVE_NAME.search(self.goal or "") else "")
                 + "\n\nSCREEN NOW (OCR, approximate):\n" + "\n".join(f"- {t}" for t in self._page_text(view)[:LIST_LIMIT])}]
        try:
            v = self._double_check(msgs)
        except BrainError as e:
            return f"couldn't double-check the task ({e}); keep going or ask_owner"
        if self.log:
            self.log.write({"event": "verify", "kind": "task", **v})
        if v.get("supported") is True:
            return None
        why = f"double-check: not done right: {v.get('why') or ''}"
        self._last_refusal = (acted, why, claim)
        return why

    def verify_answer(self, reply: dict[str, Any], view: screen.View) -> str | None:
        """A second opinion before an answer counts: one fresh request, a stronger model if one is free.
        Returns why it's refused, or None. A task (not a question) is checked by verify_task instead."""
        if not asks_for_information(self.goal):
            return self.verify_task(reply, view)
        msgs = [{"role": "system", "content": "You check an answer against a screen. Be strict: the screen must "
                 "show the answer for exactly what the goal asks (the right place, item or person), not something "
                 "similar, and the exact reading asked for: a forecast, high, low, hourly or 'feels like' number is not 'the temperature now' (refuse it). Text typed into the address bar or a search box doesn't count. Reply with JSON: "
                 "{\"supported\": true|false, \"why\": \"one sentence\", \"quote\": \"the exact line on the screen "
                 "that names the subject\"}"},
                {"role": "user", "content": f"GOAL: {self.goal}\nANSWER: {reply.get('answer')}\n\nSCREEN (page text, "
                 f"not typed fields):\n" + "\n".join(f"- {t}" for t in self._page_text(view)[:LIST_LIMIT])
                 + ("\n\nREAD BY THE TOOL DURING THE RUN (exact; counts like the screen):\n"
                    + "\n".join(f"- {t}" for t in self._tool_readings()) if self._tool_readings() else "")}]
        try:
            v = self._double_check(msgs)
        except BrainError as e:
            return f"couldn't double-check the answer ({e}); keep going or ask_owner"
        if self.log:
            self.log.write({"event": "verify", **v})
        if v.get("supported") is True:
            q = v.get("quote") or ""
            if q and _norm(q) not in _norm(" \n ".join(self._page_text(view))):
                return "the double-check quoted something that isn't on the page; keep going"
            return None
        return f"double-check: not supported: {v.get('why') or ''}"
