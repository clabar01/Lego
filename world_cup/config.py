"""
Every tunable number for the World Cup robot lives in this file.

Change values here, not inside the other modules. The sections are grouped by
the part of the system they affect:

    HARDWARE      which LEGO devices to connect to, wheel directions
    AUDIO         how audio is captured
    NOISE MASKING the four noise filters (bandpass, volume, tonality, debounce)
    BANDS         which whistle pitch means which command
    TIMING        debounce, the "goal" double whistle, no-whistle behavior
    DRIVING       speed levels and the slow/medium/fast profiles
    PROXIMITY     light-sensor "goalie is in front of me" detection
    MQTT          broker, topics, and the message strings agreed with the opponent
    SONGS/DISPLAY cosmetic settings
"""

# ============================================================================
# HARDWARE
# ============================================================================

# Connection Card tapped on BOTH the Double Motor and the Color Sensor.
# Classmates' robots are on the same airwaves, so never connect to "the first
# device found" - always filter by this serial + color.
CARD_SERIAL = "0999"
CARD_COLOR = "RED"          # name of a legoeducation LEGO_COLOR_* constant

# The two motors are mounted mirrored on the chassis, so the same signed speed
# turns one wheel forward and the other backward. Flip one side to fix that.
# (Same values that worked on the gesture car.) If the car spins in place when
# it should drive straight, flip RIGHT_FLIP to 1 and LEFT_FLIP to -1.
LEFT_FLIP = 1
RIGHT_FLIP = -1

ROBOT_LOOP_HZ = 20          # how often the robot thread sends speeds / reads the sensor
SEND_INTERVAL_S = 0.1       # never send motor commands faster than this over BLE

# ============================================================================
# AUDIO CAPTURE
# ============================================================================

# Length of one analysis frame. Every FRAME_S seconds we read a block of audio,
# FFT it, and make one decision. 50 ms = 20 decisions per second.
FRAME_S = 0.05

# Sample rates to try if the device's own default rate fails. AirPods in
# microphone ("call") mode drop to 16 kHz or 24 kHz; the laptop mic is 48 kHz.
# The pitch detector works at any of these as long as Nyquist (rate / 2) is
# above WHISTLE_MAX_HZ.
FALLBACK_SAMPLE_RATES = (48000, 44100, 32000, 24000, 22050, 16000, 8000)

# ============================================================================
# NOISE MASKING  (see README "How we masked out unwanted noise")
# ============================================================================

# 1) BANDPASS - only look for a pitch inside this range. Human whistles live
#    roughly between 500 Hz and 4 kHz. Motor hum, footsteps, and the low part
#    of speech are below; hiss and clicks spread far above.
WHISTLE_MIN_HZ = 500
WHISTLE_MAX_HZ = 4000

# 2) VOLUME THRESHOLD - the strongest in-band peak must be louder than this.
#    At startup we listen to the room for NOISE_CALIBRATION_S seconds (don't
#    whistle!) and set the threshold to (typical ambient peak + NOISE_MARGIN_DB).
#    It is never allowed below MIN_THRESHOLD_DB. Levels are dBFS: 0 dB is the
#    loudest the mic can record, -60 dB is very quiet.
NOISE_CALIBRATION_S = 2.0
NOISE_MARGIN_DB = 12.0
MIN_THRESHOLD_DB = -65.0
DEFAULT_THRESHOLD_DB = -45.0    # used only if calibration is skipped (--noise-db)

# 3) TONALITY - a whistle is ONE strong, narrow peak. Speech, crowd noise and
#    motor noise spread energy over many frequencies. We compare the peak's
#    power to the AVERAGE power across the band (peak-to-average ratio, in dB).
#    Pure whistle: ~20-30 dB. Talking / crowd / motors: usually under ~12 dB.
TONALITY_MIN_DB = 14.0
#    Peak-to-average alone lets through HARMONIC sounds (a voice, a motor
#    whine): each harmonic is a narrow peak too. So we also require the peak
#    (+/- PEAK_WIDTH_HZ around it) to hold at least PEAK_SHARE_MIN of all the
#    energy in the band. A whistle is ~80-95 %; a voice splits its energy over
#    many harmonics and scores far lower.
PEAK_WIDTH_HZ = 60
PEAK_SHARE_MIN = 0.5

# 4) DEBOUNCE - see TIMING below (DEBOUNCE_S).

# ============================================================================
# FREQUENCY BANDS -> COMMANDS
# ============================================================================
# Each band is (command, low_hz, high_hz). A detected pitch in a band means
# that command. The small gaps BETWEEN bands are deliberate "guard bands": a
# pitch that lands on a boundary counts as no whistle instead of flickering
# between two commands. Run `python main.py --calibrate`, whistle your low,
# middle and high notes, and adjust these numbers to fit YOUR whistle.
# All five bands fit between C6 (1047 Hz) and C7 (2093 Hz), one octave, each
# about 2.4 semitones wide. The guard gaps grow with pitch because a whistle's
# wobble does too.
BANDS = [
    ("STOP",       1047, 1180),  # lowest whistle   (C6 - D6)
    ("BACKWARD",   1225, 1355),  # low middle       (D#6 - E6)
    ("TURN_LEFT",  1405, 1555),  # middle           (F6 - G6)
    ("TURN_RIGHT", 1615, 1785),  # high middle      (G#6 - A6)
    ("SPEED_UP",   1860, 2093),  # highest whistle  (A#6 - C7)
]

