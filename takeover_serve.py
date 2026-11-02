"""A test page for Take over (the agent serves the same thing to DeskAI): this computer's screen in a phone's
browser, and the phone's taps, typing and keys replayed here with the real mouse and keyboard.

    python agentcomputeruse.py takeover-serve --host <this PC's Tailscale IP> --port 8765

It prints a link with a one-time key; anyone with the link controls this computer, so it's only for testing on a
private network (Tailscale) and stops with Ctrl+C. Plain Python, no extra packages.
"""
import json
import secrets
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import takeover

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=yes">
<title>Take over</title>
<style>
  :root { --bg:#0b1120; --panel:#111a2e; --text:#e2e8f0; --muted:#94a3b8; --accent:#f59e0b; --ok:#16a34a; --line:#1e293b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px system-ui, sans-serif; }
  header { position:sticky; top:0; z-index:2; display:flex; gap:8px; align-items:center; justify-content:space-between;
           padding:10px 12px; background:var(--panel); border-bottom:1px solid var(--line); }
  #state { font-weight:600; } #state.off { color:var(--muted); }
  button { font:inherit; color:var(--text); background:#1e293b; border:1px solid #334155; border-radius:10px;
           padding:9px 12px; min-width:44px; }
  button.on { background:var(--accent); color:#111; border-color:var(--accent); font-weight:600; }
  #back { background:var(--ok); border-color:var(--ok); font-weight:600; }
  #wrap { padding:6px; } #screen { width:100%; display:block; border-radius:6px; touch-action:manipulation;
          -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; }
  #hint { color:var(--muted); font-size:13px; padding:4px 12px; }
  .row { display:flex; gap:6px; flex-wrap:wrap; padding:6px 12px; }
  #txt { flex:1; min-width:0; font:inherit; color:var(--text); background:#0f172a; border:1px solid #334155;
         border-radius:10px; padding:9px 10px; }
  #log { color:var(--muted); font-size:12px; padding:2px 12px 16px; min-height:16px; }
</style></head><body>
<header><span id="state">Connecting…</span><button id="back">Hand back</button></header>
<div id="hint">Tap to click · hold for right-click · pinch to zoom</div>
<div id="wrap"><img id="screen" alt="The computer's screen"></div>
<div class="row">
  <button id="dbl">Double</button><button id="drag">Drag</button>
  <button data-scroll="5">Scroll ▲</button><button data-scroll="-5">Scroll ▼</button>
</div>
<div class="row"><input id="txt" placeholder="Type here, then Send" autocomplete="off"><button id="send">Send</button></div>
<div class="row">
  <button data-key="enter">Enter</button><button data-key="backspace">⌫</button><button data-key="tab">Tab</button>
  <button data-key="esc">Esc</button><button data-key="up">↑</button><button data-key="down">↓</button>
  <button data-key="left">←</button><button data-key="right">→</button><button data-key="ctrl+a">Ctrl+A</button>
</div>
<div id="log"></div>
<script>
const key = new URLSearchParams(location.search).get("k");
const H = {"X-Token": key, "Content-Type": "application/json"};
const img = document.getElementById("screen"), state = document.getElementById("state"), log = document.getElementById("log");
let inControl = false, mode = "tap", dragStart = null, last = null, down = null;

async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: H, body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
function setState(on) {
  inControl = on;
  state.textContent = on ? "You're in control" : "The computer has it back";
  state.className = on ? "" : "off";
  document.getElementById("back").textContent = on ? "Hand back" : "Take over";
}
async function start() { try { await post("/start"); setState(true); loop(); } catch (e) { log.textContent = "Couldn't take over: " + e.message; } }
document.getElementById("back").onclick = async () => {
  if (inControl) { await post("/stop"); setState(false); } else { start(); }
};

async function loop() {
  while (inControl) {
    if (!document.hidden) {
      try {
        const r = await fetch("/frame?t=" + Date.now(), {headers: {"X-Token": key}});
        if (r.ok) {
          const url = URL.createObjectURL(await r.blob());
          await new Promise(res => { img.onload = res; img.onerror = res; img.src = url; });
          if (img._old) URL.revokeObjectURL(img._old); img._old = url;
        }
      } catch (e) {}
    }
    await new Promise(res => setTimeout(res, 600));
  }
}

