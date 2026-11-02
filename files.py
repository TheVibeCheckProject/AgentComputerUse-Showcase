"""Files for the loop: Open and Save dialogs done whole by the tool (open_file, save_as), opening a file in
its app, a web page's file picker, and the save guard. Mixed into Loop. (Split out of loop.py, 2026-10-11.)"""
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

import actions
import goals
import screen
import task as tasks
from goals import SAVE_NAME
from rules import TYPEABLE, _NeedsApproval


class FilesMixin:
    def _wait_dialog(self, titles, timeout: float):
        """The dialog in front whose title starts with one of `titles` (lowercase), or None after `timeout`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            w = self._current_window()
            try:
                if self._is_dialog(w) and (w.Name or "").strip().lower().startswith(titles):
                    return w
            except Exception:
                pass
            time.sleep(0.25)
        return None

    OPEN_TITLES = ("open", "file upload", "choose file", "upload")

    def _tool_readings(self) -> List[str]:
        """What the tool read exactly during the run (open_file reports how a file starts, from the disk)."""
        return [h["result"] for h in self.history if "(read from the disk, exact)" in str(h.get("result", ""))][-3:]

    def _find_file(self, path: str) -> str:
        """A full path as given, or a bare name found on the Desktop, in Documents or Downloads."""
        # Windows form (C:\a\b): the Open window rejects "C:/a/b" with an error box (2026-10-11 batch 3)
        path = os.path.normpath(str(path or "").strip().strip('"')) if str(path or "").strip() else ""
        if not path:
            raise actions.ActionFailed("open_file needs the file's path or name")
        if os.path.isabs(path):
            if not os.path.isfile(path):
                raise actions.ActionFailed(f"there's no file at {path}")
            return path
        for place in ("Desktop", "Documents", "Downloads"):
            f = os.path.join(goals.known_folder(place), path)
            if os.path.isfile(f):
                return f
        raise actions.ActionFailed(f"no file named {path!r} on the Desktop, in Documents or Downloads: give the full path")

    def _file_name_box(self, dialog, timeout: float = 6.0):
        """The dialog's "File name" box, once Windows has filled the dialog in (2026-10-11: read the instant the Open
        window appeared, it wasn't there yet, and open_file gave up)."""
        end = time.time() + timeout
        while True:
            view = screen.look(dialog, use_ocr=False, wait=False)
            box = next((i for i in view.items if i.kind in TYPEABLE and
                        (i.name or "").lower().replace("&", "").startswith("file name")), None)
            if box is not None or time.time() >= end:
                return box
            time.sleep(0.4)

    def _fill_open_dialog(self, dialog, path: str, h) -> None:
        path = os.path.normpath(path)
        box = self._file_name_box(dialog)
        if box is not None:
            # Typed once, not read back: the box reads empty (Windows reports the combo around it) or autocompleted,
            # so a read-back retyped the path three times (2026-10-11). The dialog taking the file is the check.
            actions.click(box, dialog, h)
            time.sleep(0.2)
            h.press("ctrl+a")
            h.type_text(path)
        else:
            # Every Windows file dialog: Alt+N puts the keyboard in "File name"
            actions.bring_to_front(dialog, h)
            h.press("alt+n")
            time.sleep(0.3)
            h.press("ctrl+a")
            h.type_text(path)
        h.press("enter")
        end = time.time() + 6
        while time.time() < end:                          # the dialog closes when the file is taken
            w = self._current_window()
            if not (self._is_dialog(w) and (w.Name or "").strip().lower().startswith(self.OPEN_TITLES)):
                return
            time.sleep(0.25)
        raise actions.ActionFailed("the Open window didn't take the file")

    @staticmethod
    def _file_start(path: str) -> str:
        if not path.lower().endswith((".txt", ".md", ".csv", ".json", ".log", ".py", ".html")):
            return ""
        try:
            start = " ".join(open(path, encoding="utf-8", errors="replace").read(1200).split())[:400]
        except OSError:
            return ""
        return f"; it starts (read from the disk, exact): {start!r}" if start else ""

    @classmethod
    def _opener(cls, path: str, app: str = "") -> Optional[List[str]]:
        """The command that opens the file, or None for its usual app (os.startfile). The app the task names wins;
        a text file with no app of its own goes to Notepad. (2026-10-11 batch 4: .md had no app on the Mini, so a
        double-click showed Windows' "How do you want to open this file?" and the file "didn't open in a window",
        though the task said Notepad.)"""
        import shutil
        app = (app or "").strip().strip('"')
        if app:
            exe = app if app.lower().endswith(".exe") else app + ".exe"
            found = shutil.which(exe) or shutil.which(app) or (app if os.path.isfile(app) else None)
            if found:
                return [found, path]
            raise actions.ActionFailed(f"there's no app called {app!r} on this computer")
        if cls._has_app(path):
            return None
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
            head.decode("utf-8")
            text = b"\x00" not in head
        except (OSError, UnicodeDecodeError):
            text = False
        if text:
            return [shutil.which("notepad.exe") or "notepad.exe", path]
        raise actions.ActionFailed(f"{os.path.basename(path)} has no app to open it on this computer")

    @staticmethod
    def _has_app(path: str) -> bool:
        """Whether Windows has an app for this kind of file (FindExecutable: 31 means none)."""
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(520)
            return ctypes.windll.shell32.FindExecutableW(path, None, buf) > 32
        except Exception:
            return True

    def _open_with_its_app(self, path: str, h, app: str = "") -> str:
        """Opens the file in its usual app (Notepad for a .txt), as a double-click would, and takes the window that
        shows it as the task's own. For when the task has no app window to press Ctrl+O in (2026-10-11: the model
        had closed Notepad, and open_file refused again and again)."""
        import uiautomation as auto
        name = os.path.basename(path).lower()
        stem = name.rsplit(".", 1)[0]
        cmd = self._opener(path, app)
        if cmd is None:
            os.startfile(path)
        else:
            subprocess.Popen(cmd, close_fds=True)
        end = time.time() + 10
        while time.time() < end:
            time.sleep(0.4)
            # Any top-level window showing the file, not just the one in front: the tool runs in the background, so
            # the window it starts opens behind (2026-10-11: Notepad opened behind and "didn't open in a window")
            w = None
            for cand in [screen.foreground()] + list(auto.GetRootControl().GetChildren()):
                try:
                    title = (cand.Name or "").lower() if cand is not None else ""
                    if cand is not None and not self._is_dialog(cand) and (name in title or stem in title):
                        w = cand
                        break
                except Exception:
                    continue
            if w is not None:
                self.task.opened(w)
                self.window = w
                try:
                    actions.bring_to_front(w, h)
                except actions.ActionFailed:
                    pass
                return f"opened {path} in its app (a window of its own now)" + self._file_start(path)
        raise actions.ActionFailed(f"{os.path.basename(path)} didn't open in a window")

    # Whole words, buttons only: "browse" matched "Browser compatibility" and "upload" a link (2026-10-11 batch 2)
    FILE_BUTTON = re.compile(r"\b(no files? (chosen|selected)|choose (a |your )?(files?|images?|photos?|documents?)|"
                             r"select (a |your )?(files?|images?|photos?)|browse|upload|attach)\b", re.I)

    def _choose_on_page(self, path: str, win, h) -> str:
        """A web page that wants a file: the tool clicks the page's own file button ("Choose File", "Select a file",
        "Browse", "Upload"), then fills the Open window it shows. (2026-10-11: the model called open_file before
        clicking the button and got an error; the owner wants runs without errors.)"""
        view = screen.look(win, use_ocr=False, wait=False)
        button = next((i for i in view.items if not i.offscreen and i.kind in ("Button", "Edit")
                       and len(i.name or "") <= 80 and self.FILE_BUTTON.search(i.name or "")), None)
        if button is None:
            raise actions.ActionFailed("there's no file button (Choose file, Upload, Browse) on this page to use")
        actions.click(button, win, h)
        dialog = self._wait_dialog(self.OPEN_TITLES, 8.0)
        if dialog is None:
            raise actions.ActionFailed(f"clicking {button.name!r} didn't show an Open window")
        self._fill_open_dialog(dialog, path, h)
        self.window = self._current_window()
        return f"chose {path} with the page's {button.name!r} button"

    def _open_file(self, reply: Dict[str, Any], win, h) -> str:
        """Opens a file the whole way, done by the tool, not the model (2026-10-11: asked to open Story1.txt and read
        it, the model clicked Open four times, then closed Notepad before reading). In an app it presses Ctrl+O;
        an Open dialog already in front (a web page's "Choose file") is filled as it is. Reports how a text file
        starts, read from the disk (exact), so a question about it needs no reading of the screen."""
        path = self._find_file(reply.get("path") or reply.get("text") or "")
        if h.dry_run:
            return f"would open {path}"
        front = self._current_window()
        if self._is_dialog(front) and (front.Name or "").strip().lower().startswith(self.OPEN_TITLES):
            self._fill_open_dialog(front, path, h)            # a web page's file picker (or one the app opened)
            self.window = self._current_window()
            return f"chose {path} in the Open window"
        if win is None or not self._may_touch(win) or not self.task.owns_window(win):
            return self._open_with_its_app(path, h, str(reply.get("app") or ""))   # no app window of its own
        if self._is_browser(win):
            return self._choose_on_page(path, win, h)
        actions.press("ctrl+o", win, h)
        dialog = self._wait_dialog(self.OPEN_TITLES, 8.0)
        if dialog is None:
            raise actions.ActionFailed("the Open window didn't appear after ctrl+o")
        self._fill_open_dialog(dialog, path, h)
        name = os.path.basename(path).lower()
        end = time.time() + 6
        while time.time() < end:
            w = self._current_window()
            if name in (w.Name or "").lower() or name.rsplit(".", 1)[0] in (w.Name or "").lower():
                self.window = w
                break
            time.sleep(0.25)
        else:
            raise actions.ActionFailed(f"{os.path.basename(path)} didn't open (the window title doesn't show it)")
        return f"opened {path}" + self._file_start(path)

    def _save_as(self, reply: Dict[str, Any], win, h) -> str:
        """Saves the task's own document under a full path, the whole Save dialog done by the tool, not the model
        (2026-10-10, the owner: "this is supposed to be the easiest test". Three runs in a row the model, driving
        the dialog from a list of controls, typed the name into the Search box, closed the dialog with alt+f4,
        typed the path over the story, and approved Save under the wrong name.) Asks the owner before replacing a
        file, and checks the file on disk afterwards."""
        path = str(reply.get("path") or reply.get("text") or "").strip().strip('"')
        if not path:
            raise actions.ActionFailed("save_as needs the full path to save to")
        if not os.path.isabs(path):
            place = "Desktop" if re.search(r"\bdesktop\b", self.goal, re.I) else "Documents"
            path = os.path.join(goals.known_folder(place), path)
        if win is None or not self._may_touch(win):
            raise actions.ActionFailed("save_as saves the app window you typed in; that window isn't in front")
        tasks.check_may_type_here(self.task, win)
        if h.dry_run:
            return f"would save as {path}"
        actions.press("ctrl+shift+s", win, h)
        dialog = self._wait_dialog(("save as",), 8.0)
        if dialog is None:
            raise actions.ActionFailed("the Save as window didn't open after ctrl+shift+s")
        view = screen.look(dialog, use_ocr=False, wait=False)
        box = next((i for i in view.items if i.kind in TYPEABLE and (i.name or "").lower().startswith("file name")),
                   None)
        if box is None:
            h.press("esc")
            raise actions.ActionFailed("the Save as window has no 'File name' box")
        actions.type_into(box, path, dialog, h)
        h.press("enter")
        confirm = self._wait_dialog(("confirm save as",), 2.5)
        if confirm is not None:
            cview = screen.look(confirm, use_ocr=False, wait=False)
            yes = next((i for i in cview.items if i.kind == "Button"
                        and (i.name or "").replace("&", "").strip().lower() == "yes"), None)
            if yes is None:
                raise actions.ActionFailed("a 'Confirm Save As' box without a Yes button")
            self.window = confirm
            raise _NeedsApproval(yes, f"replacing the existing file {path}")
        deadline = time.time() + 4
        while time.time() < deadline:
            if os.path.isfile(path) and os.path.getmtime(path) >= getattr(self, "started_at", 0.0) - 5:
                self.window = self._current_window()
                return f"saved as {path} (checked on disk)"
            time.sleep(0.25)
        raise actions.ActionFailed(f"the Save dialog closed but {path} isn't on disk")

    def _still_open_problem(self) -> Optional[str]:
        """For a goal that says to close an app ("then close Notepad"): a window of that app the task opened, still
        there, means it isn't done. Read from Windows, not claimed (2026-10-11: "closed Notepad without changing
        anything" was accepted while the Notepad the task opened stood open)."""
        m = re.search(r"\bclose\s+(?:the\s+|my\s+)?([a-z][a-z0-9 ]{2,20}?)(?:\s+(?:window|app|without|and|then)\b|[.,;!]|$)",
                      (self.goal or "").lower())
        if not m or not self.task.windows:
            return None
        app = m.group(1).strip()
        try:
            import uiautomation as auto
            for w in auto.GetRootControl().GetChildren():
                if w.NativeWindowHandle in self.task.windows and app.split()[0] in (w.Name or "").lower():
                    return f"{w.Name!r} (a window this task opened) is still open, and the goal says to close {app}"
        except Exception:
            return None
        return None

    def _save_guard(self, reply: Dict[str, Any], view: screen.View, a: str) -> Optional[str]:
        """For a goal that saves a file by name ("save it as Story1.txt to the desktop"), two rules the model can't
        overrule (2026-10-10, task-099: it asked to approve "overwrite Story1" while the File name box still held
        Notepad's default name, the approval clicked Save under that name, then it closed Notepad with nothing saved
        and couldn't reopen it):
        - Save can't be clicked or approved until the "File name" box holds that name;
        - the app (not a dialog) can't be closed while that file isn't saved yet, read from the disk."""
        m = SAVE_NAME.search(self.goal or "")
        if not m:
            return None
        name = m.group(1).strip()
        item = None
        try:
            item = next((i for i in view.items if i.id == int(reply.get("id"))), None)
        except (TypeError, ValueError):
            pass
        label = (item.name or "").replace("&", "").strip().lower() if item is not None else ""
        if a in ("click", "needs_approval") and label == "save":
            box = next((i for i in view.items if i.kind in TYPEABLE and (i.name or "").lower().startswith("file name")),
                       None)
            held = " ".join(self._field_text(box).split()) if box is not None else ""
            if box is not None and not held.lower().endswith((name.lower(), name.rsplit(".", 1)[0].lower())):
                place = "Desktop" if re.search(r"\bdesktop\b", self.goal, re.I) else "Documents"
                return (f"the 'File name' box holds {held[:60]!r}, not {name!r}: first type the full path "
                        f"({os.path.join(goals.known_folder(place), name)}) into it (item {box.id}) with submit true")
        keys = (reply.get("keys") or "").lower().replace(" ", "")
        closing = (a == "press" and keys in ("alt+f4", "ctrl+w")) or (a == "click" and label == "close")
        if closing and not self._is_dialog(self.window):
            f = goals.saved_file(self.goal, getattr(self, "started_at", 0.0))
            if f is not None and f["path"] is None:
                return (f"{name} isn't saved yet (checked on disk): save it first, or closing loses the typed text "
                        f"and the app can't be reopened in this run")
        return None
