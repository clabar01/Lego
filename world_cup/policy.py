"""
Decision policy: turns per-frame whistle bands into robot commands.

Input:  one band name per audio frame ("STOP", "TURN_LEFT", "TURN_RIGHT",
        "SPEED_UP", or None when the frame has no valid whistle).
Output: the drive state the robot should be in (speed level + steering), a
        label for the display, and a one-shot GOAL flag.

Three pieces, each small and separately testable:

    Debouncer       noise filter 4 - a band must hold for DEBOUNCE_S of
                    consecutive frames before it counts
    GoalDetector    spots "tweet-tweet": two short high whistles in a row
    DrivePolicy     applies confirmed commands and the no-whistle behavior
"""

import math
from dataclasses import dataclass, asdict

import config


@dataclass
class DriveState:
    """What the car should be doing. Kept tiny so it can be sent over MQTT."""
    speed_level: int = 0        # <0 = backing up, 0 = stopped .. config.SPEED_LEVELS
    steer: int = 0              # -1 = left, 0 = straight, +1 = right

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        level = max(-config.SPEED_LEVELS, min(config.SPEED_LEVELS, int(d.get("speed_level", 0))))
        steer = max(-1, min(1, int(d.get("steer", 0))))
        return cls(level, steer)


@dataclass
class PolicyOutput:
    drive: DriveState
    decision: str               # big label on the display
    detail: str = ""            # smaller explanation under it
    goal: bool = False          # True on the one frame the goal whistle completes
    fired: str | None = None    # command confirmed on THIS frame (one-shot)
    candidate: str | None = None
    progress: float = 0.0       # 0..1 toward confirming the candidate


# ----------------------------------------------------------------------------

class Debouncer:
    """Noise filter 4: a band only becomes a command after it holds steady.

    - The same band must appear for `hold_frames` frames in a row.
    - A different band restarts the count (so a whistle sliding from left to
      right doesn't fire both).
    - Up to DROPOUT_FRAMES empty frames inside a whistle are tolerated, since
      a real whistle wavers; more than that and the whistle is over.
    """

    def __init__(self, frame_s=config.FRAME_S):
        self.hold_frames = max(1, math.ceil(config.DEBOUNCE_S / frame_s - 1e-9))
        self.reset()

    def reset(self):
        self.candidate = None
        self.streak = 0
        self.dropouts = 0

    def update(self, band):
        """Returns (confirmed_band or None, just_confirmed: bool)."""
        was_confirmed = self.confirmed
        if band is None:
            self.dropouts += 1
            if self.dropouts > config.DROPOUT_FRAMES:
                self.reset()
        elif band == self.candidate:
            self.streak += 1
            self.dropouts = 0
        else:
            self.candidate, self.streak, self.dropouts = band, 1, 0
        now_confirmed = self.confirmed
        return now_confirmed, (now_confirmed is not None and not was_confirmed)

    @property
    def confirmed(self):
        return self.candidate if self.streak >= self.hold_frames else None

    @property
    def progress(self):
        return min(1.0, self.streak / self.hold_frames) if self.candidate else 0.0


