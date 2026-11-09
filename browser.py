"""The browser: a normal Edge, launched normally, with the tool's own profile.

No automation interface (no debugging port, no Playwright/Selenium): sites block browsers driven that way,
so Edge is driven only by the real mouse and keyboard. Its own profile keeps sessions (sign-ins last weeks)
and stays separate from the owner's personal browsing.
"""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE = Path(os.environ.get("ACU_EDGE_PROFILE") or (ROOT / "data" / "edge-profile"))
EDGE_PATHS = [Path(os.environ.get("ACU_EDGE", "")),
              Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
              Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
EDGE_CLASS = "Chrome_WidgetWin_1"          # Edge and Chrome share the engine and the window class


def edge_exe() -> Path:
    for p in EDGE_PATHS:
        if str(p) not in ("", ".") and p.is_file():
            return p
    raise FileNotFoundError("Microsoft Edge isn't installed where expected")


def launch_args(url: str = "about:blank", profile: Path = PROFILE) -> list:
    """Edge as a person would start it, with the tool's own profile; first-run and default-browser prompts
    skipped. Nothing here exposes an automation interface."""
    return [str(edge_exe()), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--new-window", "--start-maximized", url]


def _maximize(hwnd) -> None:
    """Full size, so sites use their normal desktop layout (2026-10-11: at ~920 px wide, Wikipedia's search box sat
    where a click hit empty page and typing failed four times)."""
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, 3)  # type: ignore[attr-defined]  # SW_MAXIMIZE; Windows-only API
    except Exception:
        pass


def profile_pids(profile: Path = PROFILE) -> set:
    """The Edge processes running with the tool's own profile (its --user-data-dir), never the owner's."""
    marker = str(profile).lower()
    pids = set()
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() == "msedge.exe" and \
                        marker in " ".join(proc.info["cmdline"] or []).lower():
                    pids.add(proc.info["pid"])
            except (psutil.Error, TypeError):
                continue
    except ImportError:
        pass
    return pids


def open_edge(url: str = "about:blank", timeout: float = 20.0, profile: Path = PROFILE):
    """Starts Edge on `url` with the tool's own profile and returns ITS window once it's on screen: a new window of
    an Edge process running that profile. (2026-10-11: it took any new Edge window, and a Google sign-in window the
    owner's Pinterest tab opened at that moment became "the task's own"; the run typed into the owner's Edge.)"""
    import uiautomation as auto
    profile.mkdir(parents=True, exist_ok=True)
    before = {w.NativeWindowHandle for w in auto.GetRootControl().GetChildren() if w.ClassName == EDGE_CLASS}
    subprocess.Popen(launch_args(url, profile), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        ours = profile_pids(profile)
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.ClassName == EDGE_CLASS and w.NativeWindowHandle not in before and (w.Name or "").strip() \
                        and w.ProcessId in ours:
                    _maximize(w.NativeWindowHandle)
                    return w
            except Exception:
                continue
    raise TimeoutError("the tool's own Edge didn't open a window")


def find_window(title_part: str) -> object | None:
    import uiautomation as auto
    for w in auto.GetRootControl().GetChildren():
        if w.ClassName == EDGE_CLASS and title_part.lower() in (w.Name or "").lower():
            return w
    return None


def close_window(win, profile: Path = PROFILE) -> None:
    """The window's own close; if Edge is still running with the tool's profile after that (a pop-up can
    block closing), its processes are ended. Only the tool's profile: the owner's Edge is never touched."""
    try:
        win.GetWindowPattern().Close()
    except Exception:
        pass
    time.sleep(1.5)
    marker = str(profile).lower()
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                               "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")],
                             capture_output=True, text=True, timeout=20, check=False).stdout
    except Exception:
        return
    for line in out.splitlines():
        pid, _, cmd = line.partition("\t")
        if marker in cmd.lower() and pid.strip().isdigit():
            subprocess.run(["taskkill", "/PID", pid.strip(), "/F"], capture_output=True, check=False)
