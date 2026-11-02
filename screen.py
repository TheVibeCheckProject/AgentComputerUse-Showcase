"""The eyes: what's in front, as one numbered list.

Two reads, merged every time:
- Windows' accessibility list (UI Automation) of the window in front: web pages in Edge and every app. It
  covers whole pages, including what's below the visible part (marked off-screen).
- The OCR pass over that window's screenshot. OCR text inside an accessibility item is a duplicate and
  dropped; text inside an item that has no name names it; anything else is added as on-screen text that can
  be clicked. When they disagree, what's on screen wins.
"""
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

Rect = Tuple[int, int, int, int]


def init_dpi_awareness() -> bool:
    """Initializes Windows Per-Monitor V2 DPI awareness at process startup.

    Prevents coordinate scaling virtualization across 100%, 125%, 150%, and 4K displays.
    """
    try:
        import ctypes
        from ctypes import wintypes
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = wintypes.HANDLE(-4)
        user32 = ctypes.windll.user32
        res = user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        return bool(res)
    except Exception:
        return False


# Initialize DPI awareness at process startup
init_dpi_awareness()

# What can be acted on, and what's worth reading
ACTIONABLE = {"Button", "Hyperlink", "Edit", "CheckBox", "RadioButton", "ComboBox", "ListItem", "MenuItem",
              "TabItem", "TreeItem", "Slider", "SplitButton", "MenuBar", "Spinner", "DataItem"}
READABLE = {"Text", "Header", "HeaderItem", "Image", "Document"}
MAX_ITEMS = 300                 # a huge page lists its visible part first, then the rest up to this
READ_BUDGET = 4000              # elements visited per read (a 2,000-link page took 15 s to walk in full)
READY_SAMPLE = 600              # enough elements to tell "filled in and steady"
EDGE_ADDRESS_BAR = "Address and search bar"



