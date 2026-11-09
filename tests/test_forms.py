"""Tests for profile store and single-pass form automation."""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import form_filler
import profile_store
import screen
from hands import Hands
from task import Task


def test_profile_store_defaults_and_override(tmp_path):
    p_file = tmp_path / "test_profile.json"
    p = profile_store.load_profile(p_file)
    assert p["first_name"] == ""
    assert p["country"] == ""

    # Save custom data
    custom = {"first_name": "Test", "email": "test@example.com", "phone": "555-1234"}
    profile_store.save_profile(custom, p_file)

    reloaded = profile_store.load_profile(p_file)
    assert reloaded["first_name"] == "Test"
    assert reloaded["email"] == "test@example.com"
    assert reloaded["phone"] == "555-1234"


def test_form_mapper_identifies_fields():
    custom_profile = {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane.doe@example.com",
        "phone": "555-0100",
        "zip": "90210",
    }
    items = [
        screen.Item(1, "Edit", "First Name", (100, 100, 200, 130), False, "uia", control=object()),
        screen.Item(2, "Edit", "Last Name", (220, 100, 320, 130), False, "uia", control=object()),
        screen.Item(3, "Edit", "Email Address", (100, 150, 320, 180), False, "uia", control=object()),
        screen.Item(4, "Edit", "Phone Number", (100, 200, 320, 230), False, "uia", control=object()),
        screen.Item(5, "Edit", "ZIP Code", (100, 250, 200, 280), False, "uia", control=object()),
        screen.Item(6, "Button", "Submit Application", (100, 320, 240, 360), False, "uia", control=object()),
    ]

    mapped = form_filler.map_form(items, profile=custom_profile)
    assert len(mapped) == 5

    keys = [m[1] for m in mapped]
    values = [m[2] for m in mapped]
    assert keys == ["first_name", "last_name", "email", "phone", "zip"]
    assert values == ["Jane", "Doe", "jane.doe@example.com", "555-0100", "90210"]


def test_find_submit_button():
    items = [
        screen.Item(1, "Edit", "First Name", (100, 100, 200, 130), False, "uia", control=object()),
        screen.Item(2, "Button", "Submit Application", (100, 320, 240, 360), False, "uia", control=object()),
    ]
    btn = form_filler.find_submit_button(items)
    assert btn is not None
    assert btn.id == 2
    assert btn.name == "Submit Application"


def test_fill_form_fields_dry_run():
    class DummyWindow:
        Name = "Notepad"
        NativeWindowHandle = 9999

    task_obj = Task("fill form")
    task_obj.opened(DummyWindow)

    items = [
        screen.Item(1, "Edit", "First Name", (100, 100, 200, 130), False, "uia", control=object()),
        screen.Item(2, "Edit", "Email", (100, 150, 320, 180), False, "uia", control=object()),
    ]
    mappings = [
        (items[0], "first_name", "Jane"),
        (items[1], "email", "jane.doe@example.com"),
    ]

    hands = Hands(dry_run=True)
    with patch("actions.bring_to_front"):
        count = form_filler.fill_form_fields(mappings, DummyWindow, hands, task_obj)
    assert count == 2
