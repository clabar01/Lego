"""
Songs: short melodies synthesized with numpy and played through pyaudio.

    victory   rising fanfare (we won)
    death     "sad trombone" wah-wah-wah-waaah (we lost)
    cheer     short arpeggio (stretch goal: laptop 2 can trigger it)

Songs play on a background thread so nothing waits for them. The songs in
config.HUB_SONGS (victory and death) are also beeped on the robot's Double
Motor at the same time (turn off with --no-hub-songs); it can only play a
fixed-length beep per note, so it sounds rougher.
"""

import threading
import time

import numpy as np
import pyaudio

import config

_NOTE_INDEX = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6,
               "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


def note_hz(name):
    """'A4' -> 440.0, 'C#5' -> 554.4. 'R' = rest (0 Hz)."""
    if name == "R":
        return 0.0
    pitch, octave = name[:-1], int(name[-1])
    semis = _NOTE_INDEX[pitch] + 12 * (octave + 1) - 69     # MIDI number - A4
    return 440.0 * 2 ** (semis / 12)


# (note, seconds). The last death note wobbles (vibrato) like a trombone.
SONGS = {
    "victory": [("C5", .12), ("E5", .12), ("G5", .12), ("C6", .28), ("R", .06),
                ("G5", .12), ("C6", .55), ("R", .1), ("E6", .15), ("D6", .15), ("C6", .7)],
    "death":   [("G4", .38), ("F#4", .38), ("F4", .38), ("E4", 1.3)],
    "cheer":   [("G4", .1), ("C5", .1), ("E5", .1), ("G5", .25), ("E5", .1), ("G5", .4)],
}


def synthesize(song, rate=config.SONG_SAMPLE_RATE, volume=config.SONG_VOLUME):
    """Melody -> int16 samples. A few harmonics make it sound less like a
    test tone; a short attack/release envelope avoids clicks between notes."""
    out = []
    notes = SONGS[song]
    for i, (name, dur) in enumerate(notes):
        n = int(rate * dur)
        t = np.arange(n) / rate
        hz = note_hz(name)
        if hz == 0:
            out.append(np.zeros(n))
            continue
        if song == "death" and i == len(notes) - 1:
            phase = 2 * np.pi * (hz * t + (6 / (2 * np.pi * 5)) * np.sin(2 * np.pi * 5 * t))
        else:
            phase = 2 * np.pi * hz * t
        wave = np.sin(phase) + 0.35 * np.sin(2 * phase) + 0.15 * np.sin(3 * phase)
        env = np.ones(n)
        a, r = min(n, int(0.01 * rate)), min(n, int(0.06 * rate))
        env[:a] = np.linspace(0, 1, a)
        env[n - r:] *= np.linspace(1, 0, r)
        out.append(wave * env)
    audio = np.concatenate(out) / 1.5 * volume
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


def duration(song):
    return sum(d for _, d in SONGS[song])


class SongPlayer:
    """Plays songs without blocking.

    output_device   pyaudio output index (None = system default)
    on_play         callback(seconds) before a song starts - used to mute the
                    microphone so our own song isn't heard as whistles
    hub_beep        callable(freq) to also beep config.HUB_SONGS on the robot, or None
    """

    def __init__(self, output_device=None, on_play=None, hub_beep=None, status=None):
        self.output_device = output_device
        self.on_play = on_play
        self.hub_beep = hub_beep
        self.status = status
        self._lock = threading.Lock()
        self._playing_until = 0.0
        self._cache = {}

    def busy(self):
        return time.monotonic() < self._playing_until

    def play(self, song, interrupt=True):
        """Start a song. With interrupt=False, a song already playing wins and
        this call returns False (used for aux-requested songs)."""
        with self._lock:
            if self.busy() and not interrupt:
                return False
            self._playing_until = time.monotonic() + duration(song)
        if self.on_play:
            self.on_play(duration(song) + 0.3)
        if self.status:
            self.status.log(f"song: {song}")
        threading.Thread(target=self._play_laptop, args=(song,), daemon=True).start()
        if self.hub_beep and song in config.HUB_SONGS:
            threading.Thread(target=self._play_hub, args=(song,), daemon=True).start()
        return True

    def _play_laptop(self, song):
        pa = pyaudio.PyAudio()
        try:
            rate = self._output_rate(pa)
            if song not in self._cache or self._cache[song][0] != rate:
                self._cache[song] = (rate, synthesize(song, rate))
            data = self._cache[song][1]
            stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, output=True,
                             output_device_index=self.output_device)
            stream.write(data.tobytes())
            stream.stop_stream()
            stream.close()
        except Exception as e:
            print(f"song playback failed: {e!r}")
        finally:
            pa.terminate()

    def _output_rate(self, pa):
        idx = self.output_device
        if idx is None:
            idx = pa.get_default_output_device_info()["index"]
        for rate in (config.SONG_SAMPLE_RATE,
                     int(pa.get_device_info_by_index(idx)["defaultSampleRate"])):
            try:
                if pa.is_format_supported(rate, output_device=idx, output_channels=1,
                                          output_format=pyaudio.paInt16):
                    return rate
            except ValueError:
                pass
        return config.SONG_SAMPLE_RATE

    def _play_hub(self, song):
        for name, dur in SONGS[song]:
            hz = note_hz(name)
            if hz:
                self.hub_beep(hz)
            time.sleep(dur)
