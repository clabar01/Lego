"""
ME193 World Cup - drive the robot with a VIOLIN instead of whistling.

Same program as main.py (same flags, display, robot, MQTT and game logic);
only the listening part changes. The whistle code is untouched: run main.py
to whistle, violin.py to play.

    python violin.py --list-devices
    python violin.py --calibrate --device 1     # play each note, check the band
    python violin.py --no-mqtt --device 1        # local test: press s, then play
    python violin.py --role ball --device 1      # the real game over MQTT
    python violin.py --mode drive --device 1     # 2-laptop: violin laptop drives

Notes -> commands (VIOLIN_NOTES below): a D major scale up the D string,
plus D5 to declare the win.

    D4  open D     294 Hz   STOP
    E4  D str 1    330 Hz   BACKWARD   (hold it: backs up faster every 0.8 s)
    F#4 D str 2    370 Hz   TURN LEFT
    G4  D str 3    392 Hz   TURN RIGHT
    A4  open A     440 Hz   SPEED UP   (hold it: faster every 0.8 s)
    D5  A str 3    587 Hz   WE WON     (hold it 1 s - WIN_HOLD_S; ball only)

D5 is an octave above the open D (STOP), and pitch detectors sometimes jump
an octave. So the WIN note must be held a full second before it counts, far
longer than a glitch. Replaces the whistle's "tweet-tweet" goal.

Different from whistling: the car only moves WHILE you play. When no command
note is heard for SILENCE_STOP_S (0.3 s, enough for a bow change) it stops.

Why a different detector: a whistle is almost a pure sine, so "loudest
frequency = the note" works. A violin is full of overtones and its loudest
one is often NOT the note you play (on a laptop mic the G string's 2nd or 3rd
harmonic is usually louder than the fundamental). So ViolinDetector finds the
FUNDAMENTAL with the YIN algorithm (de Cheveigné & Kawahara, 2002): it looks
for the repeat period of the waveform, which is 1 / the played note, however
the energy is spread across the overtones. The whistle filters "one narrow
peak" and "peak share" would reject every violin note, so YIN's own
periodicity score (clarity) replaces them.
"""

import sys

import numpy as np

import audio
import config
import main
from audio import PitchDetector

# ============================================================================
# VIOLIN SETTINGS
# ============================================================================

# (command, note). Change a note here to remap a command.
VIOLIN_NOTES = [
    ("STOP",       "D4"),
    ("BACKWARD",   "E4"),
    ("TURN_LEFT",  "F#4"),
    ("TURN_RIGHT", "G4"),
    ("SPEED_UP",   "A4"),
    ("WIN",        "D5"),       # hold 1 s = we won (config.WIN_HOLD_S)
]

# How far out of tune a note may be and still count, in cents (100 cents =
# 1 semitone). 50 = anything closer to this note than to its neighbors; it
# also leaves room for vibrato.
VIOLIN_TOLERANCE_CENTS = 50

# Search range for the fundamental: just below open G up to past B5.
# (Keep every note in VIOLIN_NOTES inside it.)
VIOLIN_MIN_HZ = 180
VIOLIN_MAX_HZ = 1100

# Loudness is measured over the whole violin sound (fundamental + overtones).
VIOLIN_LEVEL_MAX_HZ = 4000

# YIN periodicity. clarity = 1 - (YIN dip at the period): a steady bowed note
# is ~0.85-0.95, talking and noise are usually well under 0.7.
YIN_THRESHOLD = 0.15            # first dip below this is taken as the period
VIOLIN_MIN_CLARITY = 0.75       # below this the frame is "not a steady note"


# ============================================================================

_NOTE_INDEX = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6,
               "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


def note_hz(name):
    """'A4' -> 440.0 (equal temperament)."""
    pitch, octave = name[:-1], int(name[-1])
    return 440.0 * 2 ** ((_NOTE_INDEX[pitch] + 12 * (octave + 1) - 69) / 12)


def violin_bands():
    """VIOLIN_NOTES -> [(command, low_hz, high_hz)], each note +/- tolerance."""
    k = 2 ** (VIOLIN_TOLERANCE_CENTS / 1200)
    return [(cmd, round(note_hz(n) / k, 1), round(note_hz(n) * k, 1))
            for cmd, n in VIOLIN_NOTES]


VIOLIN_BANDS = violin_bands()


def band_for(f0):
    for name, lo, hi in VIOLIN_BANDS:
        if lo <= f0 < hi:
            return name
    return None


