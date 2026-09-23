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


def test_backward_runs_both_wheels_in_reverse():
    from robot import wheel_speeds
    left, right = wheel_speeds(DriveState(-1, 0), "medium")
    assert left == right == -config.MIN_DRIVE_SPEED


def test_victory_and_death_also_beep_on_robot(monkeypatch):
    from songs import SongPlayer
    hub = []
    monkeypatch.setattr(SongPlayer, "_play_laptop", lambda self, song: None)
    monkeypatch.setattr(SongPlayer, "_play_hub", lambda self, song: hub.append(song))
    player = SongPlayer(hub_beep=lambda hz: None)
    for song in ("victory", "death", "cheer"):
        player.play(song)
    time.sleep(0.1)
    assert sorted(hub) == ["death", "victory"]


# ---------------------------------------------------------------- defense arm

def _in_forbidden_zone(angle_from_up_deg):
    """Bottom third: within DEFENSE_FORBIDDEN_DEG/2 of straight down."""
    from_down = abs((angle_from_up_deg % 360) - 180)
    return from_down < config.DEFENSE_FORBIDDEN_DEG / 2


def test_defense_targets_and_swing_path_avoid_bottom_third():
    from robot import defense_target
    left, right = defense_target("left"), defense_target("right")
    assert left == -right and defense_target("up") == 0
    # The relative counter doesn't wrap, so a swing visits every angle
    # between the two targets - through the top, never through the bottom.
    lo, hi = min(left, right), max(left, right)
    assert not any(_in_forbidden_zone(a) for a in range(lo, hi + 1))
    assert _in_forbidden_zone(180)                     # sanity check of the helper


def test_angle_from_up_wraps_to_signed_degrees():
    from robot import angle_from_up
    assert angle_from_up(0, up=0) == 0
    assert angle_from_up(350, up=0) == -10
    assert angle_from_up(10, up=350) == 20
    assert angle_from_up(180, up=0) == -180


def test_controller_moves_sim_arm(capsys):
    sim = SimRobot(use_sensor=False, use_defense=True)
    ctl = RobotController(sim, Status())
    ctl.start()
    try:
        ctl.defend("left")
        time.sleep(0.2)
    finally:
        ctl.stop()
    from robot import defense_target
    assert f"defense arm -> {defense_target('left'):+d}" in capsys.readouterr().out
