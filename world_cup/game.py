"""
World Cup game logic: the state machine that decides when the robot may drive
and what happens when the game ends.

    WAITING FOR START --"start"--> PLAYING --+--> WON
                                             +--> LOST
          ^                                  |
          +---------- reset (key r / "<prefix>_reset") ----+

Ball role
    - light sensor sees the goalie close in front -> stop motors immediately,
      publish "<prefix>_caught", play the death song             => LOST
    - GOAL whistle (two short high whistles) -> publish "<prefix>_scored",
      play the victory song                                        => WON
Goalie role
    - hears "<prefix>_caught" -> victory song                      => WON
    - hears "<prefix>_scored" -> death song                        => LOST

Driving commands are only passed to the robot while PLAYING. Before "start"
and after the game ends, the motors are held stopped and whistles are shown
on screen but ignored, until a reset.

Mirror mode (stretch goal, the drive/aux laptops): no robot, no songs, never
publishes; it just follows the game messages so the laptop's display shows
the right game state.
"""

import threading

import config

WAITING, PLAYING, WON, LOST = "WAITING FOR START", "PLAYING", "WON", "LOST"

STATE_LIGHTS = {                  # hub light for each state: (color, pattern)
    WAITING: ("YELLOW", "BREATHE"),
    PLAYING: ("BLUE", "SOLID"),
    WON: ("GREEN", "DOUBLE_BLINK"),
    LOST: ("RED", "SHORT_BLINK"),
}


class Game:
    def __init__(self, role, status, robot=None, songs=None, publish=None, mirror=False):
        """
        role     "ball" or "goalie"
        robot    RobotController or None
        songs    SongPlayer or None
        publish  callable(topic, payload) or None (offline)
        mirror   True on laptops that only DISPLAY the game (stretch goal)
        """
        assert role in ("ball", "goalie")
        self.role = role
        self.status = status
        self.robot = robot
        self.songs = songs
        self.publish = publish
        self.mirror = mirror
        self.state = WAITING
        self.generation = 0           # bumps on start/reset; the audio thread
                                      # resets its policy when it sees a change
        self._lock = threading.RLock()
        status.set(game=self.state, role=role)
        if robot and role == "ball":
            robot.on_proximity = self.caught
            robot.proximity_enabled = True
        self._light()

    # ------------------------------------------------------------ MQTT input
    def on_connected(self):
        """Every (re)connect: announce we're ready if still waiting."""
        if self.state == WAITING:
            self._send(config.MSG_READY)

    def handle_message(self, payload):
        """A message on GAME_TOPIC. Anything not for our match is ignored."""
        msg = payload.strip()
        if msg in (config.MSG_START, f"{config.MATCH_PREFIX}_start"):
            self.start()
        elif msg == config.MSG_RESET:
            self.reset()
        elif msg == config.MSG_CAUGHT:
            # Goalie wins when the ball is caught. (The ball already ended the
            # game itself when it published this, so its own echo is ignored.)
            self._end(WON if self.role == "goalie" else LOST, f"heard {msg}")
        elif msg == config.MSG_SCORED:
            self._end(LOST if self.role == "goalie" else WON, f"heard {msg}")
        elif msg == config.MSG_READY:
            self.status.log(f"heard {msg}")

    # ---------------------------------------------------------- game events
    def start(self):
        with self._lock:
            if self.state != WAITING:
                self.status.log(f"'start' ignored - game is {self.state} (press r to reset)")
                return
            self.state = PLAYING
            self.generation += 1
        self.status.set(game=self.state)
        self.status.log("GAME STARTED - whistle control enabled")
        self._light()

    def reset(self):
        with self._lock:
            self.state = WAITING
            self.generation += 1
        if self.robot:
            self.robot.halt()
        self.status.set(game=self.state)
        self.status.log("reset - waiting for start")
        self._light()
        self._send(config.MSG_READY)

    def drive(self, drive, ttl=None):
        """Pass a drive state to the robot, but ONLY while playing."""
        if self.robot is None:
            return
        if self.state == PLAYING:
            self.robot.set_drive(drive, ttl=ttl)
        else:
            self.robot.halt()

    def goal(self):
        """GOAL whistle heard. Only the ball can score."""
        if self.role != "ball":
            self.status.log("goal whistle ignored (we are the goalie)")
            return
        if self.state != PLAYING:
            self.status.log(f"goal whistle ignored - game is {self.state}")
            return
        self._send(config.MSG_SCORED)
        self._end(WON, "GOAL! we scored")

    def caught(self):
        """Light sensor: goalie right in front of the ball (robot thread)."""
        if self.role != "ball" or self.state != PLAYING:
            return
        if self.robot:
            self.robot.halt()                    # stop FIRST, then tell everyone
        self._send(config.MSG_CAUGHT)
        self._end(LOST, "caught by the goalie")

    # -------------------------------------------------------------- helpers
    def _end(self, result, why):
        with self._lock:
            if self.state != PLAYING:
                return                           # already over (or never started)
            self.state = result
        if self.robot:
            self.robot.halt()
        self.status.set(game=result)
        self.status.log(f"GAME OVER: {result} ({why})")
        if self.songs:
            self.songs.play("victory" if result == WON else "death")
        self._light()

    def _send(self, msg):
        if self.publish and not self.mirror:
            self.publish(config.GAME_TOPIC, msg)
            self.status.log(f"sent '{msg}'")

    def _light(self):
        if self.robot:
            self.robot.light(*STATE_LIGHTS[self.state])
