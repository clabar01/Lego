"""
Stretch goal: split control of ONE robot across TWO laptops over MQTT.

    Laptop 1 (--mode drive)  whistles -> DRIVE state   -> ME193/Rogers/<prefix>/drive
    Laptop 2 (--mode aux)    whistles -> songs / light / speed profile
                                                         -> ME193/Rogers/<prefix>/aux
    Robot laptop (--mode robot)  subscribes to both + the game topic, owns the
                                 Bluetooth connection, runs the game logic.

Message format: small JSON objects.

  drive topic
    {"type": "drive", "speed_level": 0-5, "steer": -1|0|1, "decision": "TURN LEFT"}
        sent DRIVE_PUBLISH_HZ times per second (QoS 0). It is the full state,
        not a delta, so a lost message is simply replaced by the next one. The
        stream doubles as a heartbeat: if the robot hears nothing for
        DRIVE_LINK_TIMEOUT_S it stops the motors.
    {"type": "goal"}                      the goal whistle (QoS 1)

  aux topic
    {"type": "profile", "value": "slow"|"medium"|"fast"}   (retained, so a robot
                                          that restarts picks up the last choice)
    {"type": "light", "color": "GREEN"}   hub light color
    {"type": "song", "name": "cheer"}     play a song on the robot laptop
"""

import json

import config
from policy import DriveState

SONGS_ALLOWED_FROM_AUX = ("cheer",)     # victory/death stay reserved for the game


def drive_msg(drive, decision):
    return {"type": "drive", "speed_level": drive.speed_level, "steer": drive.steer,
            "decision": decision}


def goal_msg():
    return {"type": "goal"}


def profile_msg(name):
    return {"type": "profile", "value": name}


def light_msg(color):
    return {"type": "light", "color": color}


def song_msg(name):
    return {"type": "song", "name": name}


def parse(payload):
    """JSON payload -> dict, or None if it isn't one of ours."""
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return msg if isinstance(msg, dict) and "type" in msg else None


class RobotSideHandler:
    """Executes drive/aux messages on the robot laptop."""

    def __init__(self, game, robot, songs, status):
        self.game, self.robot, self.songs, self.status = game, robot, songs, status
        self.last_drive_t = None

    def on_message(self, topic, payload, now):
        if topic == config.GAME_TOPIC:
            self.game.handle_message(payload)
            return
        msg = parse(payload)
        if msg is None:
            return
        kind = msg["type"]
        if topic == config.DRIVE_TOPIC:
            if kind == "drive":
                self.last_drive_t = now
                drive = DriveState.from_dict(msg)
                # ttl: if the drive laptop goes quiet, the robot thread stops the car.
                self.game.drive(drive, ttl=config.DRIVE_LINK_TIMEOUT_S)
                self.status.set(decision=str(msg.get("decision", "")),
                                speed_level=drive.speed_level, steer=drive.steer,
                                detail="from drive laptop")
            elif kind == "goal":
                self.status.log("goal whistle from drive laptop")
                self.game.goal()
        elif topic == config.AUX_TOPIC:
            if kind == "profile" and msg.get("value") in config.PROFILES:
                self.robot.set_profile(msg["value"])
                self.status.log(f"aux: profile {msg['value']}")
            elif kind == "light" and msg.get("color") in config.HUB_LIGHT_COLORS:
                self.robot.light(msg["color"])
                self.status.log(f"aux: light {msg['color']}")
            elif kind == "song" and msg.get("name") in SONGS_ALLOWED_FROM_AUX:
                # A game-over song already playing is never cut off by aux.
                if not self.songs.play(msg["name"], interrupt=False):
                    self.status.log("aux: song ignored (another song is playing)")
