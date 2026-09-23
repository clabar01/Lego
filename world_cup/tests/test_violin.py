"""Violin note detection (violin.py) on synthetic violin-like tones."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config                                              # noqa: E402
import violin                                              # noqa: E402
from violin import VIOLIN_NOTES, ViolinDetector, band_for, note_hz, yin   # noqa: E402

RNG = np.random.default_rng(0)


def bowed(rate, hz, seconds=config.FRAME_S, vibrato_cents=0.0, fundamental=0.3,
          noise=0.01, level=0.3):
    """Sawtooth-like bowed string: many harmonics, a WEAK fundamental (as a
    laptop mic hears a violin), optional 5.5 Hz vibrato, a little bow noise."""
    t = np.arange(int(rate * seconds)) / rate
    wobble = hz * (2 ** (vibrato_cents / 1200 * np.sin(2 * np.pi * 5.5 * t)) - 1)
    phase = 2 * np.pi * np.cumsum(hz + wobble) / rate
    x = fundamental * np.sin(phase)
    for k in range(2, 12):
        if k * hz < 0.45 * rate:
            x += (1.0 / k) * np.sin(k * phase + RNG.uniform(0, 2 * np.pi))
    x += noise * RNG.standard_normal(len(t))
    return (level * x / np.max(np.abs(x))).astype(np.float32)


def detector(rate):
    d = ViolinDetector(rate, int(round(rate * config.FRAME_S)))
    d.threshold_db = -50
    return d


@pytest.mark.parametrize("rate", [48000, 44100, 16000])
@pytest.mark.parametrize("cmd,note", VIOLIN_NOTES)
def test_each_command_note(rate, cmd, note):
    det = detector(rate).analyze(bowed(rate, note_hz(note), vibrato_cents=20))
    assert det.band == cmd, (det.pitch_hz, det.reason)


@pytest.mark.parametrize("cmd,note", VIOLIN_NOTES)
def test_weak_fundamental_is_not_an_octave_error(cmd, note):
    # Almost no fundamental: the 2nd harmonic (one octave up) is far louder.
    # Matters most for the note an octave below WIN.
    det = detector(48000).analyze(bowed(48000, note_hz(note), fundamental=0.05))
    assert det.band == cmd and abs(det.pitch_hz - note_hz(note)) < 5


def test_out_of_tune_within_tolerance():
    cmd, note = VIOLIN_NOTES[0]
    for cents in (35, -35):
        hz = note_hz(note) * 2 ** (cents / 1200)
        assert detector(48000).analyze(bowed(48000, hz)).band == cmd


def test_other_notes_are_not_commands():
    for note in ("C4", "F4", "C5", "G5"):
        det = detector(48000).analyze(bowed(48000, note_hz(note)))
        assert det.band is None and det.reason == "not a command note", note


def test_noise_is_not_a_note():
    x = (0.3 * RNG.standard_normal(2400)).astype(np.float32)
    det = detector(48000).analyze(x)
    assert det.band is None and not det.tonal


def test_silence_still_has_a_pitch_for_the_display():
    det = detector(48000).analyze(np.zeros(2400, dtype=np.float32))
    assert det.pitch_hz is not None and det.band is None


def test_quiet_note_rejected():
    det = detector(48000).analyze(bowed(48000, 440.0, level=0.0005))
    assert det.band is None and det.reason == "too quiet"


def test_yin_pure_tones():
    rate = 48000
    t = np.arange(2400) / rate
    for hz in (196.0, 330.0, 987.8):
        f0, clarity = yin(np.sin(2 * np.pi * hz * t), rate)
        assert abs(f0 - hz) < 1.0 and clarity > 0.95


def test_bands_do_not_overlap_and_no_octaves():
    bands = sorted(violin.VIOLIN_BANDS, key=lambda b: b[1])
    for (_, _, hi), (_, lo, _) in zip(bands, bands[1:]):
        assert hi <= lo
    # An octave-up error must not turn into another DRIVING command. (It may
    # land on WIN; WIN is protected by its long hold instead.)
    for _, n in VIOLIN_NOTES:
        assert band_for(2 * note_hz(n)) in (None, "WIN")


def test_use_violin_switches_detector_and_bands(monkeypatch):
    import audio
    for name in ("PitchDetector",):
        monkeypatch.setattr(audio, name, getattr(audio, name))
    for name in ("BANDS", "WHISTLE_MIN_HZ", "WHISTLE_MAX_HZ", "TONALITY_MIN_DB",
                 "PEAK_SHARE_MIN", "BACKWARD_RAMP", "STOP_WHEN_SILENT", "GOAL_TWEETS"):
        monkeypatch.setattr(config, name, getattr(config, name))
    violin.use_violin()
    assert audio.PitchDetector is ViolinDetector
    assert config.BANDS == violin.VIOLIN_BANDS
    assert "WIN" in {b[0] for b in config.BANDS}
    assert config.BACKWARD_RAMP and config.STOP_WHEN_SILENT and not config.GOAL_TWEETS


# ---------------------------------------------------------------- violin driving

@pytest.fixture
def violin_policy(monkeypatch):
    """DrivePolicy with the violin options on (restored after the test)."""
    from policy import DrivePolicy
    monkeypatch.setattr(config, "BACKWARD_RAMP", True)
    monkeypatch.setattr(config, "STOP_WHEN_SILENT", True)
    monkeypatch.setattr(config, "GOAL_TWEETS", False)
    return DrivePolicy()


F = config.FRAME_S


def feed(p, bands, t=0.0):
    outs = []
    for b in bands:
        t += F
        outs.append(p.update(b, t))
    return outs, t


def frames(seconds):
    return int(round(seconds / F))


def test_holding_backward_backs_up_faster(violin_policy):
    outs, _ = feed(violin_policy, ["BACKWARD"] * frames(config.DEBOUNCE_S + 2 * config.SPEED_REPEAT_S + 0.05))
    levels = [o.drive.speed_level for o in outs]
    assert min(levels) == -3 and levels[-1] == -3        # -1, then -2, then -3
    assert levels == sorted(levels, reverse=True)         # only ever gets faster


def test_backward_never_faster_than_top_level(violin_policy):
    outs, _ = feed(violin_policy, ["BACKWARD"] * frames(config.DEBOUNCE_S + 10 * config.SPEED_REPEAT_S))
    assert outs[-1].drive.speed_level == -config.SPEED_LEVELS


def test_car_stops_when_violin_stops(violin_policy):
    outs, t = feed(violin_policy, ["SPEED_UP"] * frames(config.DEBOUNCE_S + 0.1))
    assert outs[-1].drive.speed_level == 1
    outs, t = feed(violin_policy, [None] * frames(config.SILENCE_STOP_S - 0.1), t)
    assert outs[-1].drive.speed_level == 1                # a bow change doesn't stop it
    outs, t = feed(violin_policy, [None] * frames(0.2), t)
    assert outs[-1].drive.speed_level == 0 and outs[-1].detail == "stopped - not playing"


def test_changing_notes_does_not_stop_the_car(violin_policy):
    outs, t = feed(violin_policy, ["SPEED_UP"] * frames(config.DEBOUNCE_S + 0.1))
    outs, t = feed(violin_policy, ["TURN_LEFT"] * frames(config.DEBOUNCE_S + 0.3), t)
    assert all(o.drive.speed_level == 1 for o in outs)    # debounce gap isn't "silence"
    assert outs[-1].drive.steer == -1


def test_win_note_needs_a_long_hold(violin_policy):
    short, t = feed(violin_policy, ["WIN"] * frames(config.WIN_HOLD_S - 0.2))
    assert not any(o.goal for o in short)
    outs, _ = feed(violin_policy, [None] * 5 + ["WIN"] * frames(config.WIN_HOLD_S + 1.0), t)
    assert sum(o.goal for o in outs) == 1                 # fires exactly once
    assert next(o for o in outs if o.goal).decision == "GOAL"


def test_tweets_do_not_win_on_violin(violin_policy):
    seq = ["SPEED_UP"] * 3 + [None] + ["SPEED_UP"] * 3 + [None] * 5
    outs, _ = feed(violin_policy, seq)
    assert not any(o.goal for o in outs)


def test_whistle_defaults_unchanged():
    # Without violin.use_violin() the whistle behaviour is exactly as before.
    assert not config.BACKWARD_RAMP and not config.STOP_WHEN_SILENT and config.GOAL_TWEETS
