"""The loop's fixed rules: the actions the model may choose, what counts as risky or a text field, sign-in
patterns, limits, and the exceptions that stop or pause a step. (Split out of loop.py, 2026-10-11.)"""
import re

ACTIONS = ("click", "type", "press", "scroll", "open_url", "launch_app", "new_page", "switch_window", "wait",
           "needs_approval", "ask_owner", "owner_types", "done", "fail", "save_as", "open_file")
TYPEABLE = {"Edit", "ComboBox", "Document"}
# Keys that select or delete text: refused while a text field has the keyboard (type the whole new text instead)
EDITING_KEYS = re.compile(r"(^|\+)\s*(backspace|delete|del)\s*$|shift\s*\+\s*(ctrl\s*\+\s*)?(left|right|up|down|home|"
                          r"end|page_?up|page_?down)|ctrl\s*\+\s*shift\s*\+\s*(left|right|up|down|home|end)|"
                          r"ctrl\s*\+\s*(x|backspace|delete)", re.I)
RISKY_CLICK = re.compile(r"\b(submit|send|pay|buy|purchase|place order|checkout|check out|delete|remove|post|"
                         r"publish|install|confirm order|sign up|create account|register|enter now|enter giveaway|"
                         r"apply now|submit application|unsubscribe)\b", re.I)
AUTH_ALT_PATTERNS = re.compile(
    r"\b(try another way|more ways to sign in|other ways to sign in|sign in another way|"
    r"use another method|use your phone|sign in with a different account|use passkey|"
    r"forgot password)\b", re.I
)
PHONE_PROMPT_PATTERNS = re.compile(
    r"\b(check your phone|tap yes on your phone|tap yes|sent a notification to your|"
    r"open the google app|approve the sign-in|verify it's you on your device)\b", re.I
)
# Only a sign-in page can be waiting on the owner's phone (an article saying "check your phone" isn't)
SIGNIN_URL = re.compile(r"(accounts\.|login|log-in|signin|sign-in|/auth|oauth|challenge|verify)", re.I)
TAPPED_YES = "I tapped Yes"
TYPE_PASSWORD = "Type my password"
CONSOLE_PROCESSES = {"cmd.exe", "conhost.exe", "windowsterminal.exe", "openconsole.exe", "powershell.exe",
                     "pwsh.exe", "python.exe", "py.exe"}
LIST_LIMIT = 140


# "A 200 word story" means about 200: anything from 70% up passes, only a clearly short text is sent back
# (2026-10-10, the owner, after a 167-word story was refused and they were asked about it: "It was just a simple ask
# for a 200 word thing. It doesn't have to be perfect.")
LENGTH_SLACK = 0.7


class _Paused(Exception):
    """No answer from the owner in time: the task pauses safely (nothing half-submitted)."""


class _HandedBack(Exception):
    """The owner answered a request by taking over the computer and handing back: look again, don't act."""


class _NeedsApproval(Exception):
    def __init__(self, item, why):
        super().__init__(why)
        self.item, self.why = item, why