# Human-friendly labels shown on screen.
COMMAND_LABELS = {
    "STOP": "STOP",
    "BACKWARD": "BACKWARD",
    "DEFEND_LEFT": "ARM LEFT",      # defense laptop only (DEFENSE_BANDS)
    "DEFEND_RIGHT": "ARM RIGHT",
    "TURN_LEFT": "TURN LEFT",
    "TURN_RIGHT": "TURN RIGHT",
    "SPEED_UP": "SPEED UP",
}

# ============================================================================
# TIMING
# ============================================================================

# A pitch must stay in the same band for this long (consecutive frames) before
# it becomes a command. One noisy frame can never change what the robot does.
DEBOUNCE_S = 0.45

# Frames of "nothing" allowed inside a whistle without breaking it (whistles
# waver; a single dropped frame shouldn't restart the debounce count).
DROPOUT_FRAMES = 1

# Holding the high whistle keeps speeding up, one level every SPEED_REPEAT_S.
SPEED_REPEAT_S = 0.8

# GOAL command: two SHORT high whistles in a row ("tweet-tweet").
# Each whistle is shorter than DEBOUNCE_S, so on its own it never triggers
# SPEED UP - that's what keeps the two commands apart.
GOAL_BAND = "SPEED_UP"
GOAL_MIN_WHISTLE_S = 0.08   # shorter than this = a click / glitch, not a whistle
GOAL_MAX_WHISTLE_S = 0.40   # longer than this = a normal whistle, not a "tweet"
GOAL_MIN_GAP_S = 0.08       # silence between the two tweets...
GOAL_MAX_GAP_S = 0.60       # ...must be in this range
GOAL_WINDOW_S = 1.5         # start of tweet 1 to end of tweet 2

# NO-WHISTLE BEHAVIOR (see README "When no whistle is detected")
#   - A turn continues for TURN_HOLD_S after the whistle ends, then the car
#     straightens out and keeps cruising at its current speed.
#   - After NO_WHISTLE_TIMEOUT_S with no confirmed whistle, the car slows down
#     one speed level every SLOWDOWN_STEP_S until it stops.
TURN_HOLD_S = 0.5
BACKWARD_HOLD_S = 0.5       # backing up stops this long after the whistle ends
NO_WHISTLE_TIMEOUT_S = 4.0
SLOWDOWN_STEP_S = 1.0

# ============================================================================
# DRIVING
# ============================================================================

SPEED_LEVELS = 5            # each SPEED UP whistle adds one level (0 = stopped)
MIN_DRIVE_SPEED = 20        # motor % at level 1 (below ~20% the car may not move)

# Speed profiles (slow / medium / fast). In stretch-goal mode, laptop 2 picks
# the profile; otherwise use --profile or keys 1/2/3 in the display window.
#   max_speed    motor % at the top speed level
#   turn_ratio   inner wheel speed as a fraction of the outer wheel while
#                turning (1 = no turn, 0 = inner wheel stopped, <0 = spins back)
#   pivot_speed  motor % used when turning while stopped (spin in place)
PROFILES = {
    "slow":   {"max_speed": 45,  "turn_ratio": 0.35, "pivot_speed": 25},
    "medium": {"max_speed": 70,  "turn_ratio": 0.15, "pivot_speed": 35},
    "fast":   {"max_speed": 100, "turn_ratio": -0.2, "pivot_speed": 45},
}
DEFAULT_PROFILE = "medium"

# ============================================================================
# PROXIMITY (ball role: "the goalie is right in front of me")
# ============================================================================
# The forward-facing color sensor reports reflected light, 0-100 %. When an
# object is close in front, much more of the sensor's own light bounces back,
# so reflection jumps. We measure a baseline with nothing in front at startup,
# then trigger when reflection >= max(baseline + PROXIMITY_DELTA,
# PROXIMITY_MIN_ABS) for PROXIMITY_HOLD_FRAMES readings in a row.
PROXIMITY_BASELINE_SAMPLES = 20
PROXIMITY_DELTA = 15
PROXIMITY_MIN_ABS = 20
PROXIMITY_HOLD_FRAMES = 2

# ============================================================================
# DEFENSE ARM (Single Motor on top of the robot, run by the defense laptop)
# ============================================================================
# The defense laptop (--mode defense) only needs two whistles, so it uses its
# own two wide bands instead of BANDS: low half of C6-C7 = arm left, high half
# = arm right. Each whistle swings the arm all the way to that side.
DEFENSE_BANDS = [
    ("DEFEND_LEFT",  1047, 1480),   # low whistle  (C6 - F#6)
    ("DEFEND_RIGHT", 1570, 2093),   # high whistle (G6 - C7)
]

