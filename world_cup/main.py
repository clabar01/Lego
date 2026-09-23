"""
ME193 World Cup - whistle-controlled LEGO robot.

Examples
    python main.py --list-devices          # show microphones / speakers
    python main.py --calibrate             # whistle and see the detected pitch
    python main.py --device 1              # whistle control, live display
    python main.py --sim                   # same, with a simulated robot
    python main.py --role goalie           # play as the goalie
    python main.py --sim --no-mqtt         # offline test: press s to start

Stretch goal (two laptops, one robot):
    python main.py --mode robot --role ball    # laptop connected to the robot
    python main.py --mode drive --device 1     # laptop 1: whistles drive
    python main.py --mode aux --device 0       # laptop 2: songs, light, speed profile
    python main.py --mode defense --device 0   # laptop 2 instead: whistles swing the defense arm

Keys in the display window are listed at the bottom of the window.
"""

import argparse
import sys
import threading
import time

import config
from audio import AudioEngine, print_devices
from calibrate import Calibrator
from display import Display, keyhelp
from game import Game
from mqtt_link import MqttLink
from policy import AuxPolicy, Debouncer, DrivePolicy
from remote import (RobotSideHandler, defense_msg, drive_msg, goal_msg, light_msg,
                    profile_msg, song_msg)
from robot import LegoHardware, RobotController, SimRobot
from songs import SongPlayer
from status import Status


def build_parser():
    p = argparse.ArgumentParser(description="Whistle-controlled LEGO robot (ME193 World Cup)")
    p.add_argument("--list-devices", action="store_true",
                   help="list audio input/output devices and exit")
    p.add_argument("--device", type=int, default=None,
                   help="audio input device index (default: system default mic)")
    p.add_argument("--mode", choices=["single", "robot", "drive", "aux", "defense"],
                   default="single",
                   help="single = one laptop does everything (default). Stretch goal: "
                        "robot = laptop connected to the robot, drive = whistles drive "
                        "it over MQTT, aux = whistles control songs/light/speed profile, "
                        "defense = whistles swing the defense arm")
    p.add_argument("--role", choices=["ball", "goalie"], default="ball",
                   help="which robot we are in the match (default %(default)s)")
    p.add_argument("--calibrate", action="store_true",
                   help="calibration mode: whistle and see the detected frequency")
    p.add_argument("--noise-db", type=float, default=None,
                   help="fixed volume threshold in dBFS (skips ambient-noise calibration)")
    p.add_argument("--noise-margin", type=float, default=None,
                   help=f"dB above ambient noise a whistle must reach (default "
                        f"{config.NOISE_MARGIN_DB:g}). Raise it so a laptop ignores "
                        f"whistlers further away (2-laptop mode)")
    p.add_argument("--no-display", action="store_true",
                   help="console only, no plot window")
    # Robot
    p.add_argument("--sim", action="store_true",
                   help="simulated robot (no Bluetooth) - for testing anywhere")
    p.add_argument("--card", default=config.CARD_SERIAL,
                   help=f"Connection Card serial (default {config.CARD_SERIAL})")
    p.add_argument("--no-sensor", action="store_true",
                   help="don't connect the color sensor")
    p.add_argument("--no-defense", action="store_true",
                   help="--mode robot: don't connect the Single Motor defense arm")
    p.add_argument("--profile", choices=list(config.PROFILES), default=config.DEFAULT_PROFILE,
                   help="speed profile (default %(default)s)")
    # Songs
    p.add_argument("--output-device", type=int, default=None,
                   help="audio output device index for songs (default: system default)")
    p.add_argument("--no-hub-songs", action="store_true",
                   help="play the victory/death songs on the laptop only, not the robot too")
    # MQTT
    p.add_argument("--no-mqtt", action="store_true",
                   help="offline: don't connect to the broker (press s to start)")
    p.add_argument("--broker", default=config.BROKER_HOST,
                   help=f"MQTT broker host (default {config.BROKER_HOST})")
    p.add_argument("--port", type=int, default=config.BROKER_PORT,
                   help=f"MQTT broker port (default {config.BROKER_PORT})")
    return p


# ----------------------------------------------------------------------------

def run_with_display(status, audio, keys, title, headless):
    """Show the live display (or a console loop) until the user quits."""
    if headless:
        print("Running without display. Ctrl-C to quit.")
        last = None
        try:
            while True:
                s = status.get()
                line = f"{s['decision']:<12} {s['detail']}"
                if line != last:
                    print(line)
                    last = line
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
    else:
        Display(status, audio=audio, keys=keys, title=title).run()


