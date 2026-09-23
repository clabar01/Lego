"""
Robot control: LEGO Education Double Motor + forward-facing Color Sensor.

    wheel_speeds()       pure function: DriveState + profile -> (left %, right %)
    ProximityDetector    "is the goalie right in front of me?" from reflection
    defense_target()     pure function: "left"/"right"/"up" -> arm angle (deg from up)
    LegoHardware         the real BLE devices (legoeducation library)
    SimRobot             stand-in with the same interface, for testing anywhere
    RobotController      background thread that owns the hardware

Every BLE call happens on the RobotController thread. Other threads only set
a target (set_drive) or queue a request (light, beep), so audio processing and
the display never wait on Bluetooth, and Bluetooth never sees two callers at
once.
"""

import queue
import threading
import time

import config
from policy import DriveState


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ----------------------------------------------------------------------------

def level_to_speed(level, profile):
    """Speed level 1..SPEED_LEVELS -> motor %, spread evenly up to max_speed."""
    if level <= 0:
        return 0
    top = config.PROFILES[profile]["max_speed"]
    if config.SPEED_LEVELS == 1:
        return top
    frac = (level - 1) / (config.SPEED_LEVELS - 1)
    return round(config.MIN_DRIVE_SPEED + frac * (top - config.MIN_DRIVE_SPEED))


def wheel_speeds(drive, profile):
    """(left, right) motor % for a drive state. Positive = forward."""
    p = config.PROFILES[profile]
    if drive.speed_level < 0:
        s = level_to_speed(-drive.speed_level, profile)
        return -s, -s               # backing up: straight, slowest level
    if drive.speed_level <= 0:
        if drive.steer == 0:
            return 0, 0
        # Turning while stopped: spin in place so you can aim the robot.
        s = p["pivot_speed"]
        return (-s, s) if drive.steer < 0 else (s, -s)
    speed = level_to_speed(drive.speed_level, profile)
    inner = round(speed * p["turn_ratio"])
    if drive.steer < 0:
        return inner, speed         # left turn: left (inner) wheel slower
    if drive.steer > 0:
        return speed, inner
    return speed, speed


def defense_target(side):
    """Arm angle for a side, in degrees from straight up. Never inside the
    forbidden bottom zone (see DEFENSE_* in config.py)."""
    if side == "up":
        return 0
    sign = config.DEFENSE_LEFT_SIGN if side == "left" else -config.DEFENSE_LEFT_SIGN
    return round(sign * config.DEFENSE_LIMIT_DEG)


def angle_from_up(absolute, up=None):
    """Motor absolute position (0-359) -> signed degrees from up, -180..179."""
    up = config.DEFENSE_UP_POSITION if up is None else up
    return (round(absolute) - up + 180) % 360 - 180