def yin(x, sample_rate, fmin=VIOLIN_MIN_HZ, fmax=VIOLIN_MAX_HZ, threshold=YIN_THRESHOLD):
    """Fundamental frequency of one frame with YIN. Returns (f0_hz, clarity)."""
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    n = len(x)
    tau_min = max(2, int(sample_rate / fmax))
    tau_max = min(int(sample_rate / fmin) + 1, n // 2)
    w = n - tau_max                                  # comparison window length
    if w <= tau_max or not np.any(x):
        return None, 0.0

    # Difference function d(tau) = sum_j (x[j] - x[j+tau])^2, j < w, via FFT:
    # d = energy(x[0:w]) + energy(x[tau:tau+w]) - 2 * crosscorr(tau).
    size = 1 << int(np.ceil(np.log2(n + w)))
    corr = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(x[:w], size)), size)
    corr = corr[:tau_max + 1]
    energy = np.concatenate(([0.0], np.cumsum(x ** 2)))
    e0 = energy[w]
    taus = np.arange(tau_max + 1)
    d = e0 + (energy[taus + w] - energy[taus]) - 2 * corr
    d[0] = 0.0

    # Cumulative mean normalized difference: a dip toward 0 = strong repeat.
    cmnd = np.ones_like(d)
    running = np.cumsum(d[1:])
    cmnd[1:] = d[1:] * np.arange(1, tau_max + 1) / np.maximum(running, 1e-20)

    # The FIRST dip under the threshold is the period (later dips are 2x, 3x
    # the period = octave-down errors). Walk down to the bottom of that dip.
    below = np.flatnonzero(cmnd[tau_min:tau_max] < threshold)
    if below.size:
        tau = tau_min + below[0]
        while tau + 1 < tau_max and cmnd[tau + 1] < cmnd[tau]:
            tau += 1
    else:
        tau = tau_min + int(np.argmin(cmnd[tau_min:tau_max]))
    clarity = float(max(0.0, 1.0 - cmnd[tau]))

    # Parabolic interpolation for a sub-sample period.
    if 0 < tau < tau_max:
        a, b, c = cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if denom != 0 else 0.0
    else:
        shift = 0.0
    return sample_rate / (tau + shift), clarity


class ViolinDetector(PitchDetector):
    """Drop-in replacement for audio.PitchDetector: same Detection output,
    so the ambient-noise calibration, debouncing, display and robot code all
    work unchanged."""

    def __init__(self, sample_rate, frame_len):
        super().__init__(sample_rate, frame_len)
        # Loudness over the violin's whole range, not the whistle range.
        hi = min(VIOLIN_LEVEL_MAX_HZ, 0.45 * sample_rate)
        self.band_mask = (self.freqs >= VIOLIN_MIN_HZ) & (self.freqs <= hi)
        self.band_idx = np.flatnonzero(self.band_mask)

    def analyze(self, samples, t=0.0):
        # The whistle analysis gives the spectrum and the loudness (volume
        # filter). Its pitch, tonality and band are replaced below.
        det = super().analyze(samples, t)
        det.band, det.reason = None, ""
        f0, clarity = yin(samples, self.sample_rate)
        if f0 is not None:
            det.pitch_hz = f0                # else keep the loudest peak for the display
        det.tonality_db = clarity * 100      # shown on the display as the 2nd filter
        det.peak_share = clarity
        det.tonal = f0 is not None and clarity >= VIOLIN_MIN_CLARITY
        if not det.loud:
            det.reason = "too quiet"
        elif not det.tonal:
            det.reason = "not a steady note"
        else:
            det.band = band_for(f0)
            if det.band is None:
                det.reason = "not a command note"
        return det


def use_violin():
    """Switch the shared program from whistles to violin."""
    audio.PitchDetector = ViolinDetector     # AudioEngine builds its detector from this
    config.BANDS = VIOLIN_BANDS              # display, calibration and policy follow
    config.WHISTLE_MIN_HZ = VIOLIN_MIN_HZ    # display plot range
    config.WHISTLE_MAX_HZ = VIOLIN_LEVEL_MAX_HZ
    # The display prints these as the "tonality" filter's minimums.
    config.TONALITY_MIN_DB = VIOLIN_MIN_CLARITY * 100
    config.PEAK_SHARE_MIN = VIOLIN_MIN_CLARITY
    config.BACKWARD_RAMP = True              # hold BACKWARD: back up faster and faster
    config.STOP_WHEN_SILENT = True           # stop playing = car stops
    config.GOAL_TWEETS = False               # the WIN note replaces the tweet-tweet goal


def run(argv=None):
    args = main.build_parser().parse_args(argv)
    if args.mode in ("aux", "defense"):
        sys.exit("violin.py only drives (single / drive / robot modes). "
                 "Use main.py for the aux and defense laptops.")
    use_violin()
    print("VIOLIN mode - notes: " + ", ".join(
        f"{n}={config.COMMAND_LABELS[c]}" for c, n in VIOLIN_NOTES))
    return main.main(argv)


if __name__ == "__main__":
    sys.exit(run())
