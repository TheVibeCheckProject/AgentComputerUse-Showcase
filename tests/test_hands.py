"""Tests for human-like mouse physics, Bézier trajectories, and click dwell times."""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hands import Guard, Hands, bezier_trajectory, fitts_duration  # noqa: E402


def test_bezier_trajectory_reaches_destination_precisely():
    start = (100, 150)
    end = (800, 600)
    traj = bezier_trajectory(start, end)
    assert len(traj) >= 10
    assert traj[-1] == end


def test_bezier_trajectory_short_distance():
    # Very short distance (< 3px) returns destination directly
    start = (100, 100)
    end = (101, 102)
    traj = bezier_trajectory(start, end)
    assert traj == [end]


def test_bezier_trajectory_has_natural_curvature():
    # A straight line has cross product 0 for all intermediate points.
    # An organic curved path must have non-zero lateral deviation.
    start = (100, 100)
    end = (1000, 800)
    dx, dy = end[0] - start[0], end[1] - start[1]
    dist = math.hypot(dx, dy)

    has_curvature = False
    for _ in range(5):
        traj = bezier_trajectory(start, end, deviation_scale=0.25)
        # Check max perpendicular deviation from straight line connecting start and end
        max_dev = 0.0
        for px, py in traj[1:-1]:
            # Distance from point (px, py) to line through start and end: |dx*(y0-py) - (x0-px)*dy| / dist
            d_line = abs(dx * (start[1] - py) - (start[0] - px) * dy) / dist
            if d_line > max_dev:
                max_dev = d_line
        if max_dev > 5.0:
            has_curvature = True
            break
    assert has_curvature, "Bézier trajectory should exhibit non-zero curvature"


def test_fitts_duration_scaling():
    short_d = fitts_duration(5.0)
    med_d = fitts_duration(250.0)
    long_d = fitts_duration(1600.0)

    assert 0.10 <= short_d <= 0.20
    assert 0.15 <= med_d <= 0.50
    assert 0.25 <= long_d <= 0.65
    assert short_d < med_d <= long_d


class MockMouse:
    def __init__(self, start_pos=(100, 100)):
        self.position = start_pos
        self.events = []

    def press(self, button):
        self.events.append(("press", button, self.position))

    def release(self, button):
        self.events.append(("release", button, self.position))


def test_hands_move_and_click_with_mock_mouse():
    mouse = MockMouse(start_pos=(50, 50))
    guard = Guard(seconds=10, mouse=mouse)
    hands = Hands(guard=guard, dry_run=False)
    hands.mouse = mouse

    # Perform move with fast explicit seconds override
    hands.move(300, 200, seconds=0.05)
    assert mouse.position == (300, 200)

    # Perform click
    hands.click(400, 300)
    assert mouse.position == (400, 300)
    # Check press and release were triggered in order
    types = [ev[0] for ev in mouse.events]
    assert "press" in types
    assert "release" in types
    assert types.index("press") < types.index("release")
