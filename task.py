"""Understand before acting (the owner's rule, 2026-10-05).

A live demo launched Notepad expecting a new blank page; Windows 11 brought the owner's existing Notepad
window to the front on one of THEIR files, and the demo typed into it and pressed Ctrl+S, saving over it.
So a task now:

- keeps track of what it made: windows it opened, tabs it created, files it saved. The owner's own windows
  and files are look-only; typing into them or saving them is refused unless the task names them;
- checks the situation before each step (an expectation), and stops when the screen isn't what it
  expected, instead of pushing ahead;
- never saves blind: Ctrl+S only on its own untitled page, which must bring up a Save dialog;
- goes slowly: a pause and a fresh look between steps, its intention logged before it acts.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

UNTITLED = ("untitled",)                    # Notepad's new pages ("Untitled", "Untitled 2")


class Unexpected(Exception):
    """The screen isn't what the step expected: stop, look again, or ask, but don't push ahead."""


class NotMine(Exception):
    """A window, tab or file the task didn't make: look-only."""


def root_owner(hwnd: int) -> int:
    """The window at the top of hwnd's owner chain (a dialog's app window), or 0. Windows only."""
    try:
        import ctypes
        return int(ctypes.windll.user32.GetAncestor(int(hwnd), 3) or 0)  # type: ignore[attr-defined]  # GA_ROOTOWNER; Windows-only API
    except Exception:
        return 0


@dataclass
class Task:
    goal: str
    pause: float = 0.8                      # seconds between steps: slow enough to look again
    windows: set[int] = field(default_factory=set)       # window handles it opened
    tabs: set[str] = field(default_factory=set)          # tab names it created
    tab_ids: set[tuple] = field(default_factory=set)     # the tabs themselves (Notepad renames a new tab
                                                         # after its first line, so names aren't enough)
    files: set[str] = field(default_factory=set)         # files it saved
    allowed: set[str] = field(default_factory=set)       # owner-named windows/files it may change

    def opened(self, win) -> None:
        self.windows.add(win.NativeWindowHandle)

    def owns_window(self, win) -> bool:
        if win.NativeWindowHandle in self.windows:
            return True
        # A dialog its own window opened (a Save prompt, a "file exists" box) is its own too: Windows records the
        # window that owns a dialog (2026-10-10, a Save prompt was refused as "not yours")
        root = root_owner(win.NativeWindowHandle)
        return bool(root) and root != win.NativeWindowHandle and root in self.windows

    def expect(self, check: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
        """Waits (briefly) for the expected situation; raises Unexpected if it doesn't come."""
        end = time.time() + timeout
        while True:
            try:
                if check():
                    return
            except Exception:
                pass
            if time.time() > end:
                raise Unexpected(f"expected {what}, but the screen shows something else")
            time.sleep(0.3)

    def between_steps(self) -> None:
        time.sleep(self.pause)


# ---------------------------------------------------------------------------------------- Notepad
def selected_tab(win):
    """(name, id) of the selected tab in a Windows 11 Notepad window, or (None, None). The id stays the same
    while the tab is open, even when Notepad renames it."""
    import uiautomation as auto
    for c, d in auto.WalkControl(win, maxDepth=12):
        try:
            # The pattern methods live on the control's own class (TabItemControl here), not on the base Control
            if c.ControlTypeName == "TabItemControl" and cast(Any, c).GetSelectionItemPattern().IsSelected:
                return (c.Name or "").strip(), tuple(c.GetRuntimeId())
        except Exception:
            continue
    return None, None


def active_tab(win) -> str | None:
    """The selected tab's name ('Untitled. Unmodified.' style), or None."""
    return selected_tab(win)[0]


def document_text(win) -> str | None:
    import uiautomation as auto
    for c, d in auto.WalkControl(win, maxDepth=25):
        try:
            if c.ControlTypeName == "DocumentControl":
                return cast(Any, c).GetTextPattern().DocumentRange.GetText(-1)     # a DocumentControl's pattern
        except Exception:
            continue
    return None


def is_untitled(tab: str | None) -> bool:
    return tab is not None and tab.lower().startswith(UNTITLED)


def ensure_blank_page(task: Task, win, hands) -> str:
    """Makes sure Notepad is on a new, empty page of the task's own. If it shows one of the owner's files
    (Windows 11 reuses the open window), a new tab is opened with Ctrl+N and checked."""
    tab = active_tab(win)
    if not (is_untitled(tab) and not (document_text(win) or "").strip()):
        hands.press("ctrl+n")
        task.expect(lambda: is_untitled(active_tab(win)) and not (document_text(win) or "").strip(),
                    "a new, empty Notepad page after Ctrl+N")
    tab, tab_id = selected_tab(win)
    task.tabs.add(tab.split(".")[0].strip())
    if tab_id:
        task.tab_ids.add(tab_id)
    return tab


def check_may_type_here(task: Task, win) -> None:
    """Typing goes only into the task's own page (or a window/file the task names)."""
    if task.owns_window(win):
        return
    tab, tab_id = selected_tab(win)
    if tab_id is not None and tab_id in task.tab_ids:
        return
    if tab is not None:
        base = tab.split(".")[0].strip()
        if base in task.tabs or base in task.allowed:
            return
        raise NotMine(f"the page in front is {base!r}, which this task didn't make; it's look-only")
    title = (win.Name or "").replace("\u200b", "")
    if not any(a.lower() in title.lower() for a in task.allowed):
        raise NotMine(f"{title!r} isn't this task's window; it's look-only")


def file_name_box(win):
    """The Save dialog's 'File name' box, searched directly (a capped list can miss it: the dialog lists
    every file in the folder first)."""
    import uiautomation as auto
    for c, d in auto.WalkControl(win, maxDepth=30):
        try:
            if c.ControlTypeName == "EditControl" and "file name" in (c.Name or "").lower() and not c.IsOffscreen:
                return c
        except Exception:
            continue
    return None


def check_may_save(task: Task, win) -> None:
    """Ctrl+S only on the task's own untitled page (so a Save dialog must come up). Saving an existing file
    would overwrite it: refused unless the owner named that file."""
    tab, tab_id = selected_tab(win)
    base = (tab or "").split(".")[0].strip()
    if tab_id is not None and tab_id in task.tab_ids:
        return                                             # its own new page, whatever Notepad renamed it to
    if tab is not None and is_untitled(tab) and base in task.tabs:
        return
    if base and base in task.allowed:
        return
    raise NotMine(f"won't save {base or (win.Name or '')!r}: it isn't this task's new page, and saving "
                  "would overwrite a file")
