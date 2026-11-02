"""Understand before acting: the task only types into and saves what it made (fake windows, no real input)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import task as tasks  # noqa: E402


class FakeWin:
    def __init__(self, handle, name="Notepad"):
        self.NativeWindowHandle = handle
        self.Name = name


@pytest.fixture
def tab(monkeypatch):
    state = {"tab": None, "id": None}
    monkeypatch.setattr(tasks, "selected_tab", lambda win: (state["tab"], state["id"]))
    return state


def test_the_owners_file_is_look_only(tab):
    """The 2026-10-05 case: Notepad came up on the owner's 'New Text Document.txt'."""
    t = tasks.Task("demo")
    tab["tab"] = "New Text Document.txt. Unmodified."
    with pytest.raises(tasks.NotMine, match="look-only"):
        tasks.check_may_type_here(t, FakeWin(1))
    with pytest.raises(tasks.NotMine, match="overwrite"):
        tasks.check_may_save(t, FakeWin(1))


def test_its_own_untitled_page_may_be_typed_into_and_saved(tab):
    t = tasks.Task("demo")
    t.tabs.add("Untitled")
    tab["tab"] = "Untitled. Modified."
    tasks.check_may_type_here(t, FakeWin(1))
    tasks.check_may_save(t, FakeWin(1))


def test_someone_elses_untitled_page_is_not_its_own(tab):
    t = tasks.Task("demo")                      # it never made an Untitled page
    tab["tab"] = "Untitled. Modified."
    with pytest.raises(tasks.NotMine):
        tasks.check_may_save(t, FakeWin(1))


def test_a_file_the_owner_names_may_be_changed(tab):
    t = tasks.Task("update my notes", allowed={"notes"})
    tab["tab"] = "notes.txt. Modified."
    tasks.check_may_type_here(t, FakeWin(1))
    tasks.check_may_save(t, FakeWin(1))


def test_windows_it_opened_are_its_own(tab):
    t = tasks.Task("demo")
    w = FakeWin(42, "about:blank - Edge")
    tab["tab"] = None                           # not Notepad: no tabs
    with pytest.raises(tasks.NotMine):
        tasks.check_may_type_here(t, w)
    t.opened(w)
    tasks.check_may_type_here(t, w)


def test_an_unexpected_screen_stops_the_step():
    t = tasks.Task("demo")
    with pytest.raises(tasks.Unexpected, match="a Save dialog"):
        t.expect(lambda: False, "a Save dialog", timeout=0.4)
    t.expect(lambda: True, "anything")


def test_its_own_tab_is_still_its_own_after_notepad_renames_it(tab):
    """2026-10-05: typing renamed 'Untitled' to the first line ('Search weather in Springfield Arkansa'), and
    the save was refused. The tab itself (its id) is what counts."""
    t = tasks.Task("demo")
    t.tab_ids.add((42, 7))
    tab["tab"], tab["id"] = "Search weather in Springfield Arkansa. Modified.", (42, 7)
    tasks.check_may_type_here(t, FakeWin(1))
    tasks.check_may_save(t, FakeWin(1))
    tab["id"] = (42, 8)                       # a different tab with the same name: not its own
    with pytest.raises(tasks.NotMine):
        tasks.check_may_save(t, FakeWin(1))
