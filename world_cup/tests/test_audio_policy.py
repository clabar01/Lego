"""Offline tests for pitch detection and the decision policy (no mic, no robot).

Run from the world_cup folder:  python -m pytest tests
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config                                        # noqa: E402
from audio import PitchDetector, band_for, ambient_threshold   # noqa: E402
from policy import Debouncer, DrivePolicy, GoalDetector, AuxPolicy  # noqa: E402

RNG = np.random.default_rng(0)
F = config.FRAME_S


def frame(rate, fn):
    n = int(round(rate * F))
    t = np.arange(n) / rate
    return fn(t).astype(np.float32)


def whistle(rate, hz, amp=0.2, noise=0.002):
    return frame(rate, lambda t: amp * np.sin(2 * np.pi * hz * t)
                 + noise * RNG.standard_normal(len(t)))


def speechlike(rate, f0=180, amp=0.2):
    # Voiced speech: a low fundamental with many harmonics + breath noise.
    def fn(t):
        x = sum((amp / k) * np.sin(2 * np.pi * f0 * k * t + k) for k in range(1, 25))
        return x + 0.02 * RNG.standard_normal(len(t))
    return frame(rate, fn)


def detector(rate):
    d = PitchDetector(rate, int(round(rate * F)))
    d.threshold_db = -50
    return d


# ---------------------------------------------------------------- detection

@pytest.mark.parametrize("rate", [48000, 44100, 24000, 16000])
@pytest.mark.parametrize("hz", [700, 1100, 1300, 1480, 1700, 2000, 2500])
def test_detects_whistle_pitch_at_any_rate(rate, hz):
    det = detector(rate).analyze(whistle(rate, hz))
    assert det.loud and det.tonal
    assert abs(det.pitch_hz - hz) < 5
    assert det.band == band_for(hz)


@pytest.mark.parametrize("rate", [48000, 16000])
def test_breathy_wobbly_whistle_still_accepted(rate):
    # Real whistles have breath noise and a few-Hz vibrato of ~±20 Hz.
    x = frame(rate, lambda t: 0.1 * np.sin(2 * np.pi * (1800 * t + 20 / 6 * np.sin(2 * np.pi * 6 * t)))
              + 0.01 * RNG.standard_normal(len(t)))
    det = detector(rate).analyze(x)
    assert det.tonal, (det.tonality_db, det.peak_share)


def test_quiet_whistle_rejected_by_volume():
    det = detector(48000).analyze(whistle(48000, 2000, amp=0.0005, noise=0))
    assert not det.loud and det.band is None


@pytest.mark.parametrize("rate", [48000, 16000])
def test_white_noise_rejected_by_tonality(rate):
    x = frame(rate, lambda t: 0.3 * RNG.standard_normal(len(t)))
    det = detector(rate).analyze(x)
    assert det.loud and not det.tonal and det.band is None


@pytest.mark.parametrize("f0", [110, 180, 250])
def test_speech_rejected(f0):
    det = detector(48000).analyze(speechlike(48000, f0))
    assert det.band is None, (det.tonality_db, det.peak_share)


def test_low_hum_ignored_by_bandpass():
    # A loud 120 Hz motor hum plus a modest whistle: the hum is out of band.
    rate = 48000
    x = frame(rate, lambda t: 0.8 * np.sin(2 * np.pi * 120 * t)
              + 0.1 * np.sin(2 * np.pi * 2000 * t))
    det = detector(rate).analyze(x)
    assert abs(det.pitch_hz - 2000) < 5 and det.band == "SPEED_UP"


def test_guard_gap_is_no_band():
    assert band_for(1025) is None           # below C6
    assert band_for(1200) is None           # guard gap STOP / BACKWARD
    assert band_for(1100) == "STOP"
    assert band_for(1300) == "BACKWARD"
    assert band_for(2200) is None           # above C7


def test_ambient_threshold_floor():
    assert ambient_threshold([-100] * 10) == config.MIN_THRESHOLD_DB
    assert ambient_threshold([-40] * 10) == -40 + config.NOISE_MARGIN_DB


# ---------------------------------------------------------------- policy

def feed(policy, bands, t0=0.0):
    """Feed a list of bands, one per frame. Returns list of outputs."""
    out, t = [], t0
    for b in bands:
        t += F
        out.append(policy.update(b, t))
    return out, t


def n(seconds):
    return int(round(seconds / F))


def test_debounce_needs_hold_frames():
    d = Debouncer()
    for _ in range(d.hold_frames - 1):
        conf, _ = d.update("STOP")
        assert conf is None
    conf, just = d.update("STOP")
    assert conf == "STOP" and just


def test_single_noisy_frame_does_nothing():
    p = DrivePolicy()
    outs, _ = feed(p, [None] * 5 + ["SPEED_UP"] + [None] * 5)
    assert all(o.drive.speed_level == 0 for o in outs)


def test_band_switch_restarts_debounce():
    p = DrivePolicy()
    half = n(config.DEBOUNCE_S) // 2 + 1
    outs, _ = feed(p, ["TURN_LEFT"] * half + ["TURN_RIGHT"] * half)
    assert all(o.fired is None for o in outs)


def test_speed_up_then_repeat_while_held():
    p = DrivePolicy()
    outs, _ = feed(p, ["SPEED_UP"] * n(config.DEBOUNCE_S + config.SPEED_REPEAT_S + 0.1))
    assert outs[-1].drive.speed_level == 2


def test_turn_holds_briefly_then_straightens_and_cruises():
    p = DrivePolicy()
    feed(p, ["SPEED_UP"] * n(config.DEBOUNCE_S) + [None] * 3)
    outs, t = feed(p, ["TURN_LEFT"] * n(config.DEBOUNCE_S + 0.2))
    assert outs[-1].drive.steer == -1
    outs, t = feed(p, [None] * max(1, n(config.TURN_HOLD_S) - 1), t)
    assert outs[-1].drive.steer == -1          # still holding the turn
    outs, t = feed(p, [None] * n(0.3), t)
    assert outs[-1].drive.steer == 0 and outs[-1].drive.speed_level == 1


def test_silence_slows_to_stop():
    p = DrivePolicy()
    feed(p, ["SPEED_UP"] * n(config.DEBOUNCE_S + 2 * config.SPEED_REPEAT_S + 0.05))
    assert p.drive.speed_level == 3
    outs, _ = feed(p, [None] * n(config.NO_WHISTLE_TIMEOUT_S - 0.2))
    assert outs[-1].drive.speed_level == 3     # cruising
    outs, _ = feed(p, [None] * n(3 * config.SLOWDOWN_STEP_S + 0.5), config.NO_WHISTLE_TIMEOUT_S)
    assert outs[-1].drive.speed_level == 0


def test_stop_whistle():
    p = DrivePolicy()
    feed(p, ["SPEED_UP"] * n(config.DEBOUNCE_S))
    outs, _ = feed(p, [None] * 3 + ["STOP"] * n(config.DEBOUNCE_S))
    assert outs[-1].drive.speed_level == 0 and outs[-1].decision == "STOP"


def tweet(sec=0.2):
    return ["SPEED_UP"] * n(sec)


def test_goal_double_whistle():
    p = DrivePolicy()
    outs, _ = feed(p, tweet() + [None] * n(0.2) + tweet() + [None] * 3)
    assert sum(o.goal for o in outs) == 1
    assert all(o.drive.speed_level == 0 for o in outs)   # tweets never sped up


def test_goal_rejects_long_whistles_and_long_gaps():
    g = GoalDetector()
    seqs = [
        tweet(0.6) + [None] * n(0.2) + tweet(),        # first one too long
        tweet() + [None] * n(0.9) + tweet(),           # gap too long
        tweet() + [None] * 3,                          # only one tweet
    ]
    for seq in seqs:
        g.reset()
        t, hits = 0.0, 0
        for b in seq + [None] * 5:
            t += F
            hits += g.update(b, t)
        assert hits == 0, seq


def test_single_dropout_does_not_split_tweet():
    g = GoalDetector()
    seq = ["SPEED_UP"] * 3 + [None] + ["SPEED_UP"] * 3 + [None] * 5
    t, hits = 0.0, 0
    for b in seq:
        t += F
        hits += g.update(b, t)
    assert hits == 0     # merged into ONE 0.35 s whistle, not two tweets


def test_aux_policy_fires_once_per_whistle():
    a = AuxPolicy()
    actions = [a.update("TURN_LEFT")[0] for _ in range(n(1.5))]
    assert [x for x in actions if x] == ["NEXT LIGHT"]


def test_backward_while_held_then_stops():
    p = DrivePolicy()
    outs, _ = feed(p, ["BACKWARD"] * n(config.DEBOUNCE_S + 0.3))
    assert outs[-1].drive.speed_level == -1 and outs[-1].decision == "BACKWARD"
    outs, _ = feed(p, [None] * n(config.BACKWARD_HOLD_S + 0.2), t0=1.0)
    assert outs[-1].drive.speed_level == 0


def test_speed_up_from_reverse_goes_forward():
    p = DrivePolicy()
    feed(p, ["BACKWARD"] * n(config.DEBOUNCE_S + 0.1) + [None] * 2)
    outs, _ = feed(p, ["SPEED_UP"] * n(config.DEBOUNCE_S + 0.1), t0=1.0)
    assert outs[-1].drive.speed_level == 1
