"""Pop-ups: see them, read them, answer them.

Tested 2026-10-05 in Edge: a pop-up held the keyboard, so typing went nowhere until it was answered. Here:
find what's in the way (the panel holding the keyboard, whatever covers the spot about to be clicked, or
another small window of the same program), read it (its choices from the accessibility list and its text by
OCR of just that area), and pick an answer. Harmless pop-ups get the dismissing answer; anything else is
left to the model, and risky ones to the owner.
"""
import re
from dataclasses import dataclass, field

PREFERRED = ["Got it", "No thanks", "No, thanks", "Not now", "Maybe later", "Skip", "Dismiss", "Reject all",
             "Reject non-essential", "Necessary only", "Only necessary", "Decline", "Close", "Continue", "OK", "Done"]
NEVER = re.compile(r"\b(allow|turn on|sign in|log ?in|accept all|accept|agree|subscribe|buy|purchase|install|pay|"
                   r"upgrade|enable|start trial|yes, continue)\b", re.IGNORECASE)
RISKY = re.compile(r"\b(permission|leave site|leave page|payment|card|delete|remove|password|location|camera|"
                   r"microphone|download)\b", re.IGNORECASE)
CLICKABLE = {"ButtonControl", "HyperlinkControl", "CheckBoxControl", "MenuItemControl", "ListItemControl",
             "RadioButtonControl"}
# Only a separate panel (a web view) or a dialog window counts as a pop-up, never a toolbar, pane or group:
# Edge's tab strip and title bar have buttons named "Close", which must never be taken for a pop-up's answer.
CONTAINERS = {"DocumentControl", "WindowControl"}


@dataclass
class Popup:
    rect: tuple
    text: str
    choices: list[str]
    controls: list = field(default_factory=list, repr=False)

    @property
    def risky(self) -> bool:
        return bool(RISKY.search(self.text) or any(RISKY.search(c) for c in self.choices))


def safe_choice(choices: list[str], text: str = "") -> str | None:
    """The harmless dismissing answer, if there is one and the pop-up isn't risky; else None."""
    if RISKY.search(text or ""):
        return None
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    for want in PREFERRED:
        for c in choices:
            name = c.strip()
            # exact matches only: "Continue to checkout" is not the harmless "Continue"
            if not NEVER.search(name) and norm(name) == norm(want):
                return name
    return None


# Windows' own shell: never a pop-up. The taskbar runs inside explorer.exe, so when the task's window was a File
# Explorer window, "a small window of the same program" found the taskbar (Start, Search, Widgets, the clock...)
# and the task stopped on "a pop-up without a harmless answer" (2026-10-06).
SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "NotifyIconOverflowWindow",
                 "TopLevelWindowForOverflowXamlIsland", "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"}


def is_shell(w) -> bool:
    try:
        return (w.ClassName or "") in SHELL_CLASSES
    except Exception:
        return False


def same(a, b) -> bool:
    try:
        return a is not None and b is not None and a.GetRuntimeId() == b.GetRuntimeId()
    except Exception:
        return False


def _choices_in(container) -> list:
    import uiautomation as auto
    out = []
    for c, d in auto.WalkControl(container, maxDepth=30):
        try:
            r = c.BoundingRectangle
            if c.ControlTypeName in CLICKABLE and (c.Name or "").strip() and r.right > r.left and not c.IsOffscreen:
                out.append(c)
        except Exception:
            continue
    return out


def _climb_to_popup(node, main_window) -> object | None:
    """From an element in the way, up to the smallest container with choices that isn't the whole window."""
    wr = main_window.BoundingRectangle if main_window is not None else None
    for _ in range(12):
        if node is None:
            return None
        try:
            if node.ControlTypeName in CONTAINERS and _choices_in(node):
                r = node.BoundingRectangle
                whole = wr is not None and r.width() >= wr.width() * 0.9 and r.height() >= wr.height() * 0.8
                if same(node, main_window) or whole:
                    return None                             # reached the page or the window: not a pop-up
                if _on_top(node):
                    return node
            node = node.GetParentControl()
        except Exception:
            return None
    return None