def audio_keys(audio):
    @keyhelp("re-measure noise")
    def recalibrate():
        audio.recalibrate()
    return {"n": recalibrate}


def run_calibrate(args, status):
    status.set(mode="calibrate", game="CALIBRATION")
    cal = Calibrator(status)
    audio = AudioEngine(args.device, on_detection=cal.on_detection, noise_db=args.noise_db)
    print("\nCALIBRATION MODE - whistle your low, middle and high notes.")
    print("Each whistle is summarized below. Edit BANDS in config.py to fit.\n")
    audio.start()
    try:
        run_with_display(status, audio, audio_keys(audio), "Whistle calibration",
                         args.no_display)
    finally:
        audio.stop()


def make_robot(args, status, use_sensor=True, use_defense=False):
    """Connect the real robot (or the simulator) and start its thread."""
    use_sensor = use_sensor and not args.no_sensor
    if args.sim:
        hw = SimRobot(use_sensor=use_sensor, use_defense=use_defense)
    else:
        hw = LegoHardware(card_serial=args.card, use_sensor=use_sensor,
                          use_defense=use_defense)
    hw.connect()
    robot = RobotController(hw, status, profile=args.profile)
    robot.start()
    return robot


def robot_keys(robot):
    keys = {}
    for key, name in zip("123", config.PROFILES):
        def set_profile(name=name):
            robot.set_profile(name)
        set_profile.help = name
        keys[key] = set_profile

    @keyhelp("new sensor baseline")
    def rebaseline():
        robot.proximity.reset()
    keys["b"] = rebaseline

    if isinstance(robot.hw, SimRobot):
        @keyhelp("fake obstacle")
        def obstacle():
            robot.hw.obstacle = not robot.hw.obstacle
        keys["o"] = obstacle
    return keys


def arm_keys(robot):
    """Robot laptop: test the defense arm directly (not gated by the game)."""
    keys = {}
    for key, side in (("j", "left"), ("k", "right"), ("u", "up")):
        def move(side=side):
            robot.defend(side)
        move.help = f"arm {side}"
        keys[key] = move

    @keyhelp("arm: UP is here")
    def zero():
        robot.arm_set_up_here()
    keys["z"] = zero
    return keys


def game_keys(game):
    @keyhelp("start (local test)")
    def start():
        game.start()

    @keyhelp("reset game")
    def reset():
        game.reset()
    return {"s": start, "r": reset}


def make_mqtt(args, status, topics, on_message, on_connect=None, name="robot", **kw):
    """Create (but don't start) the MQTT link. Callers wire everything up and
    THEN call link.start(), so on_connect can never fire half-wired."""
    if args.no_mqtt:
        status.set(mqtt="disabled (--no-mqtt)")
        return None
    return MqttLink(topics, on_message, status, on_connect=on_connect, name=name,
                    host=args.broker, port=args.port, **kw)


class GatedPolicy:
    """Runs the whistle policy on the audio thread and resets it whenever the
    game starts or resets, so whistles heard before 'start' can't carry over
    (e.g. a speed level built up while waiting)."""

    def __init__(self, game):
        self.game = game
        self.policy = DrivePolicy()
        self._generation = game.generation

    def update(self, det):
        if self.game.generation != self._generation:
            self._generation = self.game.generation
            self.policy.reset(det.t)
        out = self.policy.update(det.band, det.t)
        if self.game.state != "PLAYING":
            out.detail = f"ignored - {self.game.state.lower()}"
        return out


def run_single(args, status):
    """One laptop does everything: audio -> policy -> game -> robot, plus MQTT."""
    status.set(mode="single")
    robot = make_robot(args, status, use_sensor=(args.role == "ball"))
    songs = SongPlayer(args.output_device, status=status,
                       hub_beep=None if args.no_hub_songs else robot.beep)
    game = Game(args.role, status, robot=robot, songs=songs)
    link = make_mqtt(args, status, [config.GAME_TOPIC],
                     on_message=lambda topic, payload: game.handle_message(payload),
                     on_connect=game.on_connected)
    if link:
        game.publish = link.publish
        link.start()
    gated = GatedPolicy(game)

    def on_detection(det):
        out = gated.update(det)
        status.set(decision=out.decision, detail=out.detail, progress=out.progress,
                   speed_level=out.drive.speed_level, steer=out.drive.steer)
        game.drive(out.drive)
        if out.fired:
            status.log(f"command: {config.COMMAND_LABELS[out.fired]}")
        if out.goal:
            game.goal()

    audio = AudioEngine(args.device, on_detection=on_detection, noise_db=args.noise_db)
    songs.on_play = audio.mute_for
    audio.start()
    keys = {**audio_keys(audio), **robot_keys(robot), **game_keys(game)}
    try:
        run_with_display(status, audio, keys, f"ME193 World Cup - {args.role}",
                         args.no_display)
    finally:
        audio.stop()
        robot.stop()
        if link:
            link.stop()
        print("Stopped.")


