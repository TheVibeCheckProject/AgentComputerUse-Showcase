"""Actions with their checks: what makes the real mouse and keyboard reliable.

- click: scroll the item into view if it's further down, make sure nothing covers it, then click.
- type: click the field, confirm it has the keyboard (else deal with the pop-up holding it), type real
  keystrokes, read the field back, retry once.
- open_url / launch_app: through the keyboard, the way a person does (Ctrl+L in the browser; the Start menu).
Pop-ups with a harmless answer are answered; others raise NeedsDecision for the model or the owner.
"""
import difflib
import math
import re
import time
from typing import Callable, List, Optional

import popups
import screen
from hands import Hands, Stopped


class NeedsDecision(Exception):
    """Something the tool won't decide alone: an unknown or risky pop-up."""

    def __init__(self, popup: popups.Popup, why: str):
        super().__init__(why)
        self.popup = popup


class ActionFailed(Exception):
    pass


# Fields whose label can change while it's the same field (a placeholder replaced by what was typed, a rebuilt
# address bar): these may be found again by a similar label or by position.
FIELD_KINDS = {"Edit", "ComboBox", "Document", "Spinner"}
ANCHOR_RADIUS = 35.0


def _words(s: str) -> List[str]:
    return re.findall(r"\w+", (s or "").lower())


def _same_words_or_more(old: str, new: str) -> bool:
    """One label is the other with words added at the end ('Search Google' -> 'Search Google or type').
    Whole words only, so 'Follow' is never 'Unfollow' and 'Subscribe' is never 'Unsubscribe'."""
    a, b = _words(old), _words(new)
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return long_[:len(short)] == short


def find_matching_item(item: screen.Item, items: List[screen.Item]) -> Optional[screen.Item]:
    """The same item again after the screen was rebuilt, or None (then the caller looks again and re-plans).

    1. Same kind and label (case and spacing ignored); nearest wins.
    2. A label that's the same words with more added, only if just one does (and, for buttons and links, only
       within 70 px of where it was).
       2026-10-08: a fuzzy match turned a vanished "Follow" into "Unfollow", and a 35 px position match turned
       "Archive" into the "Delete" next to it. Clicking the wrong thing is worse than looking again.
    3. Fields only (FIELD_KINDS): a similar label (>= 0.80), then the one field of that kind within 35 px.
       An unlabelled item of any kind may also be found by position, again only if just one is there.
    """
    if not items:
        return None
    cx, cy = item.center()

    def dist(i: screen.Item) -> float:
        return math.hypot(i.center()[0] - cx, i.center()[1] - cy)

    norm = lambda s: " ".join((s or "").lower().split())
    same_kind = [i for i in items if i.kind == item.kind]

    exact = [i for i in same_kind if norm(item.name) and norm(i.name) == norm(item.name)]
    if exact:
        return min(exact, key=dist)

    # A rebuilt button stays where it was; a "Delete account" across the page is not the vanished "Delete".
    extended = [i for i in same_kind if _same_words_or_more(item.name, i.name)
                and (item.kind in FIELD_KINDS or dist(i) <= 2 * ANCHOR_RADIUS)]
    if len(extended) == 1:
        return extended[0]

    if item.kind in FIELD_KINDS:
        def sim(i: screen.Item) -> float:
            return difflib.SequenceMatcher(None, norm(i.name), norm(item.name)).ratio()
        fuzzy = [i for i in same_kind if sim(i) >= 0.80]
        if fuzzy:
            return min(fuzzy, key=lambda i: (-sim(i), dist(i)))

    if item.kind in FIELD_KINDS or not (item.name or "").strip():
        nearby = [i for i in same_kind if dist(i) <= ANCHOR_RADIUS]
        if len(nearby) == 1:
            return nearby[0]

    return None


def refresh(item: screen.Item, win) -> screen.Item:
    """Finds the same item again using exact, fuzzy, or spatial anchor matching."""
    view = screen.look(win, use_ocr=False, wait=False)
    best = find_matching_item(item, view.items)
    if best is None:
        raise ActionFailed(f"{item.kind} {item.name!r} isn't on screen any more")
    item.control, item.rect, item.offscreen = best.control, best.rect, best.offscreen
    item.name = best.name
    return item


