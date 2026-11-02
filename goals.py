"""What a goal asks for, read from its words: a file to save (and whether it was saved), a question to
answer, its subjects; and text helpers for quotes. No screen, no mouse. (Split out of loop.py, 2026-10-11.)"""
import os
import re
from typing import Any, Dict, List, Optional

from rules import LENGTH_SLACK


def known_folder(name: str) -> str:
    """The real path of the owner's Desktop, Documents or Downloads (OneDrive may have moved them): Windows' own
    record of it (User Shell Folders), else the usual place under the user's folder."""
    reg = {"Desktop": "Desktop", "Documents": "Personal",
           "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}.get(name, name)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return os.path.expandvars(winreg.QueryValueEx(k, reg)[0])
    except (ImportError, OSError):
        return os.path.join(os.path.expanduser("~"), name)


SAVE_NAME = re.compile(r"\bsave\w*\b.*?\b(?:as|named|called)\s+[\"'\u201c]?([\w\- .()]+?\.[A-Za-z0-9]{1,5})\b", re.I)


def saved_file(goal: str, since: float) -> Optional[Dict[str, Any]]:
    """The file a "save it as NAME" goal asks for, as it is on disk: {"name", "places", "path" (None when not saved
    during this run where the goal says), "words", "start"}. None when the goal saves nothing by name."""
    m = SAVE_NAME.search(goal or "")
    if not m:
        return None
    name = m.group(1).strip()
    places = ["Desktop"] if re.search(r"\bdesktop\b", goal, re.I) else ["Desktop", "Documents", "Downloads"]
    for place in places:
        f = os.path.join(known_folder(place), name)
        if os.path.isfile(f) and os.path.getmtime(f) >= since - 5:
            try:
                text = open(f, encoding="utf-8", errors="replace").read(200_000)
            except OSError:
                text = ""
            return {"name": name, "places": places, "path": f, "words": len(text.split()),
                    "start": " ".join(text.split())[:160]}
    return {"name": name, "places": places, "path": None, "words": 0, "start": ""}


def saved_file_note(goal: str, since: float) -> str:
    """One line for the model: is the goal's file saved yet (read from the disk)?"""
    f = saved_file(goal, since)
    if f is None:
        return ""
    if f["path"] is None:
        return f"ON DISK: {f['name']} is not saved in {' or '.join(f['places'])} yet."
    return (f"ON DISK: {f['path']} was saved during this run ({f['words']} words, starting: {f['start'][:80]!r}). If "
            f"that's the right content, saving is DONE: don't save or type again.")


def saved_file_problem(goal: str, since: float) -> Optional[str]:
    """For a goal that saves a file under a name ("save it as Story1.txt to the desktop"): None if that file was
    written during the run where the goal says (the Desktop when it says so, else Desktop, Documents or Downloads),
    else what's wrong. Read from the disk, not the screen (2026-10-10: a run saved "Once upon a time....txt" in the
    tool's own folder, called itself done, and the double-check agreed)."""
    f = saved_file(goal, since)
    if f is None:
        return None
    name, places = f["name"], f["places"]
    if f["path"] is not None:
        # "a 200 word story": the file must hold most of it (2026-10-10: Story1.txt held only the file's path)
        want = re.search(r"\b(\d{2,5})[- ]words?\b", goal or "", re.I)
        if want and f["words"] < LENGTH_SLACK * int(want.group(1)):
            return (f"{f['path']} holds only {f['words']} words ({f['start'][:80]!r}), not the {want.group(1)} words "
                    f"the goal asks for")
        return None
    return (f"no file named {name!r} was saved in {' or '.join(places)} during this run (checked on disk). In the "
            f"Save dialog, type the full path into the 'File name' box, e.g. {os.path.join(known_folder(places[0]), name)}")


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w%$.,:/-]+", " ", (s or "").lower()).split())


QUESTION_START = re.compile(r"^\s*(please\s+)?(find|what|what's|whats|how|when|where|who|whose|which|why|tell|look up|"
                            r"check|get|search|is|are|was|were|does|do|did|can|could|will|should|show me|list|read)\b",
                            re.I)


def asks_for_information(goal: str) -> bool:
    """A question ("Find the temperature in Springfield") ends with an answer read off the screen; a task ("Open
    notepad, type..., close it") ends with something done, and its result may be gone from the screen (a closed
    window). The two are checked differently at the end (2026-10-08: a finished Notepad task had nothing left to
    quote, and the model quoted "Recycle Bin")."""
    return "?" in goal or bool(QUESTION_START.match(goal))


# A goal that reports something back, anywhere in it ("Open X and tell me its first heading")
REPORTS = re.compile(r"\b(tell me|let me know|what|which|how (many|much|long|old)|find|read me|report)\b", re.I)


LIST_FORMAT = re.compile(r"^\s*(?:\[?\d+\]?\s*)?(?:(?:Button|Edit|Text|Hyperlink|Link|ComboBox|Document|Image|MenuItem|"
                         r"ListItem|TabItem|CheckBox|RadioButton|Group|Pane|Custom|Heading|DataItem|TreeItem)\s+)?"
                         r"['\"\u201c](.*)['\"\u201d]\s*$", re.S)


def unlist(quote: str) -> str:
    """A quote as the page says it, without the list's own formatting the model copied ("[12] Button 'Choose File
    Story1.txt'" -> "Choose File Story1.txt"). 2026-10-11 batch: a right answer was refused six times over this."""
    m = LIST_FORMAT.match(quote or "")
    return m.group(1) if m else (quote or "")


def goal_subjects(goal: str) -> List[str]:
    """What the goal is about, by its proper names: 'Find the current temperature in Springfield, Illinois' ->
    ['springfield', 'illinois']. Capitalised words that aren't the first word or a common word."""
    common = {"find", "what", "how", "when", "where", "who", "which", "tell", "show", "get", "look", "check", "open",
              "search", "give", "list", "the", "is", "are", "i", "my", "me", "please", "can", "you", "a", "an",
              # buttons, places and app words aren't what an answer is about (2026-10-11: "the page's Choose File
              # button" made "choose" the subject, and the right answer, Story1.txt, was refused twice)
              "choose", "click", "press", "tap", "select", "submit", "send", "file", "files", "button", "page", "form",
              "field", "box", "window", "tab", "desktop", "documents", "downloads", "folder", "notepad", "edge",
              "browser", "save", "close", "type", "enter", "upload", "download", "do", "don't", "not", "go", "ok",
              "cancel", "then", "and", "from", "to", "into", "on", "in", "of"}
    words = re.findall(r"[A-Za-z][\w'-]*", goal)
    return [w.lower() for n, w in enumerate(words) if n > 0 and w[0].isupper() and w.lower() not in common]


def _same_text(field: str, text: str) -> bool:
    """The same text, line endings and spacing at the ends aside."""
    norm = lambda s: "\n".join(line.rstrip() for line in (s or "").replace("\r\n", "\n").replace("\r", "\n")
                               .strip().split("\n"))
    return bool(norm(text)) and norm(field) == norm(text)
