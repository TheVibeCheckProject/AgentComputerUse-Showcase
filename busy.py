"""Taking turns with video renders: a render needs every core on the Mini, so a computer task waits for it to
finish, the same way the agent's image descriptions do."""
import subprocess
import time


def render_running() -> bool:
    """A TiktokVideos render (`tiktokvideos.py render`) is running."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                              "ForEach-Object { $_.CommandLine }"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return False
    return any("tiktokvideos.py" in line.lower() and " render" in line.lower() for line in out.splitlines())


def wait_for_renders(max_wait: float = 45 * 60, poll: float = 30.0, on_wait=None) -> float:
    """Waits while a render runs (at most max_wait seconds); returns how long it waited."""
    start = time.time()
    while render_running() and time.time() - start < max_wait:
        if on_wait:
            on_wait(time.time() - start)
        time.sleep(poll)
    return time.time() - start
