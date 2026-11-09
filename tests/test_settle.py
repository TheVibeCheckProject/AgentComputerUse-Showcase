"""Tests for perceptual frame settling and dynamic visual stabilization."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import screen


def test_compute_frame_diff_identical():
    img1 = Image.new("RGB", (200, 200), color=(128, 128, 128))
    img2 = Image.new("RGB", (200, 200), color=(128, 128, 128))
    delta = screen.compute_frame_diff(img1, img2)
    assert delta == 0.0


def test_compute_frame_diff_contrasting():
    black = Image.new("RGB", (200, 200), color=(0, 0, 0))
    white = Image.new("RGB", (200, 200), color=(255, 255, 255))
    delta = screen.compute_frame_diff(black, white)
    assert pytest.approx(delta, 0.01) == 1.0


def test_compute_frame_diff_subtle_change():
    img1 = Image.new("RGB", (200, 200), color=(100, 100, 100))
    # Small box changed
    img2 = img1.copy()
    img2.paste((200, 200, 200), (20, 20, 40, 40))
    delta = screen.compute_frame_diff(img1, img2)
    assert 0.0 < delta < 0.2


def test_wait_settled_resolves_when_still():
    static_frame = Image.new("RGB", (100, 100), color=(50, 50, 50))
    with patch("ocr.grab", return_value=(static_frame, (0, 0))):
        delta = screen.wait_settled(win=None, timeout=1.0, poll_interval=0.05, threshold=0.02)
        assert delta == 0.0


def test_wait_settled_detects_animation_until_settled():
    frames = [
        Image.new("RGB", (100, 100), color=(0, 0, 0)),
        Image.new("RGB", (100, 100), color=(100, 100, 100)),
        Image.new("RGB", (100, 100), color=(200, 200, 200)),
        Image.new("RGB", (100, 100), color=(200, 200, 200)),  # Settled here!
    ]
    idx = [0]

    def mock_grab(rect=None):
        f = frames[min(idx[0], len(frames) - 1)]
        idx[0] += 1
        return (f, (0, 0))

    with patch("ocr.grab", side_effect=mock_grab):
        delta = screen.wait_settled(win=None, timeout=1.5, poll_interval=0.04, threshold=0.015)
        assert delta == 0.0
        assert idx[0] >= 4


def test_wait_settled_failsafe_on_exception():
    with patch("ocr.grab", side_effect=RuntimeError("no desktop session")):
        # Should return 0.0 gracefully instead of crashing
        delta = screen.wait_settled(win=None)
        assert delta == 0.0