# ----------------------------------------------------------------------------
# Stretch goal: two laptops, one robot (see remote.py for the message format)
# ----------------------------------------------------------------------------

def run_robot_side(args, status):
    """The laptop connected to the robot: no audio, executes MQTT commands."""
    if args.no_mqtt:
        sys.exit("--mode robot needs MQTT (remove --no-mqtt)")
    status.set(mode="robot (2-laptop)", decision="WAITING", detail="for drive laptop")
    use_defense = not args.no_defense
    robot = make_robot(args, status, use_sensor=(args.role == "ball"), use_defense=use_defense)
    songs = SongPlayer(args.output_device, status=status,
                       hub_beep=None if args.no_hub_songs else robot.beep)
    game = Game(args.role, status, robot=robot, songs=songs)
    handler = RobotSideHandler(game, robot, songs, status)
    link = make_mqtt(args, status, [config.GAME_TOPIC, config.DRIVE_TOPIC, config.AUX_TOPIC,
                                    config.DEFENSE_TOPIC],
                     on_message=lambda t, p: handler.on_message(t, p, time.monotonic()),
                     on_connect=game.on_connected, name="robot",
                     # lost MQTT = lost the defense laptop: arm back to zero
                     on_disconnect=(lambda: robot.defend("up")) if use_defense else None)
    game.publish = link.publish
    link.start()

    def watch_link():
        """Show how fresh the drive laptop's stream is (the robot thread
        already stops the car by itself when it goes stale)."""
        while True:
            last = handler.last_drive_t
            if last is None:
                txt = "drive laptop: not heard yet"
            else:
                age = time.monotonic() - last
                txt = f"drive laptop: last message {age:.1f} s ago"
                if age > config.DRIVE_LINK_TIMEOUT_S:
                    txt += "\nLINK LOST - motors stopped"
                    status.set(decision="LINK LOST", detail="no drive messages")
            status.set(audio_extra=txt)
            time.sleep(0.25)
    threading.Thread(target=watch_link, daemon=True).start()

    keys = {**robot_keys(robot), **game_keys(game)}
    if use_defense:
        keys.update(arm_keys(robot))
    try:
        run_with_display(status, None, keys, f"ME193 robot - {args.role}", args.no_display)
    finally:
        robot.stop()
        link.stop()
        print("Stopped.")


def run_drive_station(args, status):
    """Laptop 1: whistles -> drive state, published to the drive topic."""
    status.set(mode="drive laptop", robot="remote (via MQTT)")
    game = Game(args.role, status, mirror=True)      # only mirrors the game state
    link = make_mqtt(args, status, [config.GAME_TOPIC],
                     on_message=lambda t, p: game.handle_message(p), name="drive")
    if link:
        link.start()
    gated = GatedPolicy(game)
    last_pub = [0.0]

    def on_detection(det):
        out = gated.update(det)
        status.set(decision=out.decision, detail=out.detail, progress=out.progress,
                   speed_level=out.drive.speed_level, steer=out.drive.steer)
        if out.fired:
            status.log(f"command: {config.COMMAND_LABELS[out.fired]}")
        if link is None:
            return
        # Re-send the full state at DRIVE_PUBLISH_HZ (heartbeat), immediately
        # when a command fires.
        if out.fired or det.t - last_pub[0] >= 1.0 / config.DRIVE_PUBLISH_HZ:
            link.publish(config.DRIVE_TOPIC, drive_msg(out.drive, out.decision), qos=0)
            last_pub[0] = det.t
        if out.goal:
            link.publish(config.DRIVE_TOPIC, goal_msg(), qos=1)
            status.log("sent GOAL to robot")

    audio = AudioEngine(args.device, on_detection=on_detection, noise_db=args.noise_db)
    audio.start()
    try:
        run_with_display(status, audio, {**audio_keys(audio), **game_keys(game)},
                         "ME193 drive laptop", args.no_display)
    finally:
        audio.stop()
        if link:
            link.stop()


