"""Single-Pass Form Automation for AgentComputerUse.

Maps detected form elements to user profile values in a single pass,
types them with human-paced keystrokes, and halts cleanly before any submission button.
"""
import re

import actions
import screen
import task as tasks
from hands import Hands
from profile_store import get_field, load_profile

FIELD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("first_name", re.compile(r"\b(first\s*name|given\s*name|fname)\b", re.IGNORECASE)),
    ("last_name", re.compile(r"\b(last\s*name|family\s*name|surname|lname)\b", re.IGNORECASE)),
    ("full_name", re.compile(r"\b(full\s*name|your\s*name|applicant\s*name|customer\s*name|name)\b", re.IGNORECASE)),
    ("email", re.compile(r"\b(e-?mail(\s*address)?)\b", re.IGNORECASE)),
    ("phone", re.compile(r"\b(phone(\s*number)?|telephone|mobile|cell)\b", re.IGNORECASE)),
    ("street_address", re.compile(r"\b(street(\s*address)?|address(\s*line\s*1)?|address)\b", re.IGNORECASE)),
    ("city", re.compile(r"\b(city|town|municipality)\b", re.IGNORECASE)),
    ("state", re.compile(r"\b(state|province|region)\b", re.IGNORECASE)),
    ("zip", re.compile(r"\b(zip(\s*code)?|postal(\s*code)?)\b", re.IGNORECASE)),
    ("country", re.compile(r"\b(country|nation)\b", re.IGNORECASE)),
    ("linkedin_url", re.compile(r"\b(linkedin(\s*url)?)\b", re.IGNORECASE)),
    ("github_url", re.compile(r"\b(github(\s*url)?)\b", re.IGNORECASE)),
    ("portfolio_url", re.compile(r"\b(portfolio|website|personal\s*site)\b", re.IGNORECASE)),
    ("resume_path", re.compile(r"\b(resume|cv|upload\s*resume)\b", re.IGNORECASE)),
]

SUBMIT_BUTTON_PATTERNS = re.compile(
    r"\b(submit|submit\s+application|submit\s+order|apply\s+now|complete\s+order|register|sign\s+up)\b",
    re.IGNORECASE
)


def match_field_to_profile(item: screen.Item, profile: dict[str, str]) -> tuple[str, str] | None:
    """Inspects an Item's label and returns (profile_key, value) if matched."""
    if item.kind not in ("Edit", "ComboBox", "Document"):
        return None
    name = (item.name or "").strip()
    if not name:
        return None

    for key, pattern in FIELD_PATTERNS:
        if pattern.search(name):
            val = get_field(key, profile)
            if val:
                return (key, val)
    return None


def map_form(items: list[screen.Item], profile: dict[str, str] | None = None) -> list[tuple[screen.Item, str, str]]:
    """Scans all actionable items on the page and returns list of (item, profile_key, value)."""
    prof = profile or load_profile()
    matches: list[tuple[screen.Item, str, str]] = []
    seen_keys = set()

    for item in items:
        if item.offscreen:
            continue
        m = match_field_to_profile(item, prof)
        if m:
            key, val = m
            # Avoid re-filling the same semantic field multiple times on the same screen
            if key not in seen_keys or "address" in key:
                matches.append((item, key, val))
                seen_keys.add(key)
    return matches


def find_submit_button(items: list[screen.Item]) -> screen.Item | None:
    """Finds any final submission button on the page."""
    for item in items:
        if (item.kind in ("Button", "Hyperlink") and item.name and not item.offscreen
                and SUBMIT_BUTTON_PATTERNS.search(item.name)):
            return item
    return None


def fill_form_fields(mappings: list[tuple[screen.Item, str, str]], win, hands: Hands, task_obj) -> int:
    """Fills all mapped fields sequentially with human typing delays.

    Returns count of successfully typed fields.
    """
    count = 0
    tasks.check_may_type_here(task_obj, win)
    for item, key, val in mappings:
        try:
            actions.type_into(item, val, win, hands, secret=False)
            count += 1
        except Exception:
            continue
    return count
