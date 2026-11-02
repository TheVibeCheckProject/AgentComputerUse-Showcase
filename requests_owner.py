"""Asking the owner and waiting for the answer (the owner's rule: anything needed from a person goes to the
DeskAI app). The tool saves the request (with a screenshot, the spot outlined) under data/requests/, prints a
`needs_you` event (the agent turns it into a loud phone alert and keeps reminding), and waits for the answer the
agent writes to data/answers/<id>.json when the owner replies in the app.

Kinds: approve (a risky step: Approve or Deny), choice (pick one of the choices), text (a typed answer), secret
(a password or code the owner types in the app: the tool types it straight into the field; the model never sees it,
and its answer file is deleted as soon as it's read).
"""
import datetime
import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ACU_DATA_DIR") or (ROOT / "data"))
REQUESTS = DATA / "requests"
ANSWERS = DATA / "answers"
# Then the task pauses safely: nothing half-submitted. 30 minutes matches the agent's reminders
# (AlwaysOnAgent core/computer_use.py REMINDERS); 2026-10-08: a 3-minute default gave up while the phone was
# still buzzing. ACU_OWNER_WAIT_SECONDS overrides it.
WAIT = int(os.environ.get("ACU_OWNER_WAIT_SECONDS") or 30 * 60)

KEEP_DAYS = 14                     # finished requests' files (screenshot, question) are deleted after this


class NoAnswer(Exception):
    pass


def prune(now: Optional[float] = None) -> int:
    """Deletes request and answer files older than KEEP_DAYS (2026-10-10, the owner: the images mustn't add up)."""
    now = time.time() if now is None else now
    gone = 0
    for folder in (REQUESTS, ANSWERS):
        try:
            files = list(folder.iterdir())
        except OSError:
            continue
        for f in files:
            try:
                if f.is_file() and now - f.stat().st_mtime > KEEP_DAYS * 86400:
                    f.unlink()
                    gone += 1
            except OSError:
                pass
    return gone


def screenshot(window_rect: Optional[Tuple[int, int, int, int]] = None,
               highlight: Optional[Tuple[int, int, int, int]] = None) -> Optional[bytes]:
    """The window (or screen) as PNG, with the spot in question outlined in red."""
    try:
        import io
        import ocr
        from PIL import ImageDraw
        img, (ox, oy) = ocr.grab(window_rect)
        if highlight:
            d = ImageDraw.Draw(img)
            l, t, r, b = highlight
            for w in range(4):
                d.rectangle((l - ox - 4 - w, t - oy - 4 - w, r - ox + 4 + w, b - oy + 4 + w), outline=(230, 30, 30))
        img.thumbnail((1400, 1400))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None


def ask(kind: str, question: str, choices: Optional[List[str]] = None, url: Optional[str] = None,
        png: Optional[bytes] = None, emit: Callable = lambda *a, **k: None, wait: float = WAIT,
        poll: float = 1.0, stopped: Callable[[], bool] = lambda: False) -> str:
    """Saves the request, announces it, and waits for the owner's answer; raises NoAnswer after `wait` s."""
    REQUESTS.mkdir(parents=True, exist_ok=True)
    ANSWERS.mkdir(parents=True, exist_ok=True)
    prune()
    rid = uuid.uuid4().hex[:12]
    shot = None
    if png:
        shot = REQUESTS / f"{rid}.png"
        shot.write_bytes(png)
    req = {"id": rid, "kind": kind, "question": question, "choices": choices or [], "url": url,
           "screenshot": shot.name if shot else None,
           "created": datetime.datetime.now().isoformat(timespec="seconds")}
    (REQUESTS / f"{rid}.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    emit("needs_you", **req)
    answer_file = ANSWERS / f"{rid}.json"
    end = time.time() + wait
    try:
        while time.time() < end:
            if answer_file.exists():
                try:
                    value = str(json.loads(answer_file.read_text(encoding="utf-8")).get("answer", ""))
                except ValueError:
                    value = None                # being written: read again
                if value is not None:
                    if kind == "secret":
                        try:
                            answer_file.unlink()    # a password or code never stays on disk
                        except OSError:
                            pass
                    return value
            if stopped():
                break
            time.sleep(poll)            # 2026-10-08: without it the wait spun one CPU core flat out
        time_desc = f"{int(wait)} seconds" if wait < 60 else f"{int(wait // 60)} minutes"
        raise NoAnswer(f"no answer within {time_desc}")

    finally:
        req["closed"] = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            (REQUESTS / f"{rid}.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
