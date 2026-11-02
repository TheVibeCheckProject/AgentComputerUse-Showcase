"""Asking the owner: questions and approvals through DeskAI (Needs you), waiting while they've taken over,
sign-in pages (their own options, a saved password, or a password the owner types in the app; the AI never
sees it) and pop-up choices. Mixed into Loop. (Split out of loop.py, 2026-10-11.)"""
import re
import time
from typing import Dict, List, Optional

import actions
import screen
import task as tasks
from rules import (AUTH_ALT_PATTERNS, PHONE_PROMPT_PATTERNS, SIGNIN_URL, TYPE_PASSWORD, _HandedBack, _Paused)


class OwnerMixin:
    # ------------------------------------------------------------------ asking the owner
    def _ask(self, kind: str, question: str, choices=None, highlight=None) -> Optional[str]:
        """Asks the owner through DeskAI (with a screenshot, the spot outlined) and waits. None when there's no
        way to ask (a run without the app); raises _Paused when no answer comes in time."""
        if self.asker is None:
            return None
        import requests_owner
        r = self.window.BoundingRectangle if self.window is not None else None
        png = requests_owner.screenshot((r.left, r.top, r.right, r.bottom) if r else None, highlight)
        try:
            ans = self.asker(kind, question, choices, self._url(), png)
        except requests_owner.NoAnswer as e:
            raise _Paused(f"paused: {e} (nothing was submitted)")
        import takeover
        if ans == takeover.HANDED_BACK:
            if self.log:
                self.log.write({"event": "owner", "kind": kind, "question": question, "answer": "(took over, handed back)"})
            raise _HandedBack()
        if self.log:
            self.log.write({"event": "owner", "kind": kind, "question": question,
                            "answer": "(hidden)" if kind == "secret" else ans[:300]})
        return ans

    def _wait_while_owner_in_control(self, poll: float = 1.0) -> None:
        """Take over: while the owner uses the computer from their phone, the task neither looks nor asks the AI
        (the screen is changing under them). Waiting doesn't count against the time limit."""
        import takeover
        if not takeover.active():
            return
        self.emit("progress", message="the owner is in control (Take over): waiting until they hand back")
        started = time.time()
        guard = self.hands.guard
        while takeover.active() and not guard.killed.is_set():
            time.sleep(poll)
        guard.deadline += time.time() - started
        guard.last_pos = None
        raise _HandedBack()

    def _url(self) -> Optional[str]:
        if self.window is None:
            return None                         # (without a window, look() would read whatever is in front)
        try:
            return screen.look(self.window, use_ocr=False, wait=False).url
        except Exception:
            return None

    def _owner_said(self, question: str, answer: str) -> None:
        self.history.append({"action": f"asked the owner: {question[:100]}", "result": f"OWNER ANSWERED: {answer[:300]}",
                             "expect": None})
        self.owner_instructions.append(f"Asked {question[:100]!r}, the owner said: {answer[:300]}")
        self.trouble = 0

    @staticmethod
    def find_auth_alternatives(view: screen.View) -> List[screen.Item]:
        alts = []
        for i in view.items:
            if i.offscreen or not i.name:
                continue
            if i.kind in ("Button", "Hyperlink", "ListItem", "Text") and AUTH_ALT_PATTERNS.search(i.name):
                alts.append(i)
        return alts

    @staticmethod
    def password_field(view: screen.View) -> Optional[screen.Item]:
        for i in view.items:
            if i.offscreen or not i.name:
                continue
            if i.kind in ("Edit", "Document") and "password" in i.name.lower():
                return i
        return None

    @classmethod
    def has_password_field(cls, view: screen.View) -> bool:
        return cls.password_field(view) is not None

    @staticmethod
    def _domain(url: Optional[str]) -> str:
        m = re.match(r"^(?:\w+://)?([^/?#]+)", (url or "").strip())
        return m.group(1).lower() if m else ""

    @classmethod
    def is_signin_page(cls, view: screen.View) -> bool:
        return bool(SIGNIN_URL.search(view.url or "") or cls.password_field(view) is not None)

    @classmethod
    def detect_phone_prompt(cls, view: screen.View) -> Optional[str]:
        """A sign-in page waiting for the owner to approve on their phone. Only on sign-in pages: an article
        that says "check your phone" isn't waiting for anyone."""
        if not (cls.is_signin_page(view) or cls.find_auth_alternatives(view)):
            return None
        text = " \n ".join(i.name for i in view.items if i.name and not i.offscreen)
        if not PHONE_PROMPT_PATTERNS.search(text):
            return None
        m = re.search(r"\b(?:tap|number|select|code)\s+(\d{1,2})\b", text, re.I)
        num_hint = f" (tap number {m.group(1)})" if m else ""
        site = cls._domain(view.url)
        return f"{site or 'The site'} sent a sign-in prompt to your phone: tap Yes{num_hint}"

    def _signin_options(self, view: screen.View, password: bool = True) -> Dict[str, screen.Item]:
        """On a sign-in page, what the owner can pick, each tied to the item it acts on: their password (typed
        into the password box from the app) and the page's other ways to sign in ('Try another way'...)."""
        if not (self.is_signin_page(view) or self.find_auth_alternatives(view)):
            return {}
        opts: Dict[str, screen.Item] = {}
        field_ = self.password_field(view) if password else None
        if field_ is not None:
            opts[TYPE_PASSWORD] = field_
        for i in self.find_auth_alternatives(view):
            if len(opts) >= 3:
                break
            opts.setdefault(i.name.strip()[:60], i)
        return opts

    def _pick(self, answer: str, offered: Dict[str, screen.Item]) -> Optional[str]:
        """The owner picked one of the offered choices: act on the very item it came from. Anything else they
        wrote is never matched against the screen (an answer like 'Springfield' must not click a link)."""
        key = next((k for k in offered if k.strip().lower() == (answer or "").strip().lower()), None)
        if key is None:
            return None
        item = offered[key]
        if key == TYPE_PASSWORD:
            return self._owner_types(item, "Type your password")
        if self.window is not None and not self._may_touch(self.window):
            raise tasks.NotMine("the owner's choice is in a window the task didn't open")
        self.emit("progress", message=f"the owner chose {key!r}")
        actions.click(item, self.window, self.hands)
        self.trouble = 0
        return f"clicked {item.kind} {item.name!r} (the owner's choice)"

    def _fill_saved_password(self, item: screen.Item) -> Optional[str]:
        """The password saved for this site (Windows Credential Manager, `save-password`), typed straight into the
        field; None when there isn't one, so the owner is asked instead. Once per site per run: if the site turns
        it down, the next ask goes to the owner rather than typing the same wrong password again."""
        import credentials
        url = self._url()
        if not url:
            return None
        try:
            found = credentials.find_for_url(url)
        except Exception:
            return None                                 # no keyring, unreadable index: ask the owner
        if not found:
            return None
        domain, _user, password = found
        if domain in self._autofilled:
            return None
        self._autofilled.add(domain)
        # A kill switch or the owner's hand here must stop the run, so only the lookup above is forgiving.
        tasks.check_may_type_here(self.task, self.window)
        actions.type_into(item, password, self.window, self.hands, secret=True)
        del password
        self.history.append({"action": f"saved password for {domain}",
                             "result": "typed in from Credential Manager (hidden); press the page's Next or Sign in "
                                       "button next", "expect": None})
        self.trouble = 0
        return f"saved password for {domain} was typed into {item.name or item.kind!r}"

    def _owner_types(self, item: screen.Item, question: str) -> Optional[str]:
        """A password or code: autofills from Credential Manager if stored for this domain,
        else the owner types it in the app, and the tool types it into this field. The value is
        never shown to the model, never put in the history, a hint or the step log, and its answer file is
        deleted once read (requests_owner). None when there's no way to ask."""
        asked = question.lower() + " " + (item.name or "").lower()
        is_pw = "password" in asked and not re.search(r"\b(code|otp|one-time|verification)\b", asked)
        if is_pw:
            saved = self._fill_saved_password(item)
            if saved:
                return saved

        tasks.check_may_type_here(self.task, self.window)
        site = self._domain(self._url())
        q = f"{question} for {site}" if site and site not in question else question
        value = self._ask("secret", q, highlight=item.rect)
        if value is None:
            return None
        if not value:
            raise _Paused("the owner sent an empty answer for a password or code")
        try:
            actions.type_into(item, value, self.window, self.hands, secret=True)
            if is_pw:
                # Kept in memory only, until the run ends: saved if it worked and the owner says so
                # (offer_to_save_password). Until 2026-10-27 it was saved at once, so a mistyped one stuck.
                self._typed_password = (self._url() or "", value)
        finally:
            value = None
        self.history.append({"action": f"the owner typed into {item.name or item.kind!r}",
                             "result": "typed in (hidden from you); press the page's Next or Sign in button next",
                             "expect": None})
        self.trouble = 0
        return f"the owner's answer was typed into {item.name or item.kind!r}"

    def offer_to_save_password(self, status: str) -> None:
        """At the end of a run: a password the owner typed in the app (kept in memory, never written anywhere) is
        offered for saving only if the run finished, so the sign-in worked. Saved for the exact host it was typed
        on (find_for_url looks there first; no guessing which part of 'accounts.google.co.uk' is the site), under
        the username this run typed. Replaces 2026-10-26's auto-save, which saved before knowing the password worked,
        guessed the domain and the user ('default'), and hid its failures (docs/AUDIT_2026-10-27.md S3)."""
        pending, self._typed_password = getattr(self, "_typed_password", None), None
        if not pending or status != "done":
            return
        url, password = pending
        try:
            import credentials
            host = credentials.extract_hostname(url)
            if not host or "." not in host:
                return
            user = (getattr(self, "_last_typed_user", None) or "").strip() or "default"
            ans = self._ask("choice", f"Save the password you typed for {host} ({user}) so later runs sign in "
                                      f"by themselves?", ["Save", "Don't save"])
            if (ans or "").strip().lower() != "save":
                return
            credentials.store_secure_credential(host, user, password)
            self.emit("progress", message=f"saved the password for {host} ({user}) in Credential Manager")
        except _Paused:
            pass                                          # no answer in time: nothing saved
        except Exception as e:
            self.emit("progress", message=f"couldn't save the password: {type(e).__name__}")   # never the value
        finally:
            password = None

    def _approval(self, item, why: str, view) -> str:
        """A risky step: the owner approves (it's clicked now) or denies. Returns approved, declined or
        needs_approval (no way to ask)."""
        ans = self._ask("approve", f"Approve {why}?", ["Approve", "Deny"], highlight=item.rect if item else None)
        if ans is None:
            return "needs_approval"
        if ans.strip().lower() not in ("approve", "approved", "yes", "ok"):
            return "declined"
        actions.click(item, self.window, self.hands)
        self.history.append({"action": f"click {item.name!r}", "result": "the owner approved; clicked", "expect": None})
        self.trouble = 0
        return "approved"

    def _popup_choice(self, e) -> Optional[str]:
        """A pop-up without a harmless answer: the owner picks one of its own choices, which is clicked."""
        ans = self._ask("choice", f"A pop-up asks: {e.popup.text[:200]}", e.popup.choices, highlight=e.popup.rect)
        if ans is None:
            return None
        if ans not in e.popup.choices:
            raise _Paused(f"the answer {ans!r} isn't one of the pop-up's choices")
        ctrl = e.popup.controls[e.popup.choices.index(ans)]
        r = ctrl.BoundingRectangle
        self.hands.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        time.sleep(0.8)
        return ans