def run_aux_station(args, status):
    """Laptop 2: whistles -> speed profile, hub light and songs (aux topic)."""
    status.set(mode="aux laptop", robot="remote (via MQTT)", profile=args.profile)
    game = Game(args.role, status, mirror=True)
    profiles = list(config.PROFILES)
    state = {"profile": profiles.index(args.profile), "light": -1}
    link = None

    def send(msg, retain=False):
        if link:
            link.publish(config.AUX_TOPIC, msg, retain=retain)

    def set_profile(i):
        state["profile"] = max(0, min(len(profiles) - 1, i))
        name = profiles[state["profile"]]
        status.set(profile=name)
        status.log(f"profile -> {name}")
        send(profile_msg(name), retain=True)

    def next_light():
        state["light"] = (state["light"] + 1) % len(config.HUB_LIGHT_COLORS)
        color = config.HUB_LIGHT_COLORS[state["light"]]
        status.log(f"light -> {color}")
        send(light_msg(color))

    def cheer():
        status.log("song -> cheer")
        send(song_msg("cheer"))

    actions = {
        "PROFILE +": lambda: set_profile(state["profile"] + 1),
        "PROFILE -": lambda: set_profile(state["profile"] - 1),
        "NEXT LIGHT": next_light,
        "CHEER SONG": cheer,
    }
    link = make_mqtt(args, status, [config.GAME_TOPIC],
                     on_message=lambda t, p: game.handle_message(p),
                     on_connect=lambda: set_profile(state["profile"]), name="aux")
    if link:
        link.start()
    aux = AuxPolicy()

    def on_detection(det):
        action, label = aux.update(det.band)
        status.set(decision=label, progress=aux.debouncer.progress,
                   detail="high: faster  low: slower  mid-L: light  mid-R: cheer")
        if action:
            actions[action]()

    audio = AudioEngine(args.device, on_detection=on_detection, noise_db=args.noise_db)
    audio.start()
    keys = audio_keys(audio)
    for key, i in zip("123", range(len(profiles))):
        def fn(i=i):
            set_profile(i)
        fn.help = profiles[i]
        keys[key] = fn
    keys["l"] = keyhelp("light")(lambda: next_light())
    keys["c"] = keyhelp("cheer")(lambda: cheer())
    try:
        run_with_display(status, audio, keys, "ME193 aux laptop", args.no_display)
    finally:
        audio.stop()
        if link:
            link.stop()


DEFENSE_SIDES = {"DEFEND_LEFT": "left", "DEFEND_UP": "up", "DEFEND_RIGHT": "right"}


def run_defense_station(args, status):
    """Laptop 2 (defense): three whistles swing the defense arm left / zero / right.
    Uses config.DEFENSE_BANDS (main() swaps them in) instead of the drive bands."""
    status.set(mode="defense laptop", robot="remote (via MQTT)")
    game = Game(args.role, status, mirror=True)
    # Last will: if this laptop drops off, the broker tells the robot "arm up".
    link = make_mqtt(args, status, [config.GAME_TOPIC],
                     on_message=lambda t, p: game.handle_message(p), name="defense",
                     will=(config.DEFENSE_TOPIC, defense_msg("up")))
    if link:
        link.start()
    debouncer = Debouncer()

    def swing(side):
        status.log(f"defense: arm {side}")
        if link:
            link.publish(config.DEFENSE_TOPIC, defense_msg(side), qos=1)

    def on_detection(det):
        confirmed, just = debouncer.update(det.band)
        label = config.COMMAND_LABELS[confirmed] if confirmed else "NO WHISTLE"
        status.set(decision=label, progress=debouncer.progress,
                   detail="low: arm left   middle: arm zero   high: arm right")
        if just:
            swing(DEFENSE_SIDES[confirmed])

    audio = AudioEngine(args.device, on_detection=on_detection, noise_db=args.noise_db)
    audio.start()
    keys = {**audio_keys(audio), **game_keys(game),
            "j": keyhelp("arm left")(lambda: swing("left")),
            "k": keyhelp("arm right")(lambda: swing("right")),
            "u": keyhelp("arm zero")(lambda: swing("up"))}
    try:
        run_with_display(status, audio, keys, "ME193 defense laptop", args.no_display)
    finally:
        audio.stop()
        if link:
            # A clean quit doesn't trigger the last will, so say it ourselves.
            try:
                link.publish(config.DEFENSE_TOPIC, defense_msg("up"), qos=1).wait_for_publish(1.0)
            except Exception as e:
                print(f"could not send arm zero: {e!r}")
            link.stop()


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.list_devices:
        print_devices()
        return 0
    if args.noise_margin is not None:
        config.NOISE_MARGIN_DB = args.noise_margin
    status = Status()
    if args.mode == "defense":
        config.BANDS = config.DEFENSE_BANDS    # two wide bands; also for --calibrate
    if args.calibrate:
        run_calibrate(args, status)
    else:
        {"single": run_single, "robot": run_robot_side,
         "drive": run_drive_station, "aux": run_aux_station,
         "defense": run_defense_station}[args.mode](args, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