class GoalDetector:
    """Recognizes the GOAL command: two short high whistles ("tweet-tweet").

    It watches raw per-frame bands (not the debounced ones, because each tweet
    is deliberately too short to pass the debouncer). It cuts the stream into
    "segments" of GOAL_BAND frames, measures each segment's length, and fires
    when two short segments arrive with a short gap between them.
    """

    def __init__(self, frame_s=config.FRAME_S):
        self.frame_s = frame_s
        self.reset()

    def reset(self):
        self._seg_start = None      # time the current high segment started
        self._seg_end = None        # end time of its last high frame
        self._gap = 0               # non-high frames since then
        self._prev = None           # (start, end) of the previous short tweet

    def update(self, band, t):
        """Feed one frame (t = time the frame ended). Returns True on GOAL."""
        if band == config.GOAL_BAND:
            if self._seg_start is None:
                self._seg_start = t - self.frame_s
            self._seg_end = t
            self._gap = 0
            return False
        if self._seg_start is None:
            # Silence: forget an old first tweet once the window has passed.
            if self._prev and t - self._prev[0] > config.GOAL_WINDOW_S:
                self._prev = None
            return False
        self._gap += 1
        if self._gap <= config.DROPOUT_FRAMES:
            return False                    # tolerate a one-frame wobble
        start, end = self._seg_start, self._seg_end
        self._seg_start = self._seg_end = None
        return self._segment_done(start, end)

    def _segment_done(self, start, end):
        duration = end - start
        short = config.GOAL_MIN_WHISTLE_S <= duration <= config.GOAL_MAX_WHISTLE_S
        if not short:
            self._prev = None                # a long whistle cancels a pending tweet
            return False
        if self._prev is not None:
            gap = start - self._prev[1]
            if (config.GOAL_MIN_GAP_S <= gap <= config.GOAL_MAX_GAP_S
                    and end - self._prev[0] <= config.GOAL_WINDOW_S):
                self._prev = None
                return True
        self._prev = (start, end)            # remember it as a possible tweet #1
        return False


