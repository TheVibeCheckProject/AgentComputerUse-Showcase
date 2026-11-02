"""Oscillation and cycle circuit breaker for autonomous computer use.

Detects when an agent is caught in repetitive loops:
- 1-step repeat: A -> A -> A (e.g. clicking the same disabled button 3 times).
- 2-step oscillation: A -> B -> A -> B (e.g. toggling two tabs back and forth).
- 3-step loop: A -> B -> C -> A -> B -> C (e.g. cycle between 3 elements).

Trips with diagnostic reasoning feedback to break the cycle and force re-planning.
"""
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple


@dataclass
class ActionSignature:
    action: str
    target_id: Optional[int] = None
    target_name: Optional[str] = None
    text: Optional[str] = None
    keys: Optional[str] = None
    direction: Optional[str] = None
    screen: Optional[str] = None       # fingerprint of what was on screen when the action was chosen

    def key(self) -> Tuple:
        norm_name = (self.target_name or "").strip().lower()[:60]
        norm_text = (self.text or "").strip().lower()[:60]
        # Item ids are renumbered on every look, so a named target is known by its name only.
        target = None if norm_name else self.target_id
        # The screen is part of the key: the same action on a screen that changed (scrolling down a long page,
        # Next through a wizard) is progress, not a loop. Only the same action on the same screen repeats.
        # (2026-10-08: without this, three scrolls in a row tripped and five aborted the run.)
        return (self.action, target, norm_name, norm_text, self.keys, self.direction, self.screen)


@dataclass
class TripResult:
    tripped: bool
    cycle_length: int = 0
    repetitions: int = 0
    pattern_summary: str = ""
    message: str = ""


class CircuitBreaker:
    """Sliding-window oscillation detector across recent actions."""

    def __init__(self, window_size: int = 6, repeat_threshold: int = 3, cycle_threshold: int = 2):
        self.window_size = window_size
        self.repeat_threshold = repeat_threshold  # 1-step repeat count to trip
        self.cycle_threshold = cycle_threshold    # 2/3-step cycle repetitions to trip
        self.history: Deque[ActionSignature] = deque(maxlen=window_size)
        self.consecutive_trips: int = 0

    def record(self, action: str, target_id: Optional[int] = None,
               target_name: Optional[str] = None, text: Optional[str] = None,
               keys: Optional[str] = None, direction: Optional[str] = None,
               screen: Optional[str] = None) -> TripResult:
        """Records an action and evaluates whether a loop has formed."""
        sig = ActionSignature(action=action, target_id=target_id, target_name=target_name,
                              text=text, keys=keys, direction=direction, screen=screen)
        self.history.append(sig)
        result = self.evaluate()
        if result.tripped:
            self.consecutive_trips += 1
        else:
            self.consecutive_trips = 0
        return result

    def evaluate(self) -> TripResult:
        """Checks the current history for repeating 1-step, 2-step, or 3-step patterns."""
        n = len(self.history)
        if n < self.repeat_threshold:
            return TripResult(tripped=False)

        keys = [sig.key() for sig in self.history]

        # 1. Check 1-step repeat: last K items identical
        if n >= self.repeat_threshold:
            last = keys[-1]
            if all(k == last for k in keys[-self.repeat_threshold:]):
                action_name = self.history[-1].action
                target = self.history[-1].target_name or str(self.history[-1].target_id or "")
                summary = f"repeated {action_name} on {target!r}"
                msg = (f"CIRCUIT BREAKER: Repeated action '{summary}' {self.repeat_threshold} times "
                       f"with no progress. Do NOT repeat this action. Look around the page for "
                       f"required fields, error messages, or try an alternative route.")
                return TripResult(tripped=True, cycle_length=1, repetitions=self.repeat_threshold,
                                  pattern_summary=summary, message=msg)

        # 2. Check 2-step oscillation: A -> B -> A -> B (length 4)
        if n >= 4:
            sub = keys[-4:]
            if sub[0] == sub[2] and sub[1] == sub[3] and sub[0] != sub[1]:
                a_desc = f"{self.history[-2].action}({self.history[-2].target_name or ''})"
                b_desc = f"{self.history[-1].action}({self.history[-1].target_name or ''})"
                summary = f"oscillating between [{a_desc}] and [{b_desc}]"
                msg = (f"CIRCUIT BREAKER: 2-step oscillation detected: {summary}. "
                       f"Stop alternating between these actions. Re-examine the page goal.")
                return TripResult(tripped=True, cycle_length=2, repetitions=2,
                                  pattern_summary=summary, message=msg)

        # 3. Check 3-step loop: A -> B -> C -> A -> B -> C (length 6)
        if n >= 6:
            sub = keys[-6:]
            if sub[0] == sub[3] and sub[1] == sub[4] and sub[2] == sub[5]:
                summary = "3-step cyclic pattern repeated twice"
                msg = ("CIRCUIT BREAKER: 3-step cyclic loop detected. "
                       "The action sequence is repeating without making progress. "
                       "Break the cycle and re-plan.")
                return TripResult(tripped=True, cycle_length=3, repetitions=2,
                                  pattern_summary=summary, message=msg)

        return TripResult(tripped=False)

    def reset(self) -> None:
        self.history.clear()
        self.consecutive_trips = 0
