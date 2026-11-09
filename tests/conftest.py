"""Shared test setup: no test may minimize the real windows of the machine it runs on (actions.minimize_all, which
every real run does first since 2026-10-11)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _never_minimize_the_real_windows(monkeypatch):
    import actions
    calls = []
    monkeypatch.setattr(actions, "minimize_all", lambda: calls.append(1))
    return calls


@pytest.fixture(autouse=True)
def _retasks_in_a_temp_folder(monkeypatch, tmp_path):
    """Retasks a test run saves or finds stay out of the real data/retasks."""
    import retasks
    monkeypatch.setattr(retasks, "RETASKS_DIR", tmp_path / "retasks")
