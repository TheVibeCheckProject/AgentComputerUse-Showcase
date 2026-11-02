"""Read-only diagnosis (no mouse, no keyboard): open the tool's Edge, then do what open_url does before it
types, with full tracebacks."""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import browser  # noqa: E402
import popups  # noqa: E402
import screen  # noqa: E402

win = browser.open_edge("about:blank")
print("opened:", repr(win.Name), "handle", win.NativeWindowHandle)
try:
    for t in range(6):
        time.sleep(1)
        try:
            print(f"t={t + 1}s title={win.Name!r} handle={win.NativeWindowHandle}")
        except Exception as e:
            print(f"t={t + 1}s the window object failed: {type(e).__name__} {e}")
    for step in ("find_any", "look"):
        try:
            if step == "find_any":
                p = popups.find_any(win)
                print("find_any ->", None if p is None else (p.text[:80], p.choices))
            else:
                v = screen.look(win, use_ocr=False, wait=False)
                print("look ->", len(v.items), "items; address bar:",
                      [i.value for i in v.items if i.name == screen.EDGE_ADDRESS_BAR])
        except Exception:
            print(f"{step} FAILED:")
            traceback.print_exc()
finally:
    browser.close_window(win)
    print("closed")
