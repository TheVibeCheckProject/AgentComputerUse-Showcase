"""Tests for self-healing element matching, fuzzy labels, and spatial anchors."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import actions  # noqa: E402
import screen  # noqa: E402


def make_item(id_val, kind, name, rect, value=None):
    return screen.Item(
        id=id_val,
        kind=kind,
        name=name,
        rect=rect,
        offscreen=False,
        source="uia",
        value=value,
        control=object()
    )


def test_find_matching_item_exact_match():
    target = make_item(1, "Button", "Submit Application", (100, 200, 200, 240))
    candidates = [
        make_item(2, "Button", "Cancel", (300, 200, 380, 240)),
        make_item(3, "Button", "Submit Application", (105, 202, 205, 242)),
    ]
    matched = actions.find_matching_item(target, candidates)
    assert matched is not None
    assert matched.id == 3


def test_find_matching_item_fuzzy_text_match():
    # E.g. web page changed text slightly: "Search Google" -> "Search Google or type a URL"
    target = make_item(1, "Button", "Search Google", (100, 100, 220, 140))
    candidates = [
        make_item(2, "Button", "I'm Feeling Lucky", (240, 100, 360, 140)),
        make_item(3, "Button", "Search Google or type", (102, 102, 230, 142)),  # >0.80 sim
    ]
    matched = actions.find_matching_item(target, candidates)
    assert matched is not None
    assert matched.id == 3


def test_find_matching_item_spatial_anchor_fallback():
    # Rebuilt edit field with blank or changed label at nearly the exact same coordinates
    target = make_item(1, "Edit", "Customer Name", (150, 300, 350, 330))
    candidates = [
        make_item(2, "Button", "Submit", (150, 400, 250, 430)),
        # Edit field at essentially same position (center within 10px), but name was stripped or changed
        make_item(3, "Edit", "Enter full name...", (152, 302, 352, 332)),
    ]
    matched = actions.find_matching_item(target, candidates)
    assert matched is not None
    assert matched.id == 3


def test_find_matching_item_no_match():
    target = make_item(1, "Button", "Checkout", (100, 100, 180, 140))
    candidates = [
        make_item(2, "Hyperlink", "Home", (10, 10, 50, 30)),
        make_item(3, "Button", "Settings", (800, 800, 880, 840)),
    ]
    matched = actions.find_matching_item(target, candidates)
    assert matched is None


def test_a_vanished_follow_button_is_never_its_unfollow():
    # 2026-10-08: the fuzzy match (0.86) turned "Follow" into "Unfollow".
    target = make_item(1, "Button", "Follow", (100, 100, 160, 130))
    assert actions.find_matching_item(target, [make_item(2, "Button", "Unfollow", (100, 100, 170, 130))]) is None
    target = make_item(1, "Button", "Subscribe", (100, 100, 180, 130))
    assert actions.find_matching_item(target, [make_item(2, "Button", "Unsubscribe", (100, 100, 190, 130))]) is None


def test_a_button_is_never_found_by_position_alone():
    # 2026-10-08: "Archive" gone, the "Delete" 30 px away was taken for it.
    target = make_item(1, "Button", "Archive", (100, 100, 160, 130))
    assert actions.find_matching_item(target, [make_item(2, "Button", "Delete", (130, 100, 190, 130))]) is None


def test_two_buttons_that_both_extend_the_label_are_ambiguous():
    target = make_item(1, "Button", "Save", (100, 100, 160, 130))
    candidates = [make_item(2, "Button", "Save draft", (100, 100, 170, 130)),
                  make_item(3, "Button", "Save and send", (130, 100, 200, 130))]
    assert actions.find_matching_item(target, candidates) is None


def test_case_and_spacing_dont_matter_for_the_same_label():
    target = make_item(1, "Button", "Sign  in", (100, 100, 160, 130))
    matched = actions.find_matching_item(target, [make_item(2, "Button", "Sign In", (300, 300, 360, 330))])
    assert matched is not None and matched.id == 2


def test_an_unlabelled_button_can_be_found_where_it_was_if_its_the_only_one():
    target = make_item(1, "Button", "", (100, 100, 130, 130))
    one = [make_item(2, "Button", "", (102, 101, 132, 131))]
    assert actions.find_matching_item(target, one).id == 2
    two = one + [make_item(3, "Button", "", (110, 100, 140, 130))]
    assert actions.find_matching_item(target, two) is None


def test_a_vanished_delete_is_not_a_delete_account_button_across_the_page():
    target = make_item(1, "Button", "Delete", (100, 100, 160, 130))
    far = make_item(2, "Button", "Delete account", (600, 700, 720, 730))
    assert actions.find_matching_item(target, [far]) is None