class DrivePolicy:
    """Applies confirmed whistle commands, plus the no-whistle behavior.

    Confirmed whistle commands:
        SPEED_UP    +1 speed level (and +1 more every SPEED_REPEAT_S while held),
                    steering straightens
        STOP        speed level 0, straight
        TURN_LEFT   steer left for as long as the whistle is held
        TURN_RIGHT  steer right for as long as the whistle is held
        BACKWARD    back up straight for as long as the whistle is held (stops
                    BACKWARD_HOLD_S after it ends). With config.BACKWARD_RAMP it
                    backs up one level faster every SPEED_REPEAT_S, like SPEED_UP.
        WIN         (violin) held for WIN_HOLD_S -> goal, i.e. we won

    With config.STOP_WHEN_SILENT (violin) the car stops SILENCE_STOP_S after
    the last command note instead of cruising.

    No valid whistle (see README "When no whistle is detected"):
        1. A turn keeps going for TURN_HOLD_S after the whistle ends (bridges
           short breaths), then the car straightens out.
        2. Speed is kept ("cruise") - you don't have to whistle constantly.
        3. After NO_WHISTLE_TIMEOUT_S without any confirmed whistle, the car
           loses one speed level every SLOWDOWN_STEP_S until it is stopped. So
           if the whistler walks away or the mic dies, the robot stops by itself.
    """

    def __init__(self, frame_s=config.FRAME_S):
        self.frame_s = frame_s
        self.debouncer = Debouncer(frame_s)
        self.goal = GoalDetector(frame_s)
        self.reset()

    def reset(self, t=0.0):
        self.drive = DriveState()
        self._last_sound_t = t
        self._win_fired = False
        self.debouncer.reset()
        self.goal.reset()
        self._last_whistle_t = t
        self._last_turn_t = t
        self._last_back_t = t
        self._last_speed_bump_t = t
        self._last_slowdown_t = t

    def update(self, band, t):
        """Feed one frame's band (or None). Returns a PolicyOutput."""
        confirmed, just = self.debouncer.update(band)
        goal = self.goal.update(band, t) and config.GOAL_TWEETS
        d = self.drive
        fired = None
        if band is not None:
            self._last_sound_t = t
        if confirmed != "WIN":
            self._win_fired = False

        if confirmed is not None:
            self._last_whistle_t = t
            if confirmed == "SPEED_UP":
                if just or t - self._last_speed_bump_t >= config.SPEED_REPEAT_S:
                    d.speed_level = min(config.SPEED_LEVELS, max(d.speed_level, 0) + 1)
                    d.steer = 0
                    self._last_speed_bump_t = t
                    fired = confirmed
            elif confirmed == "STOP":
                d.speed_level, d.steer = 0, 0
                fired = confirmed if just else None
            elif confirmed == "BACKWARD":
                if not config.BACKWARD_RAMP:
                    d.speed_level = -1
                    fired = confirmed if just else None
                elif just or t - self._last_speed_bump_t >= config.SPEED_REPEAT_S:
                    d.speed_level = max(-config.SPEED_LEVELS, min(d.speed_level, 0) - 1)
                    self._last_speed_bump_t = t
                    fired = confirmed
                d.steer = 0
                self._last_back_t = t
            elif confirmed == "WIN":
                held = self.debouncer.streak * self.frame_s
                if not self._win_fired and held >= config.WIN_HOLD_S - 1e-9:
                    goal, self._win_fired = True, True
            elif confirmed in ("TURN_LEFT", "TURN_RIGHT"):
                d.steer = -1 if confirmed == "TURN_LEFT" else 1
                self._last_turn_t = t
                fired = confirmed if just else None
            decision = config.COMMAND_LABELS[confirmed]
            detail = (f"backing up, level {-d.speed_level}/{config.SPEED_LEVELS}"
                      if d.speed_level < 0
                      else f"speed level {d.speed_level}/{config.SPEED_LEVELS}")
        else:
            # ---- NO WHISTLE ---------------------------------------------
            decision = "NO WHISTLE"
            if d.steer != 0 and t - self._last_turn_t > config.TURN_HOLD_S:
                d.steer = 0                                  # rule 1
            if d.speed_level < 0 and t - self._last_back_t > config.BACKWARD_HOLD_S:
                d.speed_level = 0                            # backing up only while held
            silent = t - self._last_whistle_t
            if d.speed_level > 0 and silent > config.NO_WHISTLE_TIMEOUT_S:
                if t - self._last_slowdown_t >= config.SLOWDOWN_STEP_S:   # rule 3
                    d.speed_level -= 1
                    self._last_slowdown_t = t
                detail = f"slowing down (silent {silent:.0f} s)"
            elif d.speed_level > 0:
                self._last_slowdown_t = t
                detail = "holding turn" if d.steer else "cruising at current speed"
            elif d.speed_level < 0:
                detail = "backing up"
            else:
                detail = "stopped"
            if config.STOP_WHEN_SILENT and t - self._last_sound_t > config.SILENCE_STOP_S:
                d.speed_level, d.steer = 0, 0
                detail = "stopped - not playing"

        if goal:
            decision, detail = "GOAL", ("WIN note" if confirmed == "WIN"
                                        else "double high whistle")

        return PolicyOutput(drive=DriveState(d.speed_level, d.steer), decision=decision,
                            detail=detail, goal=goal, fired=fired,
                            candidate=self.debouncer.candidate,
                            progress=self.debouncer.progress)

    def force_stop(self):
        self.drive = DriveState()


class AuxPolicy:
    """Stretch goal, laptop 2: whistles control songs, light and speed profile.

    Uses the same Debouncer (so the same noise rules apply), but every
    confirmed whistle fires exactly once instead of steering:
        high   (SPEED_UP band)   -> faster profile   (slow -> medium -> fast)
        low    (STOP band)       -> slower profile
        mid-L  (TURN_LEFT band)  -> next hub light color
        mid-R  (TURN_RIGHT band) -> play the "cheer" song
    """

    ACTIONS = {
        "SPEED_UP": "PROFILE +",
        "STOP": "PROFILE -",
        "TURN_LEFT": "NEXT LIGHT",
        "TURN_RIGHT": "CHEER SONG",
        "BACKWARD": None,           # no aux action
    }

    def __init__(self, frame_s=config.FRAME_S):
        self.debouncer = Debouncer(frame_s)

    def update(self, band):
        """Returns (action or None, display label)."""
        confirmed, just = self.debouncer.update(band)
        if confirmed:
            action = self.ACTIONS.get(confirmed)
            return (action if just else None), action or "(unused)"
        return None, "NO WHISTLE"
