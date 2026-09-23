"""
ME193 World Cup - whistle-controlled LEGO robot.

Examples
    python main.py --list-devices          # show microphones / speakers
    python main.py --calibrate             # whistle and see the detected pitch
    python main.py --device 1              # whistle control, live display

Keys in the display window: q quit, n re-measure ambient noise.
"""

import argparse
import sys
import time

import config
from audio import AudioEngine, print_devices
from calibrate import Calibrator
from display import Display, keyhelp
from policy import DrivePolicy
from status import Status


def build_parser():
    p = argparse.ArgumentParser(description="Whistle-controlled LEGO robot (ME193 World Cup)")
    p.add_argument("--list-devices", action="store_true",
                   help="list audio input/output devices and exit")
    p.add_argument("--device", type=int, default=None,
                   help="audio input device index (default: system default mic)")
    p.add_argument("--calibrate", action="store_true",
                   help="calibration mode: whistle and see the detected frequency")
    p.add_argument("--noise-db", type=float, default=None,
                   help="fixed volume threshold in dBFS (skips ambient-noise calibration)")
    p.add_argument("--no-display", action="store_true",
                   help="console only, no plot window")
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


def run_single(args, status):
    """Stage 1: audio -> decision policy -> display (no robot yet)."""
    policy = DrivePolicy()

    def on_detection(det):
        out = policy.update(det.band, det.t)
        status.set(decision=out.decision, detail=out.detail, progress=out.progress,
                   speed_level=out.drive.speed_level, steer=out.drive.steer)
        if out.fired:
            status.log(f"command: {config.COMMAND_LABELS[out.fired]}")
        if out.goal:
            status.log("GOAL whistle detected")

    audio = AudioEngine(args.device, on_detection=on_detection, noise_db=args.noise_db)
    audio.start()
    try:
        run_with_display(status, audio, audio_keys(audio), "ME193 World Cup", args.no_display)
    finally:
        audio.stop()


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
