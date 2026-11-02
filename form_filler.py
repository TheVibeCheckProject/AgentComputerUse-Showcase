"""Single-Pass Form Automation for AgentComputerUse.

Maps detected form elements to user profile values in a single pass,
types them with human-paced keystrokes, and halts cleanly before any submission button.
"""
import re
from typing import Dict, List, Optional, Tuple

import actions
import screen
import task as tasks
from hands import Hands
from profile_store import get_field, load_profile

FIELD_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("first_name", re.compile(r"\b(first\s*name|given\s*name|fname)\b", re.I)),
    ("last_name", re.compile(r"\b(last\s*name|family\s*name|surname|lname)\b", re.I)),
    ("full_name", re.compile(r"\b(full\s*name|your\s*name|applicant\s*name|customer\s*name|name)\b", re.I)),
    ("email", re.compile(r"\b(e-?mail(\s*address)?)\b", re.I)),
    ("phone", re.compile(r"\b(phone(\s*number)?|telephone|mobile|cell)\b", re.I)),
    ("street_address", re.compile(r"\b(street(\s*address)?|address(\s*line\s*1)?|address)\b", re.I)),
    ("city", re.compile(r"\b(city|town|municipality)\b", re.I)),
    ("state", re.compile(r"\b(state|province|region)\b", re.I)),
    ("zip", re.compile(r"\b(zip(\s*code)?|postal(\s*code)?)\b", re.I)),
    ("country", re.compile(r"\b(country|nation)\b", re.I)),
    ("linkedin_url", re.compile(r"\b(linkedin(\s*url)?)\b", re.I)),
    ("github_url", re.compile(r"\b(github(\s*url)?)\b", re.I)),
    ("portfolio_url", re.compile(r"\b(portfolio|website|personal\s*site)\b", re.I)),
    ("resume_path", re.compile(r"\b(resume|cv|upload\s*resume)\b", re.I)),
]

SUBMIT_BUTTON_PATTERNS = re.compile(
    r"\b(submit|submit\s+application|submit\s+order|apply\s+now|complete\s+order|register|sign\s+up)\b",
    re.I
)


def match_field_to_profile(item: screen.Item, profile: Dict[str, str]) -> Optional[Tuple[str, str]]:
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


def map_form(items: List[screen.Item], profile: Optional[Dict[str, str]] = None) -> List[Tuple[screen.Item, str, str]]:
    """Scans all actionable items on the page and returns list of (item, profile_key, value)."""
    prof = profile or load_profile()
    matches: List[Tuple[screen.Item, str, str]] = []
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


def find_submit_button(items: List[screen.Item]) -> Optional[screen.Item]:
    """Finds any final submission button on the page."""
    for item in items:
        if item.kind in ("Button", "Hyperlink") and item.name and not item.offscreen:
            if SUBMIT_BUTTON_PATTERNS.search(item.name):
                return item
    return None


def fill_form_fields(mappings: List[Tuple[screen.Item, str, str]], win, hands: Hands, task_obj) -> int:
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