def covered(target) -> object | None:
    """None if the target's centre really is the target (or inside it); else the element in the way."""
    import uiautomation as auto
    r = target.BoundingRectangle
    hit = auto.ControlFromPoint((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    node = hit
    for _ in range(10):
        if node is None:
            break
        if same(node, target):
            return None
        try:
            node = node.GetParentControl()
        except Exception:
            break
    return hit


def _on_top(c) -> bool:
    """Windows' own answer: is this element what's actually showing at its centre?"""
    import uiautomation as auto
    try:
        r = c.BoundingRectangle
        hit = auto.ControlFromPoint((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    except Exception:
        return False
    node = hit
    for _ in range(15):
        if node is None:
            return False
        if same(node, c):
            return True
        try:
            node = node.GetParentControl()
        except Exception:
            return False
    return False


def find_any(main_window) -> Popup | None:
    """A pop-up on top of the window with no particular target: a panel or dialog that's smaller than the
    window, really showing on top, and has choices. (Edge's toolbar never counts: it's not a document or a
    dialog.)"""
    import uiautomation as auto
    wr = main_window.BoundingRectangle
    found = None
    # In a browser, a document inside the page is an embedded frame (part of the page), never a pop-up: the model
    # clicks what's on a page. Only windows count there (Edge's own prompts). 2026-10-11: W3Schools' demo frame
    # was taken for a pop-up and the owner was asked about it.
    try:
        import screen
        browser = screen.process_name(main_window.ProcessId).lower() in ("msedge.exe", "chrome.exe", "firefox.exe")
    except Exception:
        browser = False
    kinds = ("WindowControl",) if browser else ("DocumentControl", "WindowControl")
    for c, d in auto.WalkControl(main_window, maxDepth=20):
        try:
            if c.ControlTypeName not in kinds or c.IsOffscreen:
                continue
            r = c.BoundingRectangle
            if r.width() <= 0 or (r.width() >= wr.width() * 0.9 and r.height() >= wr.height() * 0.8):
                continue                                    # the page itself
            if browser and r.width() * r.height() > 0.4 * wr.width() * wr.height():
                continue        # a big part of the page (Edge's PDF viewer, 2026-10-11 batch), not a pop-up
            if _choices_in(c) and _on_top(c):
                found = c
                break
        except Exception:
            continue
    if found is None:
        try:                                                # a separate small window of the same program
            pid = main_window.ProcessId
            for w in auto.GetRootControl().GetChildren():
                if w.ProcessId == pid and not same(w, main_window) and not is_shell(w) \
                        and 50 < w.BoundingRectangle.width() \
                        and _choices_in(w):
                    found = w
                    break
        except Exception:
            pass
    return _read(found) if found is not None else None


def _read(box) -> Popup:
    r = box.BoundingRectangle
    rect = (r.left, r.top, r.right, r.bottom)
    try:
        import ocr
        text = " ".join(line.text for line in ocr.read(rect))
    except Exception:
        text = box.Name or ""
    controls = _choices_in(box)
    return Popup(rect, text, [(c.Name or "").strip() for c in controls], controls)


def find(main_window, target=None) -> Popup | None:
    """The pop-up in the way of `target` (or of typing in general), read: text by OCR, choices by name."""
    import uiautomation as auto
    candidates = []
    try:
        focused = auto.GetFocusedControl()
    except Exception:
        focused = None
    if focused is not None and (target is None or not same(focused, target)):
        p = _climb_to_popup(focused, main_window)
        if p is not None:
            candidates.append(p)
    if target is not None and not candidates:
        blocker = covered(target)
        if blocker is not None:
            p = _climb_to_popup(blocker, main_window)
            if p is not None:
                candidates.append(p)
    if not candidates and main_window is not None:
        # a small window of the same program (dialogs, bubbles)
        try:
            pid = main_window.ProcessId
            for w in auto.GetRootControl().GetChildren():
                if w.ProcessId == pid and not same(w, main_window) and not is_shell(w) \
                        and w.BoundingRectangle.width() > 50 \
                        and _choices_in(w):
                    candidates.append(w)
                    break
        except Exception:
            pass
    if not candidates:
        return find_any(main_window) if main_window is not None else None
    return _read(candidates[0])
