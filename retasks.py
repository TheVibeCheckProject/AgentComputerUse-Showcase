"""Retasks: a run that went clean, saved so the SAME task asked again is done without the AI.

The owner (2026-10-11): "if a task succeeded with no errors, we could always rerun it with no token usage". The
steps are the tool's own actions, found again by their target's kind and name (never a screen position), and a
replay sends each through the same checks as a normal step (Loop.check and Loop.act). The hard lines:
- only the SAME task: its wording, give or take spacing, capitals and end punctuation. A similar task with
  other details ("read Packing.txt" after "read Groceries.txt") is never matched, the AI does it fresh;
- answers are never replayed: a question's steps are repeated up to the reading point, then the model reads
  what's on the screen NOW (one request);
- writing is never replayed: text the model wrote (a note, a story) is written fresh, one request; only short
  values (a search term, a path) are typed again;
- only a clean run is saved: done and verified, no refusal, failure, retype, owner's help, sign-in or password;
- anything unexpected (a target that isn't there, a refused step) hands over to the AI from where things are,
  and a retask that goes off course twice is dropped.

Stored in data/retasks/<id>.json. (Separate from recipes.py: hand-written templates with parameters.)
"""
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

RETASKS_DIR = Path(__file__).resolve().parent / "data" / "retasks"
DROP_AFTER_FAILS = 2
# Text the model wrote, written fresh on a replay: a page of an app (Notepad's), a long text, or more than a line
WRITE_KINDS = {"Document"}
WRITE_LENGTH = 120
STEP_FIELDS = ("action", "kind", "name", "text", "submit", "keys", "url", "app", "path", "window", "direction",
               "write")
SECRET_FIELD = re.compile(r"pass(word|code|phrase)?|\bpin\b|one-time|verification code|security code|\bcode\b",
                          re.IGNORECASE)


def norm_goal(goal: str) -> str:
    """The task's wording without the noise: spacing, capitals, end punctuation."""
    return " ".join((goal or "").lower().split()).rstrip(" .!?")


def retask_id(goal: str) -> str:
    return hashlib.sha1(norm_goal(goal).encode("utf-8")).hexdigest()[:12]


def is_written(kind: str, text: str) -> bool:
    """Text the model wrote (written fresh on a replay), not a short value to type again."""
    text = text or ""
    return kind in WRITE_KINDS or len(text) > WRITE_LENGTH or "\n" in text.strip()


def step_of(reply: dict[str, Any], kind: str = "", name: str = "") -> dict[str, Any]:
    """A recorded step: the action and its target by kind and name (the list's id means nothing next time)."""
    s = {k: reply.get(k) for k in STEP_FIELDS if reply.get(k) not in (None, "", False)}
    s.pop("write", None)
    if kind:
        s["kind"] = kind
    if name:
        s["name"] = name
    if s.get("action") == "type":
        s["write"] = is_written(kind, s.get("text") or "")
        if s["write"]:
            s.pop("text", None)                  # never kept: it's written fresh each time
    return s


# Reading moves: scrolling and paging to look for something. Never stored: a click or a typing step scrolls its
# target into view itself, and for a question the looking happens after the reading point (2026-10-11: "what year
# did Missouri become a state" saved 9 scrolls hunting for the answer, which every repeat would have done again).
LOOKING_KEYS = re.compile(r"^\s*((ctrl\+)?(home|end)|page_?(up|down)|up|down|space)\s*$", re.IGNORECASE)


def is_looking(step: dict[str, Any]) -> bool:
    a = step.get("action")
    return a in ("scroll", "wait") or (a == "press" and bool(LOOKING_KEYS.match(str(step.get("keys") or ""))))


def _path(rid: str, folder: Path | None = None) -> Path:
    return (folder or RETASKS_DIR) / f"{rid}.json"


def find(goal: str, folder: Path | None = None) -> dict[str, Any] | None:
    p = _path(retask_id(goal), folder)
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return r if r.get("norm") == norm_goal(goal) and r.get("steps") else None


def save(goal: str, kind: str, steps: list[dict[str, Any]], run: str | None = None,
         folder: Path | None = None) -> dict[str, Any] | None:
    """Saves (or refreshes) the retask for this goal, without its reading moves. Nothing to repeat, or a secret
    field: not saved."""
    steps = [s for s in steps if not is_looking(s)]
    if not steps or any(SECRET_FIELD.search(str(s.get("name") or "")) for s in steps if s.get("action") == "type"):
        return None
    old = find(goal, folder) or {}
    r = {"id": retask_id(goal), "goal": goal.strip(), "norm": norm_goal(goal), "kind": kind, "steps": steps,
         "created": old.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S"),
         "updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "run": run, "uses": old.get("uses", 0), "fails": 0,
         "last_used": old.get("last_used")}
    p = _path(r["id"], folder)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    return r


def record(goal: str, ok: bool, folder: Path | None = None) -> str | None:
    """A replay's outcome: a clean one counts a use; one that went off course counts a failure, and the retask is
    dropped after DROP_AFTER_FAILS in a row. Returns "dropped" when it was."""
    r = find(goal, folder)
    if r is None:
        return None
    if ok:
        r["uses"] = r.get("uses", 0) + 1
        r["fails"] = 0
        r["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        r["fails"] = r.get("fails", 0) + 1
        if r["fails"] >= DROP_AFTER_FAILS:
            forget(r["id"], folder)
            return "dropped"
    _path(r["id"], folder).write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    return None


def list_all(folder: Path | None = None) -> list[dict[str, Any]]:
    d = folder or RETASKS_DIR
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({k: r.get(k) for k in ("id", "goal", "kind", "uses", "fails", "created", "updated", "last_used")}
                   | {"steps": len(r.get("steps") or [])})
    return sorted(out, key=lambda r: r.get("updated") or "", reverse=True)


def forget(rid: str, folder: Path | None = None) -> bool:
    if not re.fullmatch(r"[0-9a-f]{12}", rid or ""):
        return False
    p = _path(rid, folder)
    if p.exists():
        p.unlink()
        return True
    return False
