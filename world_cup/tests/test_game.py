"""Game state machine tests, plus an optional live MQTT round trip.

The live test talks to the public dev broker on a PRIVATE random topic, never
the shared class topic. Skip it with:  python -m pytest tests -m "not network"
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config                                              # noqa: E402
from game import LOST, PLAYING, WAITING, WON, Game         # noqa: E402
from policy import DriveState                              # noqa: E402
from status import Status                                  # noqa: E402


class FakeRobot:
    def __init__(self):
        self.calls = []
        self.on_proximity = None
        self.proximity_enabled = False

    def set_drive(self, drive, ttl=None):
        self.calls.append(("drive", drive.speed_level))

    def halt(self):
        self.calls.append(("halt",))

    def light(self, color, pattern="SOLID"):
        self.calls.append(("light", color))

    def defend(self, side):
        self.calls.append(("defend", side))


class FakeSongs:
    def __init__(self):
        self.played = []

    def play(self, name, interrupt=True):
        self.played.append(name)


def make(role):
    sent = []
    robot, songs = FakeRobot(), FakeSongs()
    g = Game(role, Status(), robot=robot, songs=songs,
             publish=lambda topic, msg: sent.append((topic, msg)))
    return g, robot, songs, sent


def test_no_driving_before_start():
    g, robot, _, _ = make("ball")
    g.drive(DriveState(3, 0))
    assert ("drive", 3) not in robot.calls and ("halt",) in robot.calls
    g.handle_message("start")
    assert g.state == PLAYING
    g.drive(DriveState(3, 0))
    assert robot.calls[-1] == ("drive", 3)


def test_other_teams_messages_ignored():
    g, _, songs, _ = make("goalie")
    g.handle_message("start")
    for msg in ["bob_caught", "alice_scored", "hello", f"not{config.MATCH_PREFIX}_caught"]:
        g.handle_message(msg)
    assert g.state == PLAYING and songs.played == []


def test_ball_caught():
    g, robot, songs, sent = make("ball")
    assert robot.on_proximity == g.caught and robot.proximity_enabled
    g.handle_message("start")
    g.caught()
    assert g.state == LOST and songs.played == ["death"]
    assert (config.GAME_TOPIC, config.MSG_CAUGHT) in sent
    # stop happened before the message went out
    assert robot.calls.index(("halt",)) < len(robot.calls)
    # own echo of the message does nothing more
    g.handle_message(config.MSG_CAUGHT)
    assert songs.played == ["death"]
    # driving is now ignored
    g.drive(DriveState(5, 0))
    assert ("drive", 5) not in robot.calls


def test_ball_scores():
    g, _, songs, sent = make("ball")
    g.goal()                                       # before start: ignored
    assert g.state == WAITING and not songs.played
    g.handle_message("start")
    g.goal()
    assert g.state == WON and songs.played == ["victory"]
    assert (config.GAME_TOPIC, config.MSG_SCORED) in sent


def test_goalie_reacts_to_messages():
    g, robot, songs, sent = make("goalie")
    assert robot.on_proximity is None
    g.handle_message("start")
    g.goal()                                       # goalie can't score
    assert g.state == PLAYING
    g.handle_message(config.MSG_CAUGHT)
    assert g.state == WON and songs.played == ["victory"]

    g, _, songs, _ = make("goalie")
    g.handle_message("start")
    g.handle_message(config.MSG_SCORED)
    assert g.state == LOST and songs.played == ["death"]


def test_reset_and_start_rules():
    g, _, _, sent = make("ball")
    g.on_connected()
    assert (config.GAME_TOPIC, config.MSG_READY) in sent
    g.handle_message("start")
    g.caught()
    g.handle_message("start")                      # can't restart without reset
    assert g.state == LOST
    gen = g.generation
    g.handle_message(config.MSG_RESET)
    assert g.state == WAITING and g.generation > gen
    g.handle_message("start")
    assert g.state == PLAYING


@pytest.mark.network
def test_live_mqtt_round_trip(monkeypatch):
    """Ball and goalie on the dev broker, on a private random topic."""
    from mqtt_link import MqttLink
    monkeypatch.setattr(config, "GAME_TOPIC", f"ME193/Rogers/{config.MATCH_PREFIX}/test-{uuid.uuid4().hex[:8]}")

    games, links = {}, []
    for role in ("ball", "goalie"):
        st = Status()
        g = Game(role, st, robot=FakeRobot(), songs=FakeSongs())
        link = MqttLink([config.GAME_TOPIC],
                        on_message=lambda t, p, g=g: g.handle_message(p), status=st,
                        on_connect=g.on_connected, name=f"test-{role}")
        g.publish = link.publish
        link.start()
        games[role], links = g, links + [link]
    try:
        deadline = time.time() + 15
        while not all(l.connected for l in links):
            if time.time() > deadline:
                pytest.skip("broker not reachable")
            time.sleep(0.1)
        time.sleep(0.5)
        links[0].publish(config.GAME_TOPIC, "start")
        deadline = time.time() + 10
        while not all(g.state == PLAYING for g in games.values()) and time.time() < deadline:
            time.sleep(0.1)
        assert all(g.state == PLAYING for g in games.values())
        games["ball"].goal()
        deadline = time.time() + 10
        while games["goalie"].state == PLAYING and time.time() < deadline:
            time.sleep(0.1)
        assert games["ball"].state == WON and games["goalie"].state == LOST
    finally:
        for l in links:
            l.stop()


def test_game_over_sends_defense_arm_to_zero():
    for role, msg in (("goalie", config.MSG_CAUGHT), ("ball", config.MSG_SCORED)):
        g, robot, _, _ = make(role)
        g.handle_message("start")
        assert ("defend", "up") not in robot.calls
        g.handle_message(msg)
        assert g.state == WON and ("defend", "up") in robot.calls
