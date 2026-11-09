"""Tests for domain-locked Credential Manager."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import credentials


def test_extract_hostname():
    assert credentials.extract_hostname("https://accounts.google.com/signin") == "accounts.google.com"
    assert credentials.extract_hostname("http://github.com:8080/login") == "github.com"
    assert credentials.extract_hostname("example.com/path?query=1") == "example.com"
    assert credentials.extract_hostname("") == ""


def test_matches_domain_strict():
    # Exact and subdomains match
    assert credentials.matches_domain("https://google.com/login", "google.com") is True
    assert credentials.matches_domain("https://accounts.google.com/v2", "google.com") is True
    assert credentials.matches_domain("https://sub.accounts.google.com", "google.com") is True

    # Lookalikes and phishing do NOT match
    assert credentials.matches_domain("https://evilgoogle.com/login", "google.com") is False
    assert credentials.matches_domain("https://google.com.attacker.com/v2", "google.com") is False
    assert credentials.matches_domain("https://fake-google.com", "google.com") is False
    assert credentials.matches_domain("", "google.com") is False


def test_get_credential_domain_mismatch_raises():
    with pytest.raises(credentials.DomainMismatchError, match="Security alert"):
        credentials.get_credential("google.com", "https://evilgoogle.com/signin")


def test_store_and_retrieve_credential():
    mock_vault = {}

    def mock_set_password(svc, key, pw):
        mock_vault[(svc, key)] = pw

    def mock_get_password(svc, key):
        return mock_vault.get((svc, key))

    with patch("keyring.set_password", side_effect=mock_set_password), \
         patch("keyring.get_password", side_effect=mock_get_password), \
         patch("credentials.list_credentials", return_value=[("github.com", "myuser")]):

        credentials.save_credential("github.com", "myuser", "SuperSecret123!")

        # Valid domain
        found = credentials.get_credential("github.com", "https://github.com/login")
        assert found is not None
        user, pw = found
        assert user == "myuser"
        assert pw == "SuperSecret123!"

        # Invalid domain raises error
        with pytest.raises(credentials.DomainMismatchError):
            credentials.get_credential("github.com", "https://github.com.phish.com/login")


def _saved(monkeypatch, entries):
    """entries: {(domain, user): password} in a fake Credential Manager and index."""
    import types
    fake = types.SimpleNamespace(get_password=lambda svc, key: entries.get(tuple(key.split("::", 1))))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(credentials, "list_credentials", lambda: list(entries))


def test_a_password_saved_for_the_site_is_found_on_its_sign_in_subdomain(monkeypatch):
    # 2026-10-08: only the exact host was looked up, so google.com's password never showed on accounts.google.com.
    _saved(monkeypatch, {("google.com", "me@example.com"): "pw1"})
    assert credentials.find_for_url("https://accounts.google.com/signin") == ("google.com", "me@example.com", "pw1")


def test_the_most_specific_saved_domain_wins(monkeypatch):
    _saved(monkeypatch, {("google.com", "a"): "broad", ("accounts.google.com", "b"): "specific"})
    found = credentials.find_for_url("https://accounts.google.com/x")
    assert found is not None and found[2] == "specific"


def test_look_alike_sites_find_nothing(monkeypatch):
    _saved(monkeypatch, {("google.com", "a"): "pw"})
    for url in ("https://google.com.evil.net/login", "https://evilgoogle.com/", "https://com/", ""):
        assert credentials.find_for_url(url) is None


# ------------------------------------------------------------------ the saved password inside the loop
import actions
import loop
import screen
import task as tasks
from hands import Hands, Stopped


def _signin(monkeypatch, typer):
    lp = loop.Loop("sign in", Hands(dry_run=True))
    monkeypatch.setattr(lp, "_url", lambda: "https://accounts.google.com/signin")
    monkeypatch.setattr(credentials, "find_for_url", lambda url: ("google.com", "me", "pw-secret"))
    monkeypatch.setattr(tasks, "check_may_type_here", lambda t, w: None)
    monkeypatch.setattr(actions, "type_into", typer)
    return lp, screen.Item(1, "Edit", "Enter your password", (0, 0, 10, 10))


def test_the_saved_password_is_typed_once_per_site(monkeypatch):
    typed = []
    lp, field = _signin(monkeypatch, lambda item, text, win, hands, secret=False: typed.append((text, secret)))
    assert lp._fill_saved_password(field)
    assert typed == [("pw-secret", True)]
    assert "pw-secret" not in str(lp.history)
    # the site turned it down and asks again: the owner is asked this time, not the same password again
    assert lp._fill_saved_password(field) is None
    assert len(typed) == 1


def test_the_kill_switch_while_typing_a_saved_password_stops_the_run(monkeypatch):
    # 2026-10-08: `except Exception: pass` swallowed it and the run went on to ask the owner.
    def killed(*a, **k):
        raise Stopped("the kill switch (Ctrl+Alt+End) was pressed")
    lp, field = _signin(monkeypatch, killed)
    with pytest.raises(Stopped):
        lp._fill_saved_password(field)