# Angles are measured from the arm pointing straight UP (0 deg). The light
# sensor sits under the motor, so the arm must never enter the bottom third
# of the circle: DEFENSE_FORBIDDEN_DEG centered on straight down. The arm
# swings between -DEFENSE_LIMIT_DEG and +DEFENSE_LIMIT_DEG, and always moves
# THROUGH THE TOP to get from one side to the other (never through the bottom).
DEFENSE_UP_POSITION = 0         # motor's absolute position (0-359) when the arm points
                                # straight up. Find it: point the arm up by hand on the
                                # robot laptop and press z; it prints the number.
DEFENSE_FORBIDDEN_DEG = 120     # bottom third of the circle
DEFENSE_MARGIN_DEG = 10         # extra safety gap before the forbidden zone
DEFENSE_LIMIT_DEG = 180 - DEFENSE_FORBIDDEN_DEG / 2 - DEFENSE_MARGIN_DEG   # = 110
DEFENSE_SPEED = 60              # motor % while swinging
DEFENSE_LEFT_SIGN = -1          # if "ARM LEFT" swings right, change this to 1

# ============================================================================
# MQTT
# ============================================================================
# Development broker. Swap these two lines for the professor's broker.
BROKER_HOST = "test.mosquitto.org"
BROKER_PORT = 1883

GAME_TOPIC = "ME193/Rogers"

# The whole class shares GAME_TOPIC, so our messages carry our match name.
# Change MATCH_PREFIX here and every message/topic below follows.
MATCH_PREFIX = "robot"

MSG_START = "start"                         # sent by the professor / referee
MSG_READY = f"{MATCH_PREFIX}_ready"         # each robot, when waiting for start
MSG_CAUGHT = f"{MATCH_PREFIX}_caught"       # ball -> goalie stopped the ball
MSG_SCORED = f"{MATCH_PREFIX}_scored"       # ball -> ball reached the goal
MSG_RESET = f"{MATCH_PREFIX}_reset"         # optional: back to "waiting for start"

# Stretch goal: two laptops, one robot.
DRIVE_TOPIC = f"{GAME_TOPIC}/{MATCH_PREFIX}/drive"
AUX_TOPIC = f"{GAME_TOPIC}/{MATCH_PREFIX}/aux"
DRIVE_PUBLISH_HZ = 10           # drive laptop re-sends its state this often
DRIVE_LINK_TIMEOUT_S = 1.0      # robot stops if the drive laptop goes quiet this long
DEFENSE_TOPIC = f"{GAME_TOPIC}/{MATCH_PREFIX}/defense"   # defense laptop -> arm

# ============================================================================
# SONGS & DISPLAY
# ============================================================================
SONG_SAMPLE_RATE = 44100
SONG_VOLUME = 0.35              # 0..1, laptop speaker volume of the songs
# Songs that ALSO beep on the robot's Double Motor buzzer, note by note, at the
# same time as the laptop plays them (turn off with --no-hub-songs).
HUB_SONGS = ("victory", "death")
HUB_LIGHT_COLORS = ["BLUE", "GREEN", "YELLOW", "MAGENTA", "AZURE", "ORANGE", "WHITE"]

DISPLAY_FPS = 15
DISPLAY_MAX_HZ = 5000           # right edge of the spectrum plot
PITCH_HISTORY_S = 6.0           # width of the "pitch over time" plot


def validate():
    """Catch settings that would silently break the logic."""
    assert WHISTLE_MIN_HZ < WHISTLE_MAX_HZ
    assert GOAL_MAX_WHISTLE_S < DEBOUNCE_S, (
        "Each goal 'tweet' must be shorter than DEBOUNCE_S, otherwise a tweet "
        "would also trigger a normal SPEED UP.")
    assert GOAL_MIN_GAP_S > DROPOUT_FRAMES * FRAME_S, (
        "The gap between goal tweets must be longer than the dropout tolerance, "
        "otherwise the two tweets merge into one whistle.")
    assert GOAL_BAND in {b[0] for b in BANDS}
    for name, lo, hi in BANDS:
        assert WHISTLE_MIN_HZ <= lo < hi <= WHISTLE_MAX_HZ, f"band {name} outside bandpass"
    assert DEFAULT_PROFILE in PROFILES
    assert 0 < DEFENSE_LIMIT_DEG <= 180 - DEFENSE_FORBIDDEN_DEG / 2, (
        "DEFENSE_LIMIT_DEG would let the arm into the forbidden bottom zone")
    for name, lo, hi in DEFENSE_BANDS:
        assert WHISTLE_MIN_HZ <= lo < hi <= WHISTLE_MAX_HZ, f"band {name} outside bandpass"


validate()
