"""
Audio capture (pyaudio) and whistle pitch detection (numpy FFT).

Pipeline for every FRAME_S block of microphone audio:

    samples --Hann window--> FFT --> magnitude spectrum (dBFS)
        |
        +-- 1) BANDPASS   keep only WHISTLE_MIN_HZ..WHISTLE_MAX_HZ bins
        +-- find the strongest bin in that range = candidate pitch
        +-- 2) VOLUME     is that peak louder than the noise threshold?
        +-- 3) TONALITY   is the peak much stronger than the band average?
        +-- if both pass: map the pitch to a command band (config.BANDS)

The 4th noise filter (debouncing over time) happens in policy.py, because it
needs memory across frames; this module only judges one frame at a time.
"""

import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pyaudio

import config


# ----------------------------------------------------------------------------
# Device helpers
# ----------------------------------------------------------------------------

def list_input_devices(pa=None):
    """Return [(index, name, default_rate, channels)] for every input device."""
    own = pa is None
    pa = pa or pyaudio.PyAudio()
    try:
        out = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                out.append((i, info["name"], int(info["defaultSampleRate"]),
                            info["maxInputChannels"]))
        return out
    finally:
        if own:
            pa.terminate()


def list_output_devices(pa=None):
    """Return [(index, name, default_rate)] for every output device."""
    own = pa is None
    pa = pa or pyaudio.PyAudio()
    try:
        return [(i, info["name"], int(info["defaultSampleRate"]))
                for i in range(pa.get_device_count())
                for info in [pa.get_device_info_by_index(i)]
                if info["maxOutputChannels"] > 0]
    finally:
        if own:
            pa.terminate()


def print_devices():
    pa = pyaudio.PyAudio()
    try:
        default_in = pa.get_default_input_device_info()["index"]
        print("Input devices (use --device N):")
        for i, name, rate, ch in list_input_devices(pa):
            mark = "  <- default" if i == default_in else ""
            print(f"  [{i}] {name}  ({rate} Hz, {ch} ch){mark}")
        print("\nOutput devices for songs (use --output-device N):")
        for i, name, rate in list_output_devices(pa):
            print(f"  [{i}] {name}  ({rate} Hz)")
        print("\nTip: AirPods show up as an input only once connected. In mic mode")
        print("they switch to a low 'call quality' rate (16-24 kHz); that's fine.")
    finally:
        pa.terminate()


def choose_sample_rate(pa, device_index):
    """Pick a sample rate the device actually supports.

    Try the device's own default first (AirPods in call mode report their low
    rate here), then the fallbacks. Returns the first rate PortAudio accepts.
    """
    info = pa.get_device_info_by_index(device_index)
    candidates = [int(info["defaultSampleRate"])] + list(config.FALLBACK_SAMPLE_RATES)
    for rate in candidates:
        try:
            if pa.is_format_supported(rate, input_device=device_index,
                                      input_channels=1, input_format=pyaudio.paInt16):
                return rate
        except ValueError:
            continue
    raise RuntimeError(f"No supported sample rate for input device {device_index} "
                       f"({info['name']})")


# ----------------------------------------------------------------------------
# Pitch detection (pure numpy - no pyaudio, so it is unit-testable)
# ----------------------------------------------------------------------------

@dataclass
class Detection:
    """Everything we learned from one audio frame (also what the display draws)."""
    t: float
    samples: np.ndarray                 # raw frame, float -1..1
    freqs: np.ndarray                   # FFT bin frequencies (Hz)
    spectrum_db: np.ndarray             # magnitude per bin, dBFS
    pitch_hz: float | None = None       # strongest in-band peak (even if rejected)
    level_db: float = -120.0            # loudness of that peak, dBFS
    tonality_db: float = 0.0            # peak-to-average ratio in the band
    peak_share: float = 0.0             # fraction of band energy in the peak
    threshold_db: float = config.DEFAULT_THRESHOLD_DB
    loud: bool = False                  # passed the volume threshold
    tonal: bool = False                 # passed the tonality check
    band: str | None = None             # command band, only if loud AND tonal
    reason: str = ""                    # why it was rejected (for display)
    muted: bool = False                 # input ignored (e.g. our own song playing)

    @property
    def valid(self):
        return self.band is not None


def band_for(pitch_hz):
    """Map a pitch to a command band name, or None if it falls in a guard gap."""
    for name, lo, hi in config.BANDS:
        if lo <= pitch_hz < hi:
            return name
    return None