class ProximityDetector:
    """Detects an object close in front from the sensor's reflected light.

    1. The first PROXIMITY_BASELINE_SAMPLES readings (nothing in front) are
       averaged into a baseline.
    2. Trip level = max(baseline + PROXIMITY_DELTA, PROXIMITY_MIN_ABS).
    3. Reflection must be at/above the trip level PROXIMITY_HOLD_FRAMES
       readings in a row (debounced like the whistles) before `close` is True.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._samples = []
        self.baseline = None
        self.threshold = None
        self._streak = 0
        self.close = False

    def update(self, reflection):
        """Feed one reading. Returns True only on the reading where it trips."""
        if reflection is None:
            return False
        if self.baseline is None:
            self._samples.append(reflection)
            if len(self._samples) >= config.PROXIMITY_BASELINE_SAMPLES:
                self.baseline = sum(self._samples) / len(self._samples)
                self.threshold = max(self.baseline + config.PROXIMITY_DELTA,
                                     config.PROXIMITY_MIN_ABS)
            return False
        self._streak = self._streak + 1 if reflection >= self.threshold else 0
        was = self.close
        self.close = self._streak >= config.PROXIMITY_HOLD_FRAMES
        return self.close and not was


# ----------------------------------------------------------------------------
# Hardware
# ----------------------------------------------------------------------------

def _connect(device, what, serial, color):
    """Connect with the same "not ready" retry as lelib.py: the hub advertises
    a moment before it can accept a connection, so that error is transient."""
    for attempt in range(5):
        try:
            device.connect(card_color=color, card_serial=serial)
            break
        except Exception as e:
            if "not ready" in str(e).lower() and attempt < 4:
                time.sleep(1)
            else:
                raise
    if not device.connected:
        raise ConnectionError(
            f"Could not connect to {what} (card {serial}). Press the hub button to wake "
            f"it, check the Connection Card, and make sure no other script holds it.")


class LegoHardware:
    def __init__(self, card_serial=config.CARD_SERIAL, card_color=config.CARD_COLOR,
                 use_sensor=True, use_defense=False):
        import legoeducation as le      # imported here so laptops without BLE can run stations
        self.le = le
        self.card_serial = card_serial
        self.card_color = getattr(le, f"LEGO_COLOR_{card_color}")
        self.use_sensor = use_sensor
        self.dm = le.DoubleMotor()
        self.sensor = le.ColorSensor() if use_sensor else None
        self.arm = le.SingleMotor() if use_defense else None
        self.name = f"LEGO card {card_serial}"

    def connect(self):
        print(f"Connecting to Double Motor (card {self.card_serial})...")
        _connect(self.dm, "Double Motor", self.card_serial, self.card_color)
        print("Double Motor connected.")
        if self.sensor:
            print(f"Connecting to Color Sensor (card {self.card_serial})...")
            _connect(self.sensor, "Color Sensor", self.card_serial, self.card_color)
            print("Color Sensor connected.")
        if self.arm:
            print(f"Connecting to Single Motor / defense arm (card {self.card_serial})...")
            _connect(self.arm, "Single Motor", self.card_serial, self.card_color)
            self._zero_arm()

    def _zero_arm(self):
        """Make the motor's RELATIVE position = signed degrees from straight up.
        The relative counter doesn't wrap at 360, so moving between -110 and
        +110 always passes through 0 (the top), never through the bottom."""
        absolute = float("nan")
        for _ in range(20):                 # the first notification takes a moment
            absolute = self.arm.motor.absolutePosition
            if absolute == absolute:        # not NaN
                break
            time.sleep(0.1)
        if absolute == absolute:
            offset = angle_from_up(absolute)
            print(f"Defense arm connected: absolute {absolute:.0f}, {offset:+d} deg from up.")
        else:
            offset = 0
            print("Defense arm connected, position unknown - assuming it points UP.")
        self.arm.motor_reset_relative_position(position=offset)
        self.arm.motor_set_end_state(self.le.MOTOR_END_STATE_HOLD)   # hold against hits

    def arm_to(self, angle):
        if self.arm:
            self.arm.motor_run_to_relative_position(int(angle), speed=config.DEFENSE_SPEED,
                                                    blocking=False)

    def arm_set_up_here(self):
        """The arm points straight up right now: make this 0. Returns the
        absolute position to put in config.DEFENSE_UP_POSITION."""
        if not self.arm:
            return None
        self.arm.motor_stop()
        self.arm.motor_reset_relative_position(position=0)
        return self.arm.motor.absolutePosition

    def set_wheels(self, left, right):
        le = self.le
        if left == 0 and right == 0:
            self.dm.motor_stop(motor=le.MOTOR_BOTH)
            return
        # Signed speed sets the direction; the flips undo the mirrored mounting.
        self.dm.motor_set_speed(config.LEFT_FLIP * left, motor=le.MOTOR_LEFT)
        self.dm.motor_set_speed(config.RIGHT_FLIP * right, motor=le.MOTOR_RIGHT)
        self.dm.motor_run(direction=le.MOTOR_MOVE_DIRECTION_COUNTERCLOCKWISE, motor=le.MOTOR_LEFT)
        self.dm.motor_run(direction=le.MOTOR_MOVE_DIRECTION_COUNTERCLOCKWISE, motor=le.MOTOR_RIGHT)

    def stop(self):
        self.dm.motor_stop(motor=self.le.MOTOR_BOTH)

    def reflection(self):
        """Reflected light 0-100 %, or None before the first reading arrives."""
        if not self.sensor:
            return None
        r = self.sensor.sensor.reflection
        return None if r != r else float(r)          # NaN check

    def light(self, color, pattern="SOLID"):
        self.dm.light_color(getattr(self.le, f"LEGO_COLOR_{color}"),
                            pattern=getattr(self.le, f"LIGHT_PATTERN_{pattern}"))

    def beep(self, frequency):
        self.dm.beep(frequency=int(max(0, min(2700, frequency))), blocking=False)

    def disconnect(self):
        for dev in (self.dm, self.sensor, self.arm):
            if dev is not None:
                try:
                    dev.disconnect()
                except Exception as e:
                    print(f"disconnect: {e!r}")


class SimRobot:
    """Pretend robot: same interface as LegoHardware, prints instead of driving.
    Press 'o' in the display to put a fake obstacle in front of the sensor."""

    def __init__(self, use_sensor=True, use_defense=False):
        self.use_sensor = use_sensor
        self.use_defense = use_defense
        self.obstacle = False
        self.name = "simulated"
        self._last = None

    def connect(self):
        print("Using SIMULATED robot (no Bluetooth).")

    def set_wheels(self, left, right):
        if (left, right) != self._last:
            print(f"[sim] wheels L{left:+d} R{right:+d}")
            self._last = (left, right)

    def stop(self):
        self.set_wheels(0, 0)

    def reflection(self):
        if not self.use_sensor:
            return None
        return 70.0 if self.obstacle else 6.0

    def light(self, color, pattern="SOLID"):
        print(f"[sim] hub light {color} {pattern}")

    def beep(self, frequency):
        pass

    def arm_to(self, angle):
        if self.use_defense:
            print(f"[sim] defense arm -> {angle:+d} deg from up")

    def arm_set_up_here(self):
        return 0 if self.use_defense else None

    def disconnect(self):
        pass


# ----------------------------------------------------------------------------
# Controller thread
# ----------------------------------------------------------------------------

class RobotController:
    def __init__(self, hw, status, profile=config.DEFAULT_PROFILE):
        self.hw = hw
        self.status = status
        self.profile = profile
        self.proximity = ProximityDetector()
        self.on_proximity = None        # callback(), called on the robot thread
        self.proximity_enabled = False

        self._lock = threading.Lock()
        self._target = DriveState()
        self._target_expires = None     # stretch mode: stop if the drive laptop goes quiet
        self._halt = False
        self._requests = queue.Queue()
        self._last_sent = None
        self._last_send_t = 0.0
        self._stop = threading.Event()
        self._thread = None
        status.set(profile=profile, robot=hw.name)

    # -- called from any thread ------------------------------------------------
    def set_drive(self, drive, ttl=None):
        with self._lock:
            self._target = DriveState(drive.speed_level, drive.steer)
            self._target_expires = None if ttl is None else time.monotonic() + ttl
            self._halt = False

    def halt(self):
        """Stop the motors as soon as possible and hold them at zero."""
        with self._lock:
            self._target = DriveState()
            self._halt = True

    def set_profile(self, name):
        if name in config.PROFILES:
            with self._lock:
                self.profile = name
                self._last_sent = None          # force a resend at the new speed
            self.status.set(profile=name)

    def light(self, color, pattern="SOLID"):
        self._requests.put(("light", color, pattern))

    def beep(self, frequency):
        self._requests.put(("beep", frequency))

    def defend(self, side):
        """Swing the defense arm: side = "left", "right" or "up"."""
        self._requests.put(("arm", side))

    def arm_set_up_here(self):
        self._requests.put(("arm_zero",))

    # -- lifecycle -----------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="robot", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        try:
            self.hw.stop()
        except Exception as e:
            print(f"stop: {e!r}")
        self.hw.disconnect()

    # -- robot thread ----------------------------------------------------------------
    def _run(self):
        period = 1.0 / config.ROBOT_LOOP_HZ
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick(t0)
            except Exception as e:               # BLE hiccup: log, keep going
                self.status.log(f"robot error: {e!r}")
                time.sleep(0.5)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def _tick(self, now):
        # 1) Proximity first, so a catch stops the car before any new speed goes out.
        refl = self.hw.reflection()
        tripped = self.proximity.update(refl)
        self.status.set(reflection=refl, proximity_threshold=self.proximity.threshold)
        if tripped and self.proximity_enabled and self.on_proximity:
            self.hw.stop()
            self._last_sent = (0, 0)
            self.on_proximity()

        # 2) Queued light / beep / defense arm requests.
        while True:
            try:
                req = self._requests.get_nowait()
            except queue.Empty:
                break
            if req[0] == "light":
                self.hw.light(req[1], req[2])
            elif req[0] == "beep":
                self.hw.beep(req[1])
            elif req[0] == "arm":
                self.hw.arm_to(defense_target(req[1]))
                self.status.set(arm=req[1])
            elif req[0] == "arm_zero":
                absolute = self.hw.arm_set_up_here()
                if absolute is not None:
                    self.status.log(f"arm: UP set here. For next time put "
                                    f"DEFENSE_UP_POSITION = {absolute:.0f} in config.py")

        # 3) Drive.
        with self._lock:
            drive, expires, halt, profile = (self._target, self._target_expires,
                                             self._halt, self.profile)
        if halt:
            if self._last_sent != (0, 0):
                self.hw.stop()
                self._last_sent = (0, 0)
                self.status.set(wheels=(0, 0))
            return
        if expires is not None and now > expires:
            drive = DriveState()                 # link lost -> stop
        speeds = wheel_speeds(drive, profile)
        # BLE throttle: only send when something changed, and not too often.
        if speeds != self._last_sent and now - self._last_send_t >= config.SEND_INTERVAL_S:
            self.hw.set_wheels(*speeds)
            self._last_sent, self._last_send_t = speeds, now
            self.status.set(wheels=speeds)