@dataclass
class Item:
    id: int
    kind: str                   # Button, Hyperlink, Edit, ... or Text (OCR-only)
    name: str
    rect: Rect
    offscreen: bool = False
    source: str = "uia"         # uia | ocr | uia+ocr (the name came from OCR)
    value: Optional[str] = None # an edit box's current text
    focused: bool = False
    control: object = field(default=None, repr=False, compare=False)   # the live element (same process only)

    def center(self) -> Tuple[int, int]:
        return (self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2

    def describe(self) -> str:
        bits = [f"[{self.id}] {self.kind}", repr(self.name) if self.name else "(no label)"]
        if self.value:
            bits.append(f"= {self.value[:60]!r}")
        if self.focused:
            bits.append("(has the keyboard)")
        if self.offscreen:
            bits.append("(below)" if self.rect[1] > 0 else "(off-screen)")
        if self.source == "ocr":
            bits.append("(seen on screen)")
        return " ".join(bits)

    def to_json(self) -> Dict:
        d = asdict(self)
        d.pop("control", None)
        return d


@dataclass
class View:
    window: str
    process: str
    rect: Rect
    url: Optional[str]
    items: List[Item]
    took: float = 0.0

    def text(self, limit: int = 120) -> str:
        """The list as the model reads it: the visible part first."""
        head = f"Window: {self.window!r}" + (f" | address: {self.url}" if self.url else "")
        visible = [i for i in self.items if not i.offscreen]
        below = [i for i in self.items if i.offscreen]
        lines = [head] + [i.describe() for i in visible[:limit]]
        room = max(0, limit - len(visible))
        if below:
            lines.append(f"-- further down the page ({len(below)} more) --")
            lines += [i.describe() for i in below[:room]]
        return "\n".join(lines)

    def to_json(self) -> Dict:
        return {"window": self.window, "process": self.process, "rect": self.rect, "url": self.url,
                "took": round(self.took, 2), "items": [i.to_json() for i in self.items]}


# ---------------------------------------------------------------------------------------------- reading
def _rect(c) -> Optional[Rect]:
    r = c.BoundingRectangle
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return (r.left, r.top, r.right, r.bottom)


def foreground():
    import uiautomation as auto
    ctrl = auto.GetForegroundControl()
    return ctrl.GetTopLevelControl() if ctrl is not None else None


def wait_ready(win, timeout: float = 8.0, min_nodes: int = 5) -> int:
    """Waits until the window's list is filled and unchanged over two reads (Chrome-engine pages switch
    on their accessibility once something first looks, about 2 s). Never a fixed sleep."""
    import uiautomation as auto
    end = time.time() + timeout
    prev = -1
    while True:
        n = 0
        try:
            for _ in auto.WalkControl(win, maxDepth=60):
                n += 1
                if n >= READY_SAMPLE:
                    break
        except Exception:
            n = -2                                     # items vanished mid-read: still filling in
        if n == prev and n >= min_nodes:
            return n
        if time.time() > end:
            return max(n, 0)
        prev = n
        time.sleep(0.6)


def compute_frame_diff(img1, img2, size=(160, 90)) -> float:
    """Computes normalized perceptual pixel difference between two PIL images in [0.0, 1.0]."""
    from PIL import ImageChops, ImageStat
    if img1 is None or img2 is None:
        return 1.0
    t1 = img1.convert("L").resize(size)
    t2 = img2.convert("L").resize(size)
    diff = ImageChops.difference(t1, t2)
    stat = ImageStat.Stat(diff)
    return (stat.mean[0] if stat.mean else 0.0) / 255.0


def wait_settled(win=None, timeout: float = 2.5, poll_interval: float = 0.20,
                 threshold: float = 0.012) -> float:
    """Waits until visual activity (CSS animations, skeleton loaders, spinners) subsides.

    Compares consecutive perceptual frames at poll_interval. Returns the final delta.
    Gracefully returns 0.0 if screenshot capture is unsupported or headless.
    """
    wr = _rect(win) if win is not None else None
    try:
        import ocr
        f0, _ = ocr.grab(wr)
    except Exception:
        return 0.0

    end = time.time() + timeout
    last_delta = 0.0
    while time.time() < end:
        time.sleep(poll_interval)
        try:
            f1, _ = ocr.grab(wr)
            delta = compute_frame_diff(f0, f1)
            last_delta = delta
            if delta < threshold:
                return delta
            f0 = f1
        except Exception:
            break
    return last_delta


def read_accessibility(win) -> Tuple[List[Item], Optional[str]]:
    import uiautomation as auto
    wr = _rect(win)
    items: List[Item] = []
    url = None
    try:
        focused = auto.GetFocusedControl()
        focused_id = focused.GetRuntimeId() if focused is not None else None
    except Exception:
        focused_id = None
    visited = 0
    for c, depth in auto.WalkControl(win, maxDepth=60):
        visited += 1
        if visited > READ_BUDGET:
            break
        try:
            kind = c.ControlTypeName.replace("Control", "")
            if kind not in ACTIONABLE and kind not in READABLE:
                continue
            rect = _rect(c)
            if rect is None:
                continue
            name = (c.Name or "").replace("​", "").strip()
            value = None
            if kind == "Edit":
                try:
                    value = c.GetValuePattern().Value
                except Exception:
                    value = None
                if name == EDGE_ADDRESS_BAR:
                    url = value
            if kind in READABLE and not name:
                continue
            offscreen = bool(c.IsOffscreen)
            is_focused = focused_id is not None and c.GetRuntimeId() == focused_id
            items.append(Item(0, kind, name, rect, offscreen, "uia", value, is_focused, c))
        except Exception:
            continue                                   # an element that vanished while reading
    # outside the window entirely (and not marked off-screen) is noise
    if wr:
        items = [i for i in items if i.offscreen or _overlaps(i.rect, wr)]
    return items, url


def process_name(pid: int) -> str:
    """The program's file name (msedge.exe, notepad.exe) from Windows."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        k32.CloseHandle(handle)


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _inside(point: Tuple[int, int], r: Rect) -> bool:
    return r[0] <= point[0] <= r[2] and r[1] <= point[1] <= r[3]


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# ---------------------------------------------------------------------------------------------- merging
def merge(items: List[Item], lines: list) -> List[Item]:
    """Adds the OCR pass to the accessibility list.

    - OCR text inside an item whose name already contains it: a duplicate, dropped.
    - OCR text inside an item with no name: names that item (source uia+ocr).
    - OCR text inside a named item that says something else: the screen wins for what it shows; the item
      keeps its name, the text is added as its own on-screen line.
    - OCR text in no item: added as Text, clickable at its box (source ocr).
    """
    out = list(items)
    visible = [i for i in items if not i.offscreen]
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        c = ((line.rect[0] + line.rect[2]) // 2, (line.rect[1] + line.rect[3]) // 2)
        holders = [i for i in visible if _inside(c, i.rect)]
        # the smallest item holding it is the one it belongs to
        holders.sort(key=lambda i: (i.rect[2] - i.rect[0]) * (i.rect[3] - i.rect[1]))
        named = [i for i in holders if i.name]
        if any(_norm(text) in _norm(i.name) or _norm(i.name) in _norm(text) for i in named if _norm(i.name)):
            continue
        if any(i.kind == "Edit" and i.value and _norm(text) in _norm(i.value) for i in holders):
            continue
        unnamed = [i for i in holders if not i.name and i.kind in ACTIONABLE]
        if unnamed and (not named or _area(unnamed[0]) <= _area(named[0])):
            unnamed[0].name = text
            unnamed[0].source = "uia+ocr"
            continue
        out.append(Item(0, "Text", text, line.rect, False, "ocr"))
    return out


def _area(i: Item) -> int:
    return (i.rect[2] - i.rect[0]) * (i.rect[3] - i.rect[1])


def number(items: List[Item]) -> List[Item]:
    """Visible items first (top to bottom, left to right), then what's further down; capped."""
    visible = sorted([i for i in items if not i.offscreen], key=lambda i: (i.rect[1] // 8, i.rect[0]))
    below = sorted([i for i in items if i.offscreen], key=lambda i: (i.rect[1], i.rect[0]))
    ordered = (visible + below)[:MAX_ITEMS]
    for n, i in enumerate(ordered, 1):
        i.id = n
    return ordered


def look(win=None, use_ocr: bool = True, wait: bool = True) -> View:
    """One look at the window in front (or the given one): accessibility + OCR, merged and numbered."""
    t = time.time()
    win = win or foreground()
    if win is None:
        raise RuntimeError("no window is in front")
    if wait:
        wait_ready(win)
        wait_settled(win)
    items, url = read_accessibility(win)
    wr = _rect(win)
    if use_ocr and wr:
        import ocr
        items = merge(items, ocr.read(wr))
    process = process_name(win.ProcessId)
    return View((win.Name or "").replace("​", ""), process, wr or (0, 0, 0, 0), url, number(items),
                time.time() - t)