def alive(item: screen.Item) -> bool:
    try:
        item.control.BoundingRectangle
        item.control.GetRuntimeId()
        return True
    except Exception:
        return False


def _foreground_hwnd() -> int:
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow()


def _owner_chain(hwnd: int, depth: int = 6):
    """The window and the windows that own it (a Save dialog is owned by its program's window)."""
    import ctypes
    user32 = ctypes.windll.user32
    out = []
    for _ in range(depth):
        if not hwnd:
            break
        out.append(hwnd)
        hwnd = user32.GetWindow(hwnd, 4)                  # GW_OWNER
    return out


def dialog_of(win):
    """A dialog of this window that's in front (Save As, Open, a confirmation), or None."""
    import uiautomation as auto
    fg = _foreground_hwnd()
    chain = _owner_chain(fg)
    if win.NativeWindowHandle in chain[1:]:
        return auto.ControlFromHandle(fg)
    return None


def window_in_front(win) -> bool:
    """The window, or a dialog it owns, is in front. (2026-10-05: with Notepad's Save dialog open, the tool
    thought Notepad wasn't in front and clicked its title bar behind the dialog.)"""
    fg = _foreground_hwnd()
    return win.NativeWindowHandle in _owner_chain(fg) or popups.same(screen.foreground(), win)


def _restore_if_minimized(hwnd) -> None:
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindowAsync(hwnd, 9)                 # SW_RESTORE: a minimized window has no title bar
            time.sleep(0.3)
    except Exception:
        pass


VK_F24 = 0x87                       # a key no program uses: the "last input" Windows checks before handing focus


def _ask_for_foreground(hwnd) -> None:
    """Puts the window in front. The tool runs in the background (the agent starts it), and Windows only lets the
    process that sent the LAST input take the foreground, so a plain SetForegroundWindow is refused silently
    (2026-10-11: its Edge stayed behind the owner's maximized Pinterest window for a whole run). So: send one
    harmless key (F24) first, then ask; also raise it in the z-order (topmost on and off) and share input with the
    window in front. Checked by the caller."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.GetForegroundWindow() == hwnd:
            return
        user32.keybd_event(VK_F24, 0, 0, 0)
        user32.keybd_event(VK_F24, 0, 2, 0)                   # KEYEVENTF_KEYUP
        user32.ShowWindow(hwnd, 5)                             # SW_SHOW
        flags = 0x0001 | 0x0002 | 0x0040                       # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)       # HWND_TOPMOST...
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)       # ...then back to normal: now on top of the rest
        user32.BringWindowToTop(hwnd)
        if user32.SetForegroundWindow(hwnd):
            return
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_thread = user32.GetCurrentThreadId()
        if fg_thread and fg_thread != cur_thread and user32.AttachThreadInput(cur_thread, fg_thread, True):
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                user32.AttachThreadInput(cur_thread, fg_thread, False)
    except Exception:
        pass


def _point_is_on(hwnd: int, x: int, y: int) -> bool:
    """The screen point shows this window (or a child of it), not one covering it."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        at = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        return bool(at) and user32.GetAncestor(at, 2) == hwnd          # GA_ROOT
    except Exception:
        return False