class PitchDetector:
    def __init__(self, sample_rate, frame_len):
        self.sample_rate = sample_rate
        self.frame_len = frame_len
        # Hann window: tapers the frame edges so a pure tone shows up as one
        # clean narrow peak instead of smearing energy across the spectrum.
        self.window = np.hanning(frame_len).astype(np.float32)
        # Zero-pad the FFT to >= 4x the frame for a finer frequency grid.
        self.nfft = 1 << int(np.ceil(np.log2(frame_len * 4)))
        self.freqs = np.fft.rfftfreq(self.nfft, 1.0 / sample_rate)
        # Scale so a full-scale sine wave reads 0 dBFS at its peak.
        self.scale = 2.0 / self.window.sum()

        # --- Noise filter 1: BANDPASS ---------------------------------------
        # We simply refuse to look outside the whistle range. The upper edge is
        # also capped just below Nyquist, which matters for low-rate AirPods.
        hi = min(config.WHISTLE_MAX_HZ, 0.45 * sample_rate)
        self.band_mask = (self.freqs >= config.WHISTLE_MIN_HZ) & (self.freqs <= hi)
        self.band_idx = np.flatnonzero(self.band_mask)
        if len(self.band_idx) < 8:
            raise ValueError(f"Sample rate {sample_rate} Hz is too low for the "
                             f"{config.WHISTLE_MIN_HZ}-{config.WHISTLE_MAX_HZ} Hz whistle band")

        self.threshold_db = config.DEFAULT_THRESHOLD_DB

    def analyze(self, samples, t=0.0):
        """Judge one frame of audio. `samples` is a float array in -1..1."""
        x = samples.astype(np.float32)
        x = x - x.mean()                            # remove any DC offset
        mag = np.abs(np.fft.rfft(x * self.window, self.nfft)) * self.scale
        power = mag ** 2
        spectrum_db = 20 * np.log10(mag + 1e-10)

        det = Detection(t=t, samples=samples, freqs=self.freqs,
                        spectrum_db=spectrum_db, threshold_db=self.threshold_db)

        # Strongest bin INSIDE the bandpass range = candidate whistle pitch.
        band_power = power[self.band_idx]
        k = int(np.argmax(band_power))
        peak = self.band_idx[k]
        det.pitch_hz = self._refine(spectrum_db, peak)
        det.level_db = float(spectrum_db[peak])

        # --- Noise filter 2: VOLUME -----------------------------------------
        det.loud = det.level_db >= self.threshold_db

        # --- Noise filter 3: TONALITY (peak-to-average ratio) ---------------
        # A whistle puts almost all its energy in one narrow peak, so the peak
        # towers over the average bin. Broadband sounds (talking, crowds,
        # motors, wind on the mic) raise many bins at once, so their peak is
        # only a little above average.
        det.tonality_db = float(10 * np.log10(band_power[k] / (band_power.mean() + 1e-20)))
        # ...and the peak must hold most of the band's energy, which rejects
        # harmonic sounds (voices, motor whine) that have MANY narrow peaks.
        near = np.abs(self.freqs[self.band_idx] - self.freqs[peak]) <= config.PEAK_WIDTH_HZ
        det.peak_share = float(band_power[near].sum() / (band_power.sum() + 1e-20))
        det.tonal = (det.tonality_db >= config.TONALITY_MIN_DB
                     and det.peak_share >= config.PEAK_SHARE_MIN)

        if not det.loud:
            det.reason = "too quiet"
        elif not det.tonal:
            det.reason = "not a whistle (noisy)"
        else:
            det.band = band_for(det.pitch_hz)
            if det.band is None:
                det.reason = "between bands"
        return det

    def _refine(self, spectrum_db, k):
        """Parabolic interpolation around the peak bin for sub-bin accuracy."""
        if 0 < k < len(spectrum_db) - 1:
            a, b, c = spectrum_db[k - 1], spectrum_db[k], spectrum_db[k + 1]
            denom = a - 2 * b + c
            if denom != 0:
                offset = 0.5 * (a - c) / denom
                return float(self.freqs[k] + offset * (self.freqs[1] - self.freqs[0]))
        return float(self.freqs[k])


def ambient_threshold(levels_db):
    """Volume threshold from the room's ambient noise.

    We use the MEDIAN in-band peak level of the calibration frames (robust to
    one accidental clap or cough), add NOISE_MARGIN_DB, and never go below
    MIN_THRESHOLD_DB.
    """
    if len(levels_db) == 0:
        return config.DEFAULT_THRESHOLD_DB
    return max(config.MIN_THRESHOLD_DB, float(np.median(levels_db)) + config.NOISE_MARGIN_DB)


# ----------------------------------------------------------------------------
# Live capture thread
# ----------------------------------------------------------------------------

