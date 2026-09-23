"""Stretch goal: robot-side handling of drive/aux messages (no network)."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config                                              # noqa: E402
from game import PLAYING, WON, Game                        # noqa: E402
from policy import DriveState                              # noqa: E402
from remote import (RobotSideHandler, defense_msg, drive_msg, goal_msg,  # noqa: E402
                    light_msg, profile_msg, song_msg)
from status import Status                                  # noqa: E402


class FakeRobot:
    def __init__(self):
        self.drives, self.profiles, self.lights, self.halts = [], [], [], 0
        self.arm = []
        self.on_proximity, self.proximity_enabled = None, False

    def set_drive(self, drive, ttl=None):
        self.drives.append((drive.speed_level, drive.steer, ttl))

    def halt(self):
        self.halts += 1

    def set_profile(self, name):
        self.profiles.append(name)

    def light(self, color, pattern="SOLID"):
        self.lights.append(color)

    def defend(self, side):
        self.arm.append(side)


class FakeSongs:
    def __init__(self):
        self.played = []

    def play(self, name, interrupt=True):
        self.played.append(name)
        return True


def setup():
    robot, songs, st, sent = FakeRobot(), FakeSongs(), Status(), []
    game = Game("ball", st, robot=robot, songs=songs, publish=lambda t, m: sent.append(m))
    return RobotSideHandler(game, robot, songs, st), game, robot, songs, sent


def send(h, topic, msg, t=0.0):
    h.on_message(topic, json.dumps(msg) if isinstance(msg, dict) else msg, t)


def test_drive_only_after_start_and_with_ttl():
    h, game, robot, _, _ = setup()
    send(h, config.DRIVE_TOPIC, drive_msg(DriveState(2, -1), "TURN LEFT"))
    assert robot.drives == []                       # waiting for start
    send(h, config.GAME_TOPIC, "start")
    assert game.state == PLAYING
    send(h, config.DRIVE_TOPIC, drive_msg(DriveState(2, -1), "TURN LEFT"), t=1.0)
    assert robot.drives[-1] == (2, -1, config.DRIVE_LINK_TIMEOUT_S)
    assert h.last_drive_t == 1.0


def test_goal_from_drive_laptop():
    h, game, _, songs, sent = setup()
    send(h, config.GAME_TOPIC, "start")
    send(h, config.DRIVE_TOPIC, goal_msg())
    assert game.state == WON and config.MSG_SCORED in sent and songs.played == ["victory"]


def test_aux_commands():
    h, _, robot, songs, _ = setup()
    send(h, config.AUX_TOPIC, profile_msg("fast"))
    send(h, config.AUX_TOPIC, light_msg("GREEN"))
    send(h, config.AUX_TOPIC, song_msg("cheer"))
    assert robot.profiles == ["fast"] and robot.lights[-1] == "GREEN"
    assert songs.played == ["cheer"]


def test_bad_and_disallowed_messages_ignored():
    h, _, robot, songs, _ = setup()
    send(h, config.AUX_TOPIC, "not json")
    send(h, config.AUX_TOPIC, profile_msg("warp speed"))
    send(h, config.AUX_TOPIC, song_msg("victory"))      # reserved for the game
    send(h, config.AUX_TOPIC, light_msg("PLAID"))
    send(h, config.DRIVE_TOPIC, {"no_type": 1})
    assert robot.profiles == [] and songs.played == []


def test_drive_state_clamped():
    d = DriveState.from_dict({"speed_level": 99, "steer": -7})
    assert d.speed_level == config.SPEED_LEVELS and d.steer == -1


def test_defense_only_while_playing():
    h, game, robot, _, _ = setup()
    send(h, config.DEFENSE_TOPIC, defense_msg("left"))
    assert robot.arm == []                          # waiting for start
    send(h, config.GAME_TOPIC, "start")
    send(h, config.DEFENSE_TOPIC, defense_msg("left"))
    send(h, config.DEFENSE_TOPIC, defense_msg("right"))
    send(h, config.DEFENSE_TOPIC, {"type": "defense", "side": "down"})   # rejected
    assert robot.arm == ["left", "right"]
