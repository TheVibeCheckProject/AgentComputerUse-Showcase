"""Retasks in the loop (retasks.py): recording a run's steps, saving a clean run, and repeating a saved one for
the SAME task without the AI, every step through the normal checks (check, act). Mixed into Loop. (2026-10-11)"""
import time
from typing import Any, Dict, Optional

import actions
import goals
import retasks
import screen
import task as tasks
from goals import REPORTS, asks_for_information
from rules import _NeedsApproval

# History entries that mean a run wasn't clean (a retask is saved only from a clean one)
NOT_CLEAN = ("REFUSED", "FAILED", "NOT confirmed")
NOT_CLEAN_ACTIONS = ("the owner", "pop-up", "the screen", "saved recipe", "phone prompt", "owner_types")


class RetaskMixin:
    # ------------------------------------------------------------------ recording a run
    def _record(self, reply: Dict[str, Any], view: screen.View) -> None:
        """A step that worked, by its target's kind and name."""
        kind = name = ""
        if reply.get("action") in ("click", "type") and reply.get("id") is not None:
            item = next((i for i in view.items if str(i.id) == str(reply["id"])), None)
            if item is None:
                self.recorded_gap = True             # a step whose target isn't known can't be repeated
                return
            kind, name = item.kind, (item.name or "")
        self.recorded.append(retasks.step_of(reply, kind, name))

    def _is_question(self) -> bool:
        return asks_for_information(self.goal) or bool(REPORTS.search(self.goal or ""))

    def _clean_run(self) -> bool:
        """Done with nothing that went wrong or needed the owner: no refusal, failure, retype, unconfirmed step,
        stuck ladder, pop-up or sign-in."""
        if self.retyped or self.done_refusals or self.hints or self.owner_instructions or self._autofilled \
                or getattr(self, "recorded_gap", False) or getattr(self, "unstuck", False):
            return False
        for h in self.history:
            if any(m in str(h.get("result", "")) for m in NOT_CLEAN):
                return False
            if any(m in str(h.get("action", "")) for m in NOT_CLEAN_ACTIONS):
                return False
        return True

    def _maybe_save_retask(self, status: str, res=None) -> None:
        if res is not None and self.replay_state in ("to_reading_point", "done"):
            res.detail = {**(res.detail or {}), "retask": True}     # the card says it was a repeat
        if status != "done" or self.hands.dry_run or self.replay_state is not None or not self._clean_run():
            return
        try:
            saved = retasks.save(self.goal, "info" if self._is_question() else "task", self.recorded,
                                 run=self.log.run_id if self.log else None)
        except OSError:
            saved = None
        if saved:
            self.emit("progress", message=f"saved as a retask: the same task again repeats these "
                                          f"{len(saved['steps'])} steps without the AI", retask=True)
            if self.log:
                self.log.write({"event": "retask_saved", "id": saved["id"], "steps": len(saved["steps"])})

    # ------------------------------------------------------------------ repeating one
    def _replay_target(self, step: Dict[str, Any], view: screen.View) -> Optional[screen.Item]:
        kind, name = (step.get("kind") or "").lower(), (step.get("name") or "").strip().lower()
        # On screen first; one off screen counts too (clicking and typing scroll it into view: no stored scrolls)
        live = sorted((i for i in view.items if i.kind.lower() == kind), key=lambda i: bool(i.offscreen))
        exact = [i for i in live if (i.name or "").strip().lower() == name]
        if exact:
            return exact[0]
        if name:
            return next((i for i in live if name in (i.name or "").lower()), None)
        return live[0] if len(live) == 1 else None

    def _write_fresh(self, item: screen.Item) -> str:
        """Text the task asks for, written new (writing is never replayed). One request."""
        out = self.brain.plan([
            {"role": "system", "content": "You write the text a computer task asks for; it will be typed into a "
                                          "page. Write it fresh: this task was done before, don't try to repeat it. "
                                          "Follow the task's length and form. Reply with JSON only: "
                                          "{\"text\": \"...\"}"},
            {"role": "user", "content": f"TASK: {self.goal}\nIt goes into: {item.kind} {item.name!r}"}])
        return str((out or {}).get("text") or "").strip()

    def _off_course(self, k: int, why: str, count: bool = True) -> None:
        """The replay stops; the AI carries on from where things are. A failure counts against the retask unless
        it wasn't the retask's fault (a pop-up, the owner's approval)."""
        self.replay_state = "off_course"
        dropped = retasks.record(self.goal, False) if count else None
        self.emit("progress", message=f"repeating an earlier run stopped at step {k} ({why}); the AI carries on"
                                      + ("; that retask is dropped" if dropped else ""))
        if self.log:
            self.log.write({"event": "retask_off_course", "step": k, "why": why[:300], "dropped": bool(dropped)})
        self.history.append({"action": "repeating an earlier run of this same task",
                             "result": f"stopped at its step {k}: {why}. Carry on from the current screen",
                             "expect": None})

    def _try_retask(self) -> Optional[Dict[str, Any]]:
        """The SAME task done before, cleanly: its steps again, without the AI. Returns the "done" fields (a task
        whose result checks out), {"_finish": status, ...} (the owner said no to a risky click), or None (the AI
        carries on: a question's reading point, or something unexpected)."""
        if self.hands.dry_run:
            return None
        r = retasks.find(self.goal)
        if r is None:
            return None
        self.replay_state = "running"
        steps = r["steps"]
        self.emit("progress", message=f"this task was done before: repeating its {len(steps)} step{'' if len(steps) == 1 else 's'} without the AI",
                  retask=True)
        if self.log:
            self.log.write({"event": "retask", "id": r["id"], "steps": len(steps)})
        for k, step in enumerate(steps, 1):
            a = step.get("action")
            reply = {f: step[f] for f in step if f not in ("kind", "name", "write")}
            view = self.look()
            item = None
            if a in ("click", "type"):
                for _ in range(3):                     # a page may still be loading
                    item = self._replay_target(step, view)
                    if item is not None:
                        break
                    time.sleep(1.0)
                    view = self.look()
                if item is None:
                    self._off_course(k, f"{step.get('kind')} {step.get('name')!r} isn't on the screen")
                    return None
                reply["id"] = item.id
            if a == "type" and step.get("write"):
                reply["text"] = self._write_fresh(item)
                if not reply["text"]:
                    self._off_course(k, "no fresh text came back", count=False)
                    return None
            problem = self.check(reply, view)
            if problem:
                self._off_course(k, f"the step was refused: {problem}")
                return None
            label = f"R{k}"
            self.emit("progress", step=label, action=a, target=step.get("name"), app=reply.get("app"),
                      keys=reply.get("keys"), url=reply.get("url"), path=reply.get("path"),
                      text=reply.get("text") if a == "type" else None, retask=True)
            did = a + (f" on {step.get('name')!r}" if step.get("name") else "")
            self.trail.append({"n": label, "saw": "(repeating an earlier run)", "did": did})
            try:
                result = self.act(reply, view)
            except _NeedsApproval as e:
                outcome = self._approval(e.item, e.why, view)
                if outcome != "approved":
                    return {"_finish": outcome, "question": e.why}
                result = f"the owner approved {e.why}"
            except actions.NeedsDecision as e:
                self._off_course(k, f"a pop-up came up ({str(e)[:80]})", count=False)
                return None
            except (actions.ActionFailed, tasks.NotMine, tasks.Unexpected) as e:
                self._off_course(k, f"it didn't work: {e}")
                return None
            self.history.append({"action": f"{a} (repeated)", "result": result, "expect": None})
            self.trail[-1]["did"] += f" -> {result[:160]}"
            self._note_fields()
            time.sleep(self.task.pause)
        if r.get("kind") == "info":
            # The hard line: answers are never replayed. The model reads what's on the screen NOW.
            retasks.record(self.goal, True)
            self.replay_state = "to_reading_point"
            self.history.append({"action": "repeating an earlier run of this same task",
                                 "result": f"all {len(steps)} steps done, without the AI. Now read the answer from "
                                           "the CURRENT screen (it may have changed since last time) and reply done",
                                 "expect": None})
            return None
        problem = goals.saved_file_problem(self.goal, getattr(self, "started_at", 0.0)) or self._still_open_problem()
        if problem:
            self._off_course(len(steps), f"the result doesn't check out: {problem}")
            return None
        retasks.record(self.goal, True)
        self.replay_state = "done"
        # The agent's card adds "(Repeated an earlier run: N AI requests.)" from detail.retask: said once, not twice
        return {"answer": f"Done: {self.goal.strip()}", "evidence": [],
                "detail": {"retask": r["id"], "repeated_steps": len(steps)}}
