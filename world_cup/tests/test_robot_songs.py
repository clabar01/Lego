"""Offline tests for robot helpers, the controller thread (simulated), and songs."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config                                                    # noqa: E402
from policy import DriveState                                    # noqa: E402
from robot import (ProximityDetector, RobotController, SimRobot,  # noqa: E402
                   level_to_speed, wheel_speeds)
from songs import SONGS, duration, note_hz, synthesize          # noqa: E402
from status import Status                                        # noqa: E402


def test_level_to_speed_range():
    for prof, p in config.PROFILES.items():
        assert level_to_speed(0, prof) == 0
        assert level_to_speed(1, prof) == config.MIN_DRIVE_SPEED
        assert level_to_speed(config.SPEED_LEVELS, prof) == p["max_speed"]


def test_wheel_speeds():
    assert wheel_speeds(DriveState(0, 0), "medium") == (0, 0)
    l, r = wheel_speeds(DriveState(3, 0), "medium")
    assert l == r > 0
    l, r = wheel_speeds(DriveState(3, -1), "medium")
    assert l < r                                    # left turn: left wheel slower
    l, r = wheel_speeds(DriveState(3, 1), "medium")
    assert l > r
    l, r = wheel_speeds(DriveState(0, 1), "medium")  # pivot in place
    assert l == -r and l > 0
    # a faster profile really is faster
    assert wheel_speeds(DriveState(5, 0), "fast")[0] > wheel_speeds(DriveState(5, 0), "slow")[0]


def test_proximity_baseline_and_debounce():
    p = ProximityDetector()
    for _ in range(config.PROXIMITY_BASELINE_SAMPLES):
        assert not p.update(5)
    assert p.threshold == max(5 + config.PROXIMITY_DELTA, config.PROXIMITY_MIN_ABS)
    assert not p.update(90)                          # one spike is not enough
    assert not p.update(5)
    hits = [p.update(90) for _ in range(config.PROXIMITY_HOLD_FRAMES + 2)]
    assert hits.count(True) == 1                     # trips once, on the edge
    assert p.close


def test_controller_drives_and_halts_with_sim():
    sim = SimRobot()
    ctl = RobotController(sim, Status(), profile="medium")
    tripped = []
    ctl.on_proximity = lambda: (tripped.append(1), ctl.halt())
    ctl.proximity_enabled = True
    ctl.start()
    try:
        ctl.set_drive(DriveState(2, 0))
        time.sleep(0.3)
        assert sim._last == wheel_speeds(DriveState(2, 0), "medium")
        time.sleep(config.PROXIMITY_BASELINE_SAMPLES / config.ROBOT_LOOP_HZ + 0.2)
        sim.obstacle = True
        time.sleep(0.4)
        assert tripped and sim._last == (0, 0)
        # stays stopped even though the target was driving before
        time.sleep(0.2)
        assert sim._last == (0, 0)
    finally:
        ctl.stop()


def test_controller_ttl_stops_when_link_goes_quiet():
    sim = SimRobot(use_sensor=False)
    ctl = RobotController(sim, Status())
    ctl.start()
    try:
        ctl.set_drive(DriveState(3, 0), ttl=0.3)
        time.sleep(0.2)
        assert sim._last != (0, 0)
        time.sleep(0.4)
        assert sim._last == (0, 0)
    finally:
        ctl.stop()


def test_notes_and_songs():
    assert abs(note_hz("A4") - 440) < 1e-6
    assert abs(note_hz("C5") - 523.25) < 0.01
    for name in SONGS:
        data = synthesize(name, 22050)
        assert abs(len(data) / 22050 - duration(name)) < 0.05
        assert data.dtype.name == "int16" and abs(data).max() > 1000
        # songs stay under the hub's 2700 Hz beep limit
        assert all(note_hz(n) < 2700 for n, _ in SONGS[name])
