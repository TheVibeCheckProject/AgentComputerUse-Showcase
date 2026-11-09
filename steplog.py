"""The step log: every run gets a folder with each step's screenshot and one line per step, so the owner can
see exactly what it did. data/runs/<run id>/log.jsonl + step-NN.jpg

Runs don't pile up (2026-10-10, the owner: "I don't want the images adding up to become a constraint"): screenshots
are JPEGs (about a fifth of the PNGs they were: ~2 MB a run became ~0.4 MB), and each new run deletes run folders
older than KEEP_DAYS, always keeping the newest KEEP_MIN."""
import datetime
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("ACU_RUNS_DIR") or (ROOT / "data" / "runs"))
KEEP_DAYS = 14
KEEP_MIN = 30


def prune(now: float | None = None) -> int:
    """Deletes run folders older than KEEP_DAYS, never the newest KEEP_MIN. Returns how many went."""
    now = time.time() if now is None else now
    try:
        runs = sorted((p for p in RUNS.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    gone = 0
    for p in runs[KEEP_MIN:]:
        try:
            if now - p.stat().st_mtime > KEEP_DAYS * 86400:
                shutil.rmtree(p, ignore_errors=True)
                gone += 1
        except OSError:
            pass
    return gone


class StepLog:
    def __init__(self, goal: str, run_id: str | None = None, screenshots: bool = True):
        prune()
        self.run_id = run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = RUNS / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.screenshots = screenshots
        self.write({"event": "start", "goal": goal})

    def write(self, entry: dict) -> None:
        entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"), **entry}
        with open(self.dir / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def step(self, action: str, **details) -> int:
        """Records one step, with a screenshot of the screen as it was before the step."""
        self.n += 1
        shot = None
        if self.screenshots:
            try:
                import ocr
                img, _ = ocr.grab()
                shot = self.dir / f"step-{self.n:02d}.jpg"
                img.convert("RGB").save(shot, quality=70)
            except Exception:
                shot = None
        self.write({"event": "step", "n": self.n, "action": action, "screenshot": shot.name if shot else None,
                    **details})
        return self.n

    def snapshot(self, label: str) -> str | None:
        """An extra screenshot in the middle of a step (snap-NNN.jpg, JPEG to keep runs small), noted in the log
        with what it shows. 2026-10-10, the owner: "take screenshots to figure it out, throughout the test" (a
        story's first typing pass kept vanishing with nothing in the log to say why)."""
        if not self.screenshots:
            return None
        self.snaps = getattr(self, "snaps", 0) + 1
        name = f"snap-{self.snaps:03d}.jpg"
        try:
            import ocr
            img, _ = ocr.grab()
            img.convert("RGB").save(self.dir / name, quality=70)
        except Exception:
            return None
        self.write({"event": "snapshot", "step": self.n, "file": name, "label": label})
        return name

    def end(self, ok: bool, **details) -> None:
        self.write({"event": "end", "ok": ok, **details})