def minimize_all() -> None:
    """Every window minimized (the taskbar's "minimize all"), so nothing of the owner's covers the task's own
    windows (2026-10-11, the owner: "it needs to minimize everything and open its own window every time it runs a
    new task"). Works from a background process: it's the taskbar doing it."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if tray:
            user32.PostMessageW(tray, 0x0111, 419, 0)          # WM_COMMAND, "minimize all"
            time.sleep(0.8)
    except Exception:
        pass


def bring_to_front(win, hands: Hands) -> None:
    """Brings the window in front: restore it if minimized; if neither it nor one of its own dialogs is in front,
    ask Windows for the focus, then click its title bar. Never forces the window over its own dialog (the
    in-front check comes first)."""
    hwnd = getattr(win, "NativeWindowHandle", None)
    if hwnd and not hands.dry_run:
        _restore_if_minimized(hwnd)
    if window_in_front(win):
        return
    if hwnd and not hands.dry_run:
        _ask_for_foreground(hwnd)
        time.sleep(0.2)
        if window_in_front(win):
            return
    r = win.BoundingRectangle
    x, y = r.left + min(200, r.width() // 3), r.top + 12
    # The title-bar click only when that spot really shows this window: with another window over it, the click
    # landed on the owner's window (2026-10-11, on their Pinterest tab, again and again)
    if hands.dry_run or (hwnd and _point_is_on(hwnd, x, y)):
        hands.click(x, y)
        time.sleep(0.4)
    if not hands.dry_run and not window_in_front(win):
        raise ActionFailed("another window is covering the task's own window and Windows won't bring it forward")



def answer_popup(popup: popups.Popup, hands: Hands) -> str:
    """Clicks the harmless answer; raises NeedsDecision when there isn't one."""
    if popup.risky:
        raise NeedsDecision(popup, "a pop-up that needs the owner: " + popup.text[:120])
    choice = popups.safe_choice(popup.choices, popup.text)
    if choice is None:
        raise NeedsDecision(popup, "a pop-up without a harmless answer: " + popup.text[:120])
    ctrl = popup.controls[popup.choices.index(choice)]
    r = ctrl.BoundingRectangle
    hands.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    time.sleep(0.8)
    return choice