@dataclass
class AudioSnapshot:
    """Latest data for the display, copied out under a lock."""
    detection: Detection | None = None
    pitch_history: list = field(default_factory=list)   # [(t, pitch_hz, band)]
    calibrating: bool = False
    sample_rate: int = 0
    device_name: str = ""


class AudioEngine:
    """Reads the mic on its own thread and runs the detector on every frame.

    For each frame it calls on_detection(det) - the caller plugs the decision
    policy in there. The display reads snapshot() independently, so a slow
    plot never makes us drop audio, and audio never waits for the robot.
    """

    def __init__(self, device_index=None, on_detection=None, noise_db=None):
        self.pa = pyaudio.PyAudio()
        if device_index is None:
            device_index = self.pa.get_default_input_device_info()["index"]
        self.device_index = device_index
        self.device_name = self.pa.get_device_info_by_index(device_index)["name"]
        self.sample_rate = choose_sample_rate(self.pa, device_index)
        self.frame_len = int(round(self.sample_rate * config.FRAME_S))
        self.detector = PitchDetector(self.sample_rate, self.frame_len)
        self.on_detection = on_detection

        # Ambient-noise calibration runs first unless a fixed level was given.
        self._calib_levels = []
        self._calibrating = noise_db is None
        if noise_db is not None:
            self.detector.threshold_db = noise_db

        self._muted_until = 0.0
        self._lock = threading.Lock()
        self._snapshot = AudioSnapshot(sample_rate=self.sample_rate,
                                       device_name=self.device_name,
                                       calibrating=self._calibrating)
        self._stop = threading.Event()
        self._thread = None
        self.stream = None

    # -- control --------------------------------------------------------------
    def start(self):
        self.stream = self.pa.open(format=pyaudio.paInt16, channels=1,
                                   rate=self.sample_rate, input=True,
                                   input_device_index=self.device_index,
                                   frames_per_buffer=self.frame_len)
        print(f"Audio: '{self.device_name}' at {self.sample_rate} Hz, "
              f"{self.frame_len} samples/frame ({config.FRAME_S * 1000:.0f} ms)")
        if self._calibrating:
            print(f"Measuring ambient noise for {config.NOISE_CALIBRATION_S:.0f} s "
                  f"- stay quiet, don't whistle...")
        self._thread = threading.Thread(target=self._run, name="audio", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.pa.terminate()

    def recalibrate(self):
        """Measure ambient noise again (display key 'n')."""
        with self._lock:
            self._calib_levels = []
            self._calibrating = True
            self._snapshot.calibrating = True
        print("Re-measuring ambient noise - stay quiet...")

    def mute_for(self, seconds):
        """Ignore the mic for a while, e.g. while our own song plays through the
        laptop speakers - otherwise the song's notes would be heard as whistles."""
        self._muted_until = max(self._muted_until, time.monotonic() + seconds)

    def snapshot(self):
        with self._lock:
            s = self._snapshot
            return AudioSnapshot(s.detection, list(s.pitch_history), s.calibrating,
                                 s.sample_rate, s.device_name)

    # -- thread ---------------------------------------------------------------
    def _run(self):
        calib_frames = int(config.NOISE_CALIBRATION_S / config.FRAME_S)
        while not self._stop.is_set():
            try:
                raw = self.stream.read(self.frame_len, exception_on_overflow=False)
            except OSError as e:          # device unplugged / AirPods disconnected
                print(f"Audio read error: {e}")
                time.sleep(0.1)
                continue
            t = time.monotonic()
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            det = self.detector.analyze(samples, t)

            if self._calibrating:
                # While calibrating, nothing counts as a whistle.
                self._calib_levels.append(det.level_db)
                det.band, det.reason = None, "measuring ambient noise"
                if len(self._calib_levels) >= calib_frames:
                    self.detector.threshold_db = ambient_threshold(self._calib_levels)
                    det.threshold_db = self.detector.threshold_db
                    self._calibrating = False
                    print(f"Ambient in-band noise ~ {np.median(self._calib_levels):.1f} dBFS "
                          f"-> volume threshold {self.detector.threshold_db:.1f} dBFS")
            elif t < self._muted_until:
                det.band, det.reason, det.muted = None, "muted (song playing)", True

            with self._lock:
                snap = self._snapshot
                snap.detection = det
                snap.calibrating = self._calibrating
                snap.pitch_history.append((t, det.pitch_hz if det.loud else None, det.band))
                cutoff = t - config.PITCH_HISTORY_S
                while snap.pitch_history and snap.pitch_history[0][0] < cutoff:
                    snap.pitch_history.pop(0)

            if self.on_detection:
                try:
                    self.on_detection(det)
                except Exception as e:     # never let a downstream bug kill audio
                    print(f"Error handling audio frame: {e!r}")