function spot(ev) {             // where on the picture, 0..1 across and down (works zoomed in, too)
  const b = img.getBoundingClientRect();
  return {x: Math.min(1, Math.max(0, (ev.clientX - b.left) / b.width)),
          y: Math.min(1, Math.max(0, (ev.clientY - b.top) / b.height))};
}
async function send(ev, label) {
  try { const r = await post("/input", ev); log.textContent = label || r.done; }
  catch (e) { log.textContent = "Not sent: " + e.message; }
}
img.addEventListener("contextmenu", e => e.preventDefault());
img.addEventListener("pointerdown", e => { if (e.isPrimary) down = {t: Date.now(), x: e.clientX, y: e.clientY, p: spot(e)}; });
img.addEventListener("pointerup", e => {
  if (!e.isPrimary || !down || !inControl) return;
  const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y) > 12, p = down.p, held = Date.now() - down.t;
  down = null;
  if (moved) return;                                   // a pan or pinch, not a tap
  last = p;
  if (mode === "drag") {
    if (!dragStart) { dragStart = p; log.textContent = "Drag: now tap where it should end"; return; }
    send({type: "drag", x: dragStart.x, y: dragStart.y, x2: p.x, y2: p.y}, "Dragged"); dragStart = null; setMode("tap"); return;
  }
  if (mode === "double") { send({type: "double_tap", ...p}, "Double-clicked"); setMode("tap"); return; }
  send({type: held > 600 ? "long_press" : "tap", ...p}, held > 600 ? "Right-clicked" : "Clicked");
});
function setMode(m) {
  mode = m; dragStart = null;
  document.getElementById("dbl").className = m === "double" ? "on" : "";
  document.getElementById("drag").className = m === "drag" ? "on" : "";
}
document.getElementById("dbl").onclick = () => setMode(mode === "double" ? "tap" : "double");
document.getElementById("drag").onclick = () => setMode(mode === "drag" ? "tap" : "drag");
document.querySelectorAll("[data-key]").forEach(b => b.onclick = () => send({type: "key", key: b.dataset.key}, "Pressed " + b.textContent));
document.querySelectorAll("[data-scroll]").forEach(b => b.onclick = () => send({type: "scroll", dy: +b.dataset.scroll, ...(last || {})}, "Scrolled"));
document.getElementById("send").onclick = () => {
  const t = document.getElementById("txt");
  if (t.value) { send({type: "text", text: t.value}, "Typed " + t.value.length + " characters"); t.value = ""; }
};
start();
</script></body></html>"""


def token_from_file(path: Path) -> str:
    """The key the agent uses to reach this computer's Take over (made once, kept in a file only this account
    reads; the agent has a copy in its .env)."""
    path = Path(path)
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    return token


def resolve_host(host: str, wait: float = 300, sleep: Callable[[float], None] = time.sleep) -> str:
    """'tailscale' means this computer's Tailscale address (only phones and computers on the owner's private
    network can reach it). At login Tailscale may not be up yet, so it waits for it."""
    if host != "tailscale":
        return host
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    end = time.time() + wait
    while True:
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()
            if out:
                return out[0]
        except (OSError, subprocess.SubprocessError):
            pass
        if time.time() > end:
            raise SystemExit("Tailscale isn't running: no address to listen on")
        sleep(5)


def on_ac_power() -> bool:
    """True when plugged in (or a desktop): Windows' GetSystemPowerStatus."""
    import ctypes

    class _Status(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    s = _Status()
    try:
        return bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))) and s.ACLineStatus == 1
    except Exception:
        return False


def stay_reachable(stop, every: float = 60.0) -> None:
    """The owner reaches this laptop from their phone when they're away from it, so it mustn't fall asleep: while
    plugged in, tell Windows the system is needed (the screen may still turn off; Take over wakes it). On battery
    it sleeps as usual. Runs until stop is set."""
    import ctypes
    ES_SYSTEM_REQUIRED = 0x00000001
    while not stop.is_set():
        if on_ac_power():
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_SYSTEM_REQUIRED)   # resets the sleep timer
            except Exception:
                pass
        stop.wait(every)


def serve(host: str, port: int, emit: Callable = print, fixed_token: str = None) -> None:
    host = resolve_host(host)
    token = fixed_token or secrets.token_urlsafe(12)
    import threading
    awake_stop = threading.Event()
    if fixed_token:                       # the laptop's Take over (started at login), not a test page
        threading.Thread(target=stay_reachable, args=(awake_stop,), name="StayReachable", daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):          # quiet: no request lines (they'd carry the key)
            pass

        def _ok(self) -> bool:
            if secrets.compare_digest(self.headers.get("X-Token", ""), token):
                return True
            self._json(403, {"error": "wrong key"})
            return False

        def _json(self, code: int, body) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                if not secrets.compare_digest((parse_qs(url.query).get("k") or [""])[0], token):
                    return self._json(403, {"error": "wrong key"})
                data = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(data)
            if not self._ok():
                return
            if url.path == "/activity":
                return self._json(200, takeover.activity_now())     # what a run is doing: the banner (read-only)
            if url.path == "/frame":
                # ?view=1: DeskAI's "View only" box. The picture alone moves nothing, so watching needs no control
                # and a running task carries on.
                view_only = (parse_qs(url.query).get("view") or [""])[0] == "1"
                if not view_only and not takeover.active():
                    return self._json(409, {"error": "take over first"})
                jpeg, meta = takeover.frame(view_only=view_only)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Screen", f"{meta['width']}x{meta['height']}")
                self.end_headers()
                return self.wfile.write(jpeg)
            if url.path == "/status":
                return self._json(200, takeover.status())
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._ok():
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad JSON"})
            try:
                if self.path == "/start":
                    return self._json(200, takeover.start())
                if self.path == "/stop":
                    takeover.release()
                    return self._json(200, {"active": False})
                if self.path == "/input":
                    done = takeover.apply(body)
                    emit("progress", message=f"take over: {done}")
                    return self._json(200, {"done": done})
            except takeover.TakeoverError as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

    server = ThreadingHTTPServer((host, port), Handler)
    if fixed_token:
        emit("progress", message=f"Take over is listening on http://{host}:{port} (key from the token file)")
    else:
        emit("progress", message=f"Take over test page: http://{host}:{port}/?k={token}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        awake_stop.set()
        takeover.release()
        server.server_close()
