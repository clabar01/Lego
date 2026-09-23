"""
ME193 World Cup - whistle-controlled LEGO robot.

Examples
    python main.py --list-devices          # show microphones / speakers
    python main.py --calibrate             # whistle and see the detected pitch
    python main.py --device 1              # whistle control, live display
    python main.py --sim                   # same, with a simulated robot
    python main.py --role goalie           # play as the goalie
    python main.py --sim --no-mqtt         # offline test: press s to start

Keys in the display window are listed at the bottom of the window.
"""

import argparse
import sys
import time

import config
from audio import AudioEngine, print_devices
from calibrate import Calibrator
from display import Display, keyhelp
from game import Game
from mqtt_link import MqttLink
from policy import DrivePolicy
from robot import LegoHardware, RobotController, SimRobot
from songs import SongPlayer
from status import Status


def build_parser():
    p = argparse.ArgumentParser(description="Whistle-controlled LEGO robot (ME193 World Cup)")
    p.add_argument("--list-devices", action="store_true",
                   help="list audio input/output devices and exit")
    p.add_argument("--device", type=int, default=None,
                   help="audio input device index (default: system default mic)")
    p.add_argument("--role", choices=["ball", "goalie"], default="ball",
                   help="which robot we are in the match (default %(default)s)")
    p.add_argument("--calibrate", action="store_true",
                   help="calibration mode: whistle and see the detected frequency")
    p.add_argument("--noise-db", type=float, default=None,
                   help="fixed volume threshold in dBFS (skips ambient-noise calibration)")
    p.add_argument("--no-display", action="store_true",
                   help="console only, no plot window")
    # Robot
    p.add_argument("--sim", action="store_true",
                   help="simulated robot (no Bluetooth) - for testing anywhere")
    p.add_argument("--card", default=config.CARD_SERIAL,
                   help=f"Connection Card serial (default {config.CARD_SERIAL})")
    p.add_argument("--no-sensor", action="store_true",
                   help="don't connect the color sensor")
    p.add_argument("--profile", choices=list(config.PROFILES), default=config.DEFAULT_PROFILE,
                   help="speed profile (default %(default)s)")
    # Songs
    p.add_argument("--output-device", type=int, default=None,
                   help="audio output device index for songs (default: system default)")
    p.add_argument("--hub-songs", action="store_true",
                   help="also beep the songs on the LEGO hub")
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


def make_robot(args, status, use_sensor=True):
    """Connect the real robot (or the simulator) and start its thread."""
    use_sensor = use_sensor and not args.no_sensor
    if args.sim:
        hw = SimRobot(use_sensor=use_sensor)
    else:
        hw = LegoHardware(card_serial=args.card, use_sensor=use_sensor)
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


def game_keys(game):
    @keyhelp("start (local test)")
    def start():
        game.start()

    @keyhelp("reset game")
    def reset():
        game.reset()
    return {"s": start, "r": reset}


def make_mqtt(args, status, topics, on_message, on_connect=None, name="robot"):
    if args.no_mqtt:
        status.set(mqtt="disabled (--no-mqtt)")
        return None
    link = MqttLink(topics, on_message, status, on_connect=on_connect, name=name,
                    host=args.broker, port=args.port)
    link.start()
    return link


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
                       hub_beep=robot.beep if args.hub_songs else None)
    game = Game(args.role, status, robot=robot, songs=songs)
    link = make_mqtt(args, status, [config.GAME_TOPIC],
                     on_message=lambda topic, payload: game.handle_message(payload),
                     on_connect=game.on_connected)
    if link:
        game.publish = link.publish
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


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.list_devices:
        print_devices()
        return 0
    status = Status()
    if args.calibrate:
        run_calibrate(args, status)
    else:
        run_single(args, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