def scroll_into_view(item: screen.Item, win, hands: Hands, max_turns: int = 40) -> None:
    """The real wheel over the window until the item is on screen (only the item is re-checked)."""
    ctrl = item.control
    if ctrl is None:
        raise ActionFailed("can't track that item to scroll to it")
    r = win.BoundingRectangle
    spot = ((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    top_margin = max(110, int(r.height() * 0.18))
    bottom_margin = max(80, int(r.height() * 0.14))
    for _ in range(max_turns):
        cr = ctrl.BoundingRectangle
        if not ctrl.IsOffscreen and r.top + top_margin < cr.top < r.bottom - bottom_margin:
            item.rect = (cr.left, cr.top, cr.right, cr.bottom)
            item.offscreen = False
            return
        hands.scroll(spot[0], spot[1], -3 if cr.top >= r.bottom - bottom_margin else 3)
        time.sleep(0.15)
        if hands.dry_run:
            return
    raise ActionFailed(f"couldn't scroll {item.name!r} into view")


def click(item: screen.Item, win, hands: Hands, popup_rounds: int = 2) -> Optional[str]:
    """Clicks an item; returns the pop-up answer it gave on the way, if any."""
    bring_to_front(win, hands)
    if item.offscreen:
        scroll_into_view(item, win, hands)
    answered = None
    if item.control is not None and not hands.dry_run:
        if not alive(item):
            refresh(item, win)
        for _ in range(popup_rounds):
            try:
                in_the_way = popups.covered(item.control)
            except Exception:
                refresh(item, win)
                continue
            if in_the_way is None:
                break
            p = popups.find(win, item.control)
            if p is None:
                break                                    # something odd, but no pop-up: click anyway
            answered = answer_popup(p, hands)
        try:
            cr = item.control.BoundingRectangle
        except Exception:
            cr = refresh(item, win).control.BoundingRectangle
        item.rect = (cr.left, cr.top, cr.right, cr.bottom)
    # One click, never a second "in case it didn't take". 2026-10-08: pressing Enter on a button that looked the
    # same 0.1 s later would send "Send" or "Place order" twice (most buttons don't change that fast). Whether a
    # click worked is judged on the next look, by the step's "expect".
    hands.click(*item.center())
    return answered


def _field_has_keyboard(item: screen.Item) -> bool:
    import uiautomation as auto
    try:
        focused = auto.GetFocusedControl()
    except Exception:
        return False
    node = focused
    for _ in range(4):                                    # the field itself or a part inside it
        if node is None:
            return False
        if popups.same(node, item.control):
            return True
        try:
            node = node.GetParentControl()
        except Exception:
            return False
    # the field may have been rebuilt when clicked (Edge's address bar is): same kind and name, same place
    try:
        r = focused.BoundingRectangle
        kind = focused.ControlTypeName.replace("Control", "")
        name = (focused.Name or "").replace("\u200b", "").strip()
        cx, cy = item.center()
        return kind == item.kind and name == item.name and r.left <= cx <= r.right and r.top - 40 <= cy <= r.bottom + 40
    except Exception:
        return False


def read_value(item: screen.Item) -> Optional[str]:
    """A field's text: the Value pattern (edit boxes), else the Text pattern (documents: Windows 11 Notepad's text
    area has no Value). None when neither can be read. (2026-10-08: Notepad's text was never read back, so typing
    there went unchecked and the task double-check saw an empty field.)"""
    control = getattr(item, "control", None)
    if control is None:
        return None
    try:
        return control.GetValuePattern().Value
    except Exception:
        pass
    try:
        return control.GetTextPattern().DocumentRange.GetText(-1)
    except Exception:
        return None


def _same_address(typed: str, shown: str) -> bool:
    """The address bar shows what was typed plus Edge's autocomplete (the rest of a suggested address, selected),
    and may drop "https://" or change case in the host: it counts if it starts with what was typed."""
    def norm(s: str) -> str:
        s = (s or "").strip().lower()
        for p in ("https://", "http://", "www."):
            if s.startswith(p):
                s = s[len(p):]
        return s
    t, s = norm(typed), norm(shown)
    return bool(t) and s.startswith(t)


# Told whenever typed text didn't read back right and is about to be cleared and typed again (the loop logs it and
# shows it in the narration). 2026-10-10, the owner: "the story gets typed, then the box is suddenly empty and it
# starts again... something we aren't seeing and it's not getting logged".
ON_RETYPE: Optional[Callable[[str], None]] = None
# Takes a labelled screenshot (the loop's StepLog.snapshot) when typed text read back wrong, just before it's
# cleared: the one moment worth a picture. (The screenshots all through typing a long text, added the same day to
# find the retype, were removed at the owner's word: "I don't want it to constantly take screenshots".)
ON_SNAPSHOT: Optional[Callable[[str], None]] = None


def _snap(label: str) -> None:
    if ON_SNAPSHOT is not None:
        try:
            ON_SNAPSHOT(label)
        except Exception:
            pass


def mismatch(expected: str, got: str) -> str:
    """Where typed text and the field differ, in words: lengths, the first differing character, both around it."""
    e, g = _norm_text(expected), _norm_text(got)
    i = next((k for k, (a, b) in enumerate(zip(e, g)) if a != b), min(len(e), len(g)))
    return (f"the field held {len(g)} of {len(e)} characters; first difference at character {i + 1}: expected "
            f"{e[max(0, i - 12):i + 18]!r}, found {g[max(0, i - 12):i + 18]!r}")


def _norm_text(s: str) -> str:
    """Line endings aside (Notepad's Text pattern gives \\r for a typed newline)."""
    return (s or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _settled_value(item: screen.Item, win, text: str, wait: float = 4.0) -> Optional[str]:
    """The field's text once typing has landed: while it's still the start of what was typed (keystrokes still
    arriving, as with a long paragraph), it's read again until it stops growing or `wait` runs out. (2026-10-10:
    read 0.3 s after typing a 983-character story, the field looked incomplete, so the whole story was deleted and
    typed again: the owner watched it type twice.)"""
    target = _norm_text(text)
    deadline = time.time() + wait
    last = None
    while True:
        time.sleep(0.3)
        if not alive(item):
            refresh(item, win)                            # rebuilt while typing: read the new one
        now = read_value(item)
        if now is None:
            return None
        n = _norm_text(now)
        if n == target or not target.startswith(n) or now == last or time.time() >= deadline:
            return now
        last = now


def _focus_directly(item: screen.Item) -> Optional[screen.Item]:
    """When a click didn't give the field the keyboard (it landed beside the input, or the layout moved it), Windows
    is asked to put the keyboard in the field, or in the text box inside it. Typing then still uses the real
    keyboard. Returns the item that has the keyboard, or None. (2026-10-11: Wikipedia's search, four failed tries.)"""
    import uiautomation as auto
    ctrl = getattr(item, "control", None)
    if ctrl is None:
        return None
    try:
        ctrl.SetFocus()
        time.sleep(0.2)
        if _field_has_keyboard(item):
            return item
        for c, _ in auto.WalkControl(ctrl, maxDepth=4):
            if c.ControlTypeName in ("EditControl", "ComboBoxControl"):
                c.SetFocus()
                time.sleep(0.2)
                r = c.BoundingRectangle
                inner = screen.Item(id=item.id, kind=c.ControlTypeName.replace("Control", ""),
                                    name=(c.Name or item.name or "").strip(), rect=(r.left, r.top, r.right, r.bottom),
                                    control=c)
                if _field_has_keyboard(inner):
                    return inner
    except Exception:
        return None
    return None


def _describe_focus() -> str:
    """What has the keyboard, for the log: kind, name, place (2026-10-11: Wikipedia's search kept failing and the
    log couldn't say where the keyboard was instead)."""
    import uiautomation as auto
    try:
        f = auto.GetFocusedControl()
        r = f.BoundingRectangle
        top = f.GetTopLevelControl()
        return (f"{f.ControlTypeName.replace('Control', '')} {(f.Name or '')[:40]!r} at {(r.left, r.top, r.right, r.bottom)} "
                f"in {((top.Name or '') if top else '')[:40]!r}")
    except Exception as e:
        return f"unknown ({type(e).__name__})"


def _text_field_with_keyboard(win) -> Optional[screen.Item]:
    """The text box that has the keyboard now, when it's in this window (not the address bar). Many sites' search
    boxes open or move to ANOTHER box when clicked (Wikipedia's search overlay, ssa.gov's): typing must follow the
    keyboard there. (2026-10-11 batch: two tasks failed with "the keyboard isn't on 'Search ...'".)"""
    import uiautomation as auto
    try:
        f = auto.GetFocusedControl()
        if f is None or f.ControlTypeName not in ("EditControl", "ComboBoxControl"):
            return None
        top = f.GetTopLevelControl()
        if top is None or top.NativeWindowHandle != win.NativeWindowHandle:
            return None
        name = (f.Name or "").replace("\u200b", "").strip()
        if name == screen.EDGE_ADDRESS_BAR:
            return None
        r = f.BoundingRectangle
        if r.width() <= 0 or r.height() <= 0:
            return None
        return screen.Item(id=-1, kind=f.ControlTypeName.replace("Control", ""), name=name,
                           rect=(r.left, r.top, r.right, r.bottom), control=f)
    except Exception:
        return None


def type_into(item: screen.Item, text: str, win, hands: Hands, secret: bool = False, address: bool = False) -> None:
    """Never types blind: the field must have the keyboard. Reads the field back (not for secrets, which
    only check that something is there; an address only has to start the same, see _same_address)."""
    for attempt in range(1, 4):
        click(item, win, hands)
        time.sleep(0.3)
        if hands.dry_run:
            hands.type_text(text)
            return
        if window_in_front(win) and not _field_has_keyboard(item):
            direct = _focus_directly(item)
            if direct is not None:
                item = direct                             # the field (or the text box inside it) has the keyboard now
        if not window_in_front(win) or not _field_has_keyboard(item):
            moved = None if secret or not window_in_front(win) else _text_field_with_keyboard(win)
            if moved is not None:
                item = moved                              # the box the click opened: type there
            else:
                p = popups.find(win, item.control)
                if p is None:
                    if attempt == 3:
                        raise ActionFailed(f"the keyboard isn't on {item.name!r} and nothing visible is in the way "
                                           f"(it's on {_describe_focus()}; clicked at {item.center()})")
                    continue
                answer_popup(p, hands)
                continue
        if not alive(item):
            refresh(item, win)
        before = read_value(item) or ""
        if before:
            hands.press("ctrl+a")
            hands.press("delete")
        hands.type_text(text)
        after = _settled_value(item, win, text)
        if after is None:
            return                                        # a field that can't be read back (some apps)
        same = _norm_text(after) == _norm_text(text)
        if (secret and after) or same or (address and _same_address(text, after)):
            return
        if ON_RETYPE is not None and not secret:
            try:
                ON_RETYPE(f"attempt {attempt}: " + mismatch(text, after))
            except Exception:
                pass                                      # reporting never stops the typing
        if not secret:
            _snap(f"attempt {attempt}: read back wrong, about to clear and type again")
        if attempt < 3:
            hands.press("ctrl+a")
            hands.press("delete")
    raise ActionFailed(f"typing into {item.name!r} didn't stick")


def press(combo: str, win, hands: Hands) -> None:
    bring_to_front(win, hands)
    hands.press(combo)


def clear_popups(win, hands: Hands, rounds: int = 3) -> list:
    """Answers harmless pop-ups sitting on the window before a keyboard step; returns what it answered.
    (2026-10-05: Edge's sync pop-up held the keyboard, and the typed address went nowhere.)"""
    answered = []
    if hands.dry_run:
        return answered
    for _ in range(rounds):
        p = popups.find_any(win)
        if p is None:
            break
        answered.append(answer_popup(p, hands))
    return answered


def open_url(url: str, win, hands: Hands, timeout: float = 20.0) -> None:
    """In the browser: clear pop-ups, type the address into the address bar (careful typing: the keyboard
    must be on the bar), Enter, then confirm the page changed."""
    bring_to_front(win, hands)
    if hands.dry_run:
        hands.press("ctrl+l")
        hands.type_text(url)
        hands.press("enter")
        return
    clear_popups(win, hands)
    view = screen.look(win, use_ocr=False, wait=False)
    bar = next((i for i in view.items if i.kind == "Edit" and i.name == screen.EDGE_ADDRESS_BAR), None)
    if bar is None:
        raise ActionFailed("couldn't find the address bar")
    before_title = win.Name
    type_into(bar, url, win, hands, address=True)
    hands.press("delete")          # drops Edge's autocomplete (selected after the typed text): Enter goes where typed
    hands.press("enter")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        if win.Name != before_title:
            break
    else:
        raise ActionFailed(f"the page didn't change after going to {url}")
    screen.wait_ready(win, timeout=15)
    screen.wait_settled(win, timeout=3.0)
    clear_popups(win, hands)


START_PROCESSES = {"searchhost.exe", "startmenuexperiencehost.exe", "searchapp.exe"}


def _start_menu_open(wait: float) -> bool:
    end = time.time() + wait
    while time.time() < end:
        time.sleep(0.3)
        fg = screen.foreground()
        if fg is not None and screen.process_name(fg.ProcessId).lower() in START_PROCESSES:
            return True
    return False


def start_button():
    """The taskbar's Start button, from the accessibility list."""
    import uiautomation as auto
    for w in auto.GetRootControl().GetChildren():
        if w.ClassName == "Shell_TrayWnd":
            for c, d in auto.WalkControl(w, maxDepth=8):
                try:
                    if c.ControlTypeName == "ButtonControl" and (c.Name or "").strip() == "Start":
                        return c
                except Exception:
                    continue
    return None


def open_start_menu(hands: Hands) -> str:
    """Like a person whose Windows key may not work (the owner's laptop has it locked by ASUS Armoury
    Crate, and a pressed Windows key from the tool was blocked too): click the taskbar's Start button, then
    Ctrl+Esc, then the Windows key. Confirms the Start menu really opened each time."""
    btn = start_button()
    if btn is not None:
        r = btn.BoundingRectangle
        hands.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        if hands.dry_run or _start_menu_open(3):
            return "Start button"
    for combo in ("ctrl+esc", "win"):
        hands.press(combo)
        if hands.dry_run or _start_menu_open(3):
            return combo
    raise ActionFailed("the Start menu didn't open (Start button, Ctrl+Esc and the Windows key all tried)")


def _start_menu_in_front() -> bool:
    fg = screen.foreground()
    return fg is not None and screen.process_name(fg.ProcessId).lower() in START_PROCESSES


def _start_results_show(name: str, wait: float = 6.0, budget: int = 400) -> bool:
    """The Start menu's search lists a result named like the app, so Enter will open it. The search box itself
    holds the typed name, so edit boxes don't count."""
    import uiautomation as auto
    want = name.lower().replace(" ", "")
    end = time.time() + wait
    while time.time() < end:
        time.sleep(0.3)
        fg = screen.foreground()
        if fg is None:
            continue
        try:
            for n, (c, _) in enumerate(auto.WalkControl(fg, maxDepth=14)):
                if n > budget:
                    break
                if c.ControlTypeName != "EditControl" and want in (c.Name or "").lower().replace(" ", ""):
                    return True
        except Exception:
            continue
    return False


def _top_windows() -> list:
    try:
        import uiautomation as auto
        return list(auto.GetRootControl().GetChildren())
    except Exception:
        return []


def _is_app(w, want: str) -> bool:
    try:
        proc = screen.process_name(w.ProcessId).lower()
    except Exception:
        proc = ""
    return proc not in START_PROCESSES and (want in proc.replace(" ", "") or
                                             want in (w.Name or "").lower().replace(" ", ""))


def _app_in_front(name: str, timeout: float, before_handles: Optional[set] = None):
    """The app's window: in front (a new window, or an existing one that opened it in a new tab: Windows 11 Notepad
    does that), or a NEW window of the app anywhere, even behind the Start menu's search. Returns
    (window, in_front) or (None, False).

    2026-10-08 (fifth live run): Notepad opened behind the search, which stayed on top; waiting only for the front
    took 30 s, gave up, and the task never claimed its own Notepad (it was then 'not yours' to type in)."""
    want = name.lower().replace(" ", "")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        fg = screen.foreground()
        if fg is not None and _is_app(fg, want):
            return fg, True
        if before_handles is not None:
            for w in _top_windows():
                if getattr(w, "NativeWindowHandle", None) not in before_handles and _is_app(w, want):
                    return w, False
    return None, False


# A plain program name (notepad, calc, mspaint, "snipping tool"): never a path, switches or a second command.
APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}$")


def _start_directly(name: str) -> None:
    """Windows starts the program by name, the way Win+R does."""
    import os
    if not APP_NAME.match(name) or ".." in name:
        raise ActionFailed(f"won't start {name!r} directly: not a plain program name")
    try:
        os.startfile(name)
    except OSError as e:
        raise ActionFailed(f"Windows couldn't start {name!r}: {e}")


def launch_app(name: str, hands: Hands, timeout: float = 15.0):
    """The Start menu, like a person: open it (Start button first), confirm it opened (never type blind), type the
    name, wait until the search lists it, Enter. If the app still isn't in front, close the Start menu and have
    Windows start it by name. Returns the app's window once it's in front.

    2026-10-08 (first live run, monitor off): Enter went 1 s after typing, before the search had results, so
    Notepad never opened; the model then tried to click the result in the Start menu (not the task's window,
    refused) and the run went nowhere."""
    before = screen.foreground()
    before_handles = None if hands.dry_run else {w.NativeWindowHandle for w in _top_windows()}
    how = open_start_menu(hands)
    hands.log.append(f"Start menu opened with: {how}")
    if hands.dry_run:
        hands.type_text(name)
        hands.press("enter")
        return before
    hands.type_text(name)
    if not _start_results_show(name):
        time.sleep(1.0)                                   # couldn't read the results: the old fixed pause
    hands.press("enter")
    win, in_front = _app_in_front(name, timeout, before_handles)
    if win is None:
        # Enter opened nothing at all: close the Start menu, have Windows start it by name. Never when a window of
        # the app already appeared (that would open a second copy).
        if _start_menu_in_front():
            hands.press("esc")
            time.sleep(0.5)
        hands.log.append(f"the Start menu didn't open {name!r}: starting it by name")
        _start_directly(name)
        win, in_front = _app_in_front(name, timeout, before_handles)
        if win is None:
            raise ActionFailed(f"{name!r} didn't open")
    if not in_front:
        if _start_menu_in_front():
            hands.press("esc")                            # the search sat on top of the app's new window
            time.sleep(0.4)
        bring_to_front(win, hands)
    return win


__all__ = ["NeedsDecision", "ActionFailed", "Stopped", "click", "type_into", "press", "open_url", "launch_app",
           "answer_popup", "scroll_into_view"]
