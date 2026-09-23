"""
Calibration helper: whistle, and it prints what the detector heard.

It groups consecutive "whistle-like" frames (loud AND tonal, band ignored)
into one whistle and prints a summary when the whistle ends, e.g.

    whistle 0.84 s   median 1712 Hz (1688-1741)   -31 dB   peak/avg 24 dB   share 88 %   -> TURN RIGHT

Whistle your lowest, middle and highest notes a few times each, then set
config.BANDS so each note sits comfortably inside its own band.
"""

import numpy as np

import config
from audio import band_for


class Calibrator:
    def __init__(self, status):
        self.status = status
        self._frames = []
        self._gap = 0

    def on_detection(self, det):
        whistle_like = det.loud and det.tonal and not det.muted
        if whistle_like:
            self._frames.append(det)
            self._gap = 0
            label = config.COMMAND_LABELS.get(det.band, "between bands")
            self.status.set(decision=f"{det.pitch_hz:.0f} Hz", detail=f"-> {label}")
            return
        if not self._frames:
            self.status.set(decision="LISTENING", detail=det.reason)
            return
        self._gap += 1
        if self._gap > config.DROPOUT_FRAMES:
            self._report()
            self._frames = []

    def _report(self):
        f = self._frames
        if len(f) < 2:
            return
        pitches = np.array([d.pitch_hz for d in f])
        med = float(np.median(pitches))
        band = band_for(med)
        self.status.log(
            f"whistle {len(f) * config.FRAME_S:.2f} s  median {med:.0f} Hz "
            f"({np.percentile(pitches, 10):.0f}-{np.percentile(pitches, 90):.0f})  "
            f"{np.median([d.level_db for d in f]):.0f} dB  "
            f"peak/avg {np.median([d.tonality_db for d in f]):.0f} dB  "
            f"share {np.median([d.peak_share for d in f]) * 100:.0f} %  "
            f"-> {config.COMMAND_LABELS.get(band, 'between bands')}")
