"""AgentComputerUse: uses the Windows computer like a person, for the AlwaysOnAgent (the brain).

Reads the screen through Windows' accessibility list plus an OCR pass; acts only with the real mouse and
keyboard. Like Connectors and the other tools, every command prints JSON lines on stdout: `progress` events,
then one `done` (exit 0) or `error` (exit 1).

Anything that would move the mouse or type is a DRY RUN unless --go is given.

  python agentcomputeruse.py windows
  python agentcomputeruse.py observe [--window TITLE] [--no-ocr] [--text]
  python agentcomputeruse.py act click 12 [--go]          (also: type 7 "text", press ctrl+l, scroll down,
                                                           open-url https://..., launch notepad)
  python agentcomputeruse.py demo [--go]                  the phase 1 check: Edge -> Notepad -> saved file
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
LAST_VIEW = ROOT / "data" / "last_view.json"
import takeover


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _window(title: str | None = None):
    import uiautomation as auto

    import screen
    if not title:
        return screen.foreground()
    for w in auto.GetRootControl().GetChildren():
        if title.lower() in (w.Name or "").lower():
            return w
    raise SystemExit(f"no window with {title!r} in its title")


def cmd_windows(args) -> None:
    import uiautomation as auto

    import screen
    out = []
    for w in auto.GetRootControl().GetChildren():
        r = w.BoundingRectangle
        if (w.Name or "").strip() and r.width() > 0:
            out.append({"title": w.Name.replace("\u200b", ""), "process": screen.process_name(w.ProcessId),
                        "rect": [r.left, r.top, r.right, r.bottom]})
    emit("done", windows=out)


def cmd_screen(args) -> None:
    """The screen's size; with --wake, wakes a sleeping monitor and waits for its real size, then lets it
    sleep again."""
    import busy
    import display
    before = display.size()
    if args.wake:
        try:
            after = display.wake(timeout=args.seconds)
        finally:
            display.let_sleep()
        emit("done", before=before, after=after, render_running=busy.render_running())
    else:
        emit("done", size=before, render_running=busy.render_running())


def cmd_observe(args) -> None:
    import screen
    view = screen.look(_window(args.window), use_ocr=not args.no_ocr)
    LAST_VIEW.parent.mkdir(parents=True, exist_ok=True)
    LAST_VIEW.write_text(json.dumps(view.to_json(), ensure_ascii=False), encoding="utf-8")
    if args.text:
        emit("done", text=view.text(), took=round(view.took, 2), count=len(view.items))
    else:
        emit("done", **view.to_json())


def _resolve(n: int, win):
    """Finds item n of the last observe again in a fresh look (same kind and name, nearest position)."""
    import screen
    if not LAST_VIEW.exists():
        raise SystemExit("run observe first")
    saved = json.loads(LAST_VIEW.read_text(encoding="utf-8"))
    want = next((i for i in saved["items"] if i["id"] == n), None)
    if want is None:
        raise SystemExit(f"item {n} isn't in the last observe")
    view = screen.look(win, use_ocr=want["source"] != "uia")
    same = [i for i in view.items if i.kind == want["kind"] and i.name == want["name"]]
    if not same:
        raise SystemExit(f"item {n} ({want['kind']} {want['name']!r}) isn't on screen any more")
    wx, wy = (want["rect"][0] + want["rect"][2]) / 2, (want["rect"][1] + want["rect"][3]) / 2
    return min(same, key=lambda i: abs(i.center()[0] - wx) + abs(i.center()[1] - wy))


def cmd_act(args) -> None:
    import actions
    from hands import Hands
    hands = Hands(dry_run=not args.go)
    if args.go:
        hands.guard.start_kill_switch()
    win = _window(args.window)
    try:
        what, rest = args.action, args.rest
        if what == "click":
            item = _resolve(int(rest[0]), win)
            answered = actions.click(item, win, hands)
            emit("done", did=hands.log, popup_answered=answered, dry_run=hands.dry_run)
        elif what == "type":
            item = _resolve(int(rest[0]), win)
            actions.type_into(item, " ".join(rest[1:]), win, hands)
            emit("done", did=hands.log, dry_run=hands.dry_run)
        elif what == "press":
            actions.press(rest[0], win, hands)
            emit("done", did=hands.log, dry_run=hands.dry_run)
        elif what == "scroll":
            r = win.BoundingRectangle
            hands.scroll((r.left + r.right) // 2, (r.top + r.bottom) // 2, -5 if rest[0] == "down" else 5)
            emit("done", did=hands.log, dry_run=hands.dry_run)
        elif what == "open-url":
            actions.open_url(rest[0], win, hands)
            emit("done", did=hands.log, dry_run=hands.dry_run)
        elif what == "launch":
            w = actions.launch_app(" ".join(rest), hands)
            emit("done", did=hands.log, window=(w.Name if w is not None else None), dry_run=hands.dry_run)
        else:
            raise SystemExit(f"unknown action {what!r}")
    finally:
        hands.guard.stop_kill_switch()


def cmd_demo(args) -> None:
    """Phase 1's check, no AI: search in Edge, put what the page says into a NEW Notepad page, save it as a
    new file. Understands before acting: checks each situation, touches only what it made."""
    import actions
    import browser
    import screen
    import task as tasks
    from hands import Guard, Hands
    from steplog import StepLog

    hands = Hands(guard=Guard(seconds=args.seconds, paused=takeover.active), dry_run=not args.go)
    t = tasks.Task("demo: Edge search -> a new Notepad page -> a new file")
    log = StepLog(t.goal, screenshots=args.go)
    out_file = ROOT / "data" / "demo" / f"result-{time.strftime('%H%M%S')}.txt"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    query = args.query

    def step(intent: str, **details):
        """The intention is written down before the step happens."""
        emit("progress", message=intent)
        log.step(intent, **details)

    if not args.go:
        emit("progress", message="DRY RUN: nothing will move or be typed (add --go for real)")
        for s in ("open Edge (its own profile)", f"search for {query!r} in the address bar",
                  "read the results page", "open Notepad from the Start button",
                  "make sure it's on a new, empty page of its own (Ctrl+N if it shows someone's file)",
                  "type two lines", "check the page is its own untitled one, then Ctrl+S",
                  "expect a Save dialog; type a NEW file name", "close its own tab and its own Edge"):
            hands.log.append(s)
        actions.launch_app("notepad", hands)               # the Start button route, described
        hands.log.append("press ctrl+s")
        emit("done", ok=True, steps=hands.log, dry_run=True)
        return

    import busy
    import display
    waited = busy.wait_for_renders(on_wait=lambda secs: emit("progress", message=f"a video render is running: "
                                                                              f"waiting ({int(secs)} s so far)"))
    if waited > 1:
        log.step("waited for a render", seconds=round(waited))
    screen_size = display.wake()                        # the monitor may be asleep (1024x768 fallback)
    emit("progress", message=f"screen on at {screen_size[0]}x{screen_size[1]}")
    hands.guard.start_kill_switch()
    edge = notepad = None
    our_tab_open = False
    try:
        step("open Edge (its own profile)")
        edge = browser.open_edge("about:blank")
        t.opened(edge)
        t.between_steps()
        answered = actions.clear_popups(edge, hands)
        if answered:
            step("answered pop-ups", answers=answered)

        step(f"search for {query!r} in the address bar", query=query)
        actions.open_url("https://www.bing.com/search?q=" + query.replace(" ", "+"), edge, hands)
        t.between_steps()

        view = screen.look(edge)
        pool = [i for i in view.items if not i.offscreen and i.kind in ("Text", "Header", "Hyperlink")
                and len(i.name) > 25 and query.lower() not in i.name.lower()]
        texts = [i.name for i in pool if i.source != "ocr"] + [i.name for i in pool if i.source == "ocr"]
        found = texts[0] if texts else ""
        step("read the results page", items=len(view.items), first_text=found[:200])

        step("open Notepad from the Start button")
        import uiautomation as auto
        existing = {w.NativeWindowHandle for w in auto.GetRootControl().GetChildren()
                    if screen.process_name(w.ProcessId).lower() == "notepad.exe"}
        notepad = actions.launch_app("notepad", hands)
        if notepad.NativeWindowHandle not in existing:
            t.opened(notepad)                               # a new window: its own
        t.between_steps()

        tab_before = tasks.active_tab(notepad)
        step("make sure it's on a new, empty page of its own", notepad_showed=tab_before)
        tab = tasks.ensure_blank_page(t, notepad, hands)
        our_tab_open = True
        step("on its own new page", tab=tab)
        t.between_steps()

        tasks.check_may_type_here(t, notepad)
        view = screen.look(notepad, use_ocr=False)
        editor = next((i for i in view.items if i.kind == "Document" and not i.offscreen), None)
        if editor is None:
            raise tasks.Unexpected("Notepad's text area isn't where expected")
        step("type two lines into its own page")
        actions.click(editor, notepad, hands)
        tasks.check_may_type_here(t, notepad)              # still its own page after the click?
        hands.type_text(f"Search: {query}")
        hands.press("enter")
        hands.type_text(f"First result: {found or '(nothing read)'}")
        hands.press("enter")
        t.expect(lambda: f"Search: {query}" in (tasks.document_text(notepad) or ""), "the typed lines on the page")
        t.between_steps()

        tasks.check_may_save(t, notepad)                    # its own untitled page only
        step("Ctrl+S on its own untitled page: a Save dialog must come up")
        hands.press("ctrl+s")
        t.expect(lambda: tasks.file_name_box(notepad) is not None, "a Save dialog with a File name box", 6)
        box = tasks.file_name_box(notepad)
        r = box.BoundingRectangle
        item = screen.Item(0, "Edit", box.Name or "File name:", (r.left, r.top, r.right, r.bottom), control=box)
        if out_file.exists():
            raise tasks.Unexpected(f"{out_file} already exists: won't overwrite it")
        dialog = actions.dialog_of(notepad) or notepad      # the Save dialog is a window of its own
        step("type the new file's name into the Save dialog and save", file=str(out_file))
        actions.type_into(item, str(out_file), dialog, hands)
        hands.press("enter")
        t.expect(lambda: out_file.exists(), "the new file on disk", 6)
        t.files.add(str(out_file))
        step("saved", file=str(out_file))
        log.end(True, file=str(out_file))
        emit("done", ok=True, file=str(out_file), steps=hands.log, run=log.run_id, dry_run=False)
    finally:
        hands.guard.stop_kill_switch()
        display.let_sleep()                             # the monitor may sleep again
        if args.close:
            if edge is not None:
                browser.close_window(edge)
            if notepad is not None and our_tab_open and out_file.exists():
                try:
                    tab, tab_id = tasks.selected_tab(notepad)
                    if t.owns_window(notepad):
                        notepad.GetWindowPattern().Close()        # its own window, saved: no question
                    elif tab_id in t.tab_ids:
                        hands.press("ctrl+w")                     # only its own (saved) tab
                except Exception:
                    pass


def cmd_run(args) -> None:
    """Phase 2: a goal, done step by step by the AI loop. Without --go: the plan and the first step only."""
    import loop
    from hands import Guard, Hands
    if args.goal_file:
        args.goal = Path(args.goal_file).read_text(encoding="utf-8-sig").strip()
    if not (args.goal or "").strip():
        raise SystemExit("give the goal, or --goal-file with the goal in it")
    from steplog import StepLog
    hands = Hands(guard=Guard(seconds=args.seconds, paused=takeover.active), dry_run=not args.go)
    asker = None
    if args.go and args.ask_owner:
        import requests_owner

        def asker(kind, question, choices, url, png):
            started = time.time()
            hands.guard.waiting_for_owner = True                # the watchdog doesn't count this wait
            try:
                return requests_owner.ask(kind, question, choices, url, png, emit=emit,
                                          stopped=hands.guard.killed.is_set)
            finally:
                hands.guard.waiting_for_owner = False
                hands.guard.deadline += time.time() - started     # waiting for the owner isn't working time
                hands.guard.last_pos = None                         # the mouse may have moved meanwhile
    lp = loop.Loop(args.goal, hands, max_steps=args.max_steps, emit=emit,
                   log=StepLog("run: " + args.goal, screenshots=args.go), allowed=args.allow or [], asker=asker)
    if not args.go:
        emit("progress", message="DRY RUN: plan and first step only; nothing will move or be typed (add --go)")
        emit("done", dry_run=True, **lp.preview())
        return
    import busy
    import display
    waited = busy.wait_for_renders(on_wait=lambda secs: emit("progress", message=f"a render is running: "
                                                                              f"waiting ({int(secs)} s)"))
    # The monitor is checked WHILE the run reads the screen and asks the model, not before (2026-10-08, the owner:
    # 25 s of waiting for a monitor that was off). The run starts at today's size; if a monitor wakes meanwhile the
    # size changes, and the guard makes the loop look again at the new size before any action (ScreenChanged).
    # Without --allow-small-screen, the first action waits for the check (never acting on the no-monitor screen).
    import threading
    hands.guard.lock_screen_size(display.size(), display.size)
    checked = threading.Event()
    if args.allow_small_screen:
        checked.set()
    else:
        hands.guard.gate = checked

    def check_monitor() -> None:
        try:
            size, small = display.screen_for_run(allow_small=args.allow_small_screen,
                                                 timeout=5.0 if args.allow_small_screen else 15.0)
            if small:
                emit("progress", message=f"no monitor: running on Windows' {size[0]}x{size[1]} screen "
                                         f"(--allow-small-screen); a monitor coming on later is picked up",
                     warning="small screen")
            else:
                emit("progress", message=f"screen on at {size[0]}x{size[1]}", waited_for_render=round(waited))
        except Exception as e:
            hands.guard.abort_reason = f"{e}"
        finally:
            checked.set()
    threading.Thread(target=check_monitor, name="MonitorCheck", daemon=True).start()

    def overdue() -> None:
        emit("error", message=f"the run went {int(hands.guard.WATCHDOG_GRACE)} s past its time limit without "
                              f"stopping, so it was ended (nothing more will move)", stopped=True)
        sys.stdout.flush()
        os._exit(3)
    hands.guard.start_watchdog(overdue)
    hands.guard.start_kill_switch()
    # What it's doing, for the banner on Take over's screen (activity.py): every progress event also goes there
    import activity
    activity.start(args.goal)

    def emit_run(event, **fields):
        # Each step also as a plain sentence ("say", with its "level"), for the View only screen and the agent's task
        # log. Typed text leaves this process only inside that sentence, where secrets are hidden (activity.say)
        if event == "progress":
            plain = activity.say(fields)
            fields = {k: v for k, v in fields.items() if k != "text" and v is not None}
            if plain:
                fields.update(say=plain[0], level=plain[1], kind=activity.kind_of(fields))
        emit(event, **fields)
        activity.note(event, fields)
    lp.emit = emit_run
    res = None
    try:
        res = lp.run()
    finally:
        activity.finish(res.status if res else "stopped", res.answer if res else None, res.question if res else None)
        hands.guard.stop_kill_switch()
        display.let_sleep()
        if args.close and lp.edge is not None:
            import browser
            browser.close_window(lp.edge)
    emit("done", status=res.status, answer=res.answer, evidence=res.evidence, question=res.question,
         steps=res.steps, requests=res.requests, local_requests=res.local_requests, run=res.run, detail=res.detail)


def cmd_takeover_serve(args) -> None:
    import takeover_serve
    token = takeover_serve.token_from_file(args.token_file) if args.token_file else None
    takeover_serve.serve(args.host, args.port, emit=emit, fixed_token=token)


def cmd_save_password(args) -> None:
    """Saves a site's password in Windows Credential Manager for sign-ins to fill in by themselves. The password is
    read from stdin (or typed at a hidden prompt), never from the command line, where it would land in the shell's
    history and the process list."""
    import credentials
    if sys.stdin.isatty():
        import getpass
        password = getpass.getpass(f"Password for {args.username} on {args.domain}: ")
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise SystemExit("no password given (send it on stdin)")
    domain = credentials.extract_hostname(args.domain)
    if "." not in domain:
        raise SystemExit(f"{args.domain!r} isn't a web address like google.com")
    credentials.store_secure_credential(domain, args.username, password)
    emit("done", saved=domain, username=args.username)


def cmd_forget_password(args) -> None:
    import credentials
    domain = credentials.extract_hostname(args.domain)
    gone = credentials.forget_credential(domain, args.username)
    emit("done", forgotten=gone, domain=domain, username=args.username)


def cmd_passwords(args) -> None:
    import credentials
    emit("done", saved=[{"domain": d, "username": u} for d, u in credentials.list_credentials()])


def cmd_retasks(args) -> None:
    """Saved clean runs that the same task repeats without the AI (retasks.py)."""
    import retasks
    emit("done", retasks=retasks.list_all())


def cmd_forget_retask(args) -> None:
    import retasks
    emit("done", forgotten=retasks.forget(args.id))


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentComputerUse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("windows")
    sc = sub.add_parser("screen")
    sc.add_argument("--wake", action="store_true")
    sc.add_argument("--seconds", type=float, default=25)
    o = sub.add_parser("observe")
    o.add_argument("--window")
    o.add_argument("--no-ocr", action="store_true")
    o.add_argument("--text", action="store_true")
    a = sub.add_parser("act")
    a.add_argument("action")
    a.add_argument("rest", nargs="*")
    a.add_argument("--window")
    a.add_argument("--go", action="store_true", help="really move and type (otherwise a dry run)")
    r = sub.add_parser("run")
    r.add_argument("goal", nargs="?")
    r.add_argument("--goal-file", help="read the goal from this UTF-8 text file (no quoting through shells: "
                                       "2026-10-08, a goal's quotes were stripped between PowerShell layers)")
    r.add_argument("--go", action="store_true", help="really do it (otherwise: plan and first step only)")
    r.add_argument("--max-steps", type=int, default=25)
    r.add_argument("--seconds", type=float, default=600)
    r.add_argument("--allow", nargs="*", help="windows or files of the owner's this task may change")
    r.add_argument("--close", action="store_true", help="close the task's Edge at the end")
    # Monitor on or off, a run works (2026-10-11, the owner: "the screen will be off for a little bit, that
    # shouldn't hinder the agent, it needs to be able to do both"). The real monitor is used whenever it's on or
    # wakes, even mid-run (the guard makes the loop look again at the new size).
    r.add_argument("--allow-small-screen", action="store_true", default=True,
                   help="(the default) if no monitor wakes, run on Windows' no-monitor screen (1024x768)")
    r.add_argument("--need-monitor", dest="allow_small_screen", action="store_false",
                   help="stop instead when no monitor wakes")
    r.add_argument("--ask-owner", action="store_true",
                   help="ask the owner (needs_you events + data/answers) instead of stopping; the agent sets this")
    ts = sub.add_parser("takeover-serve", help="a test page for Take over: this computer's screen in a phone's "
                                               "browser, its taps replayed here (the agent serves DeskAI instead)")
    ts.add_argument("--host", default="127.0.0.1",
                    help="an address phones can reach: this PC's Tailscale IP, or 'tailscale' to look it up")
    ts.add_argument("--port", type=int, default=8765)
    ts.add_argument("--token-file", help="keep the key in this file (made on first use), so the agent can reach "
                                         "this computer's Take over: the laptop's Take over in DeskAI")
    d = sub.add_parser("demo")
    d.add_argument("--go", action="store_true")
    d.add_argument("--query", default="weather in Springfield Illinois")
    d.add_argument("--seconds", type=float, default=180)
    d.add_argument("--close", action="store_true", help="close Edge and Notepad at the end")
    sp = sub.add_parser("save-password", help="save a site's password for sign-ins (the password on stdin)")
    sp.add_argument("domain", help="the site, e.g. google.com (also used on its subdomains)")
    sp.add_argument("username")
    fp = sub.add_parser("forget-password")
    fp.add_argument("domain")
    fp.add_argument("username")
    sub.add_parser("passwords", help="the saved sites and usernames (never the passwords)")
    sub.add_parser("retasks", help="saved clean runs the same task repeats without the AI")
    fr = sub.add_parser("forget-retask")
    fr.add_argument("id")
    args = ap.parse_args()
    try:
        {"windows": cmd_windows, "screen": cmd_screen, "observe": cmd_observe, "act": cmd_act, "demo": cmd_demo,
         "run": cmd_run, "takeover-serve": cmd_takeover_serve, "save-password": cmd_save_password,
         "forget-password": cmd_forget_password, "passwords": cmd_passwords, "retasks": cmd_retasks,
         "forget-retask": cmd_forget_retask}[args.cmd](args)
        return 0
    except SystemExit as e:
        emit("error", message=str(e))
        return 1
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)              # where it failed, beside the JSON on stdout
        name = type(e).__name__
        emit("error", message=f"{name}: {e}", stopped=name == "Stopped")
        return 1


if __name__ == "__main__":
    sys.exit(main())
