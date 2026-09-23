"""
Thread-safe status board shared by every part of the program.

The audio thread, robot thread and MQTT thread each write what they know here;
the display (main thread) reads a copy ~15 times per second. Nobody waits on
anybody else: writers just overwrite values under a short lock.
"""

import threading
import time
from collections import deque


class Status:
    def __init__(self, **initial):
        self._lock = threading.Lock()
        self._data = {
            "mode": "single",
            "role": "-",
            "game": "WAITING FOR START",
            "decision": "NO WHISTLE",
            "detail": "",
            "speed_level": 0,
            "steer": 0,
            "profile": "-",
            "wheels": (0, 0),
            "reflection": None,
            "proximity_threshold": None,
            "robot": "not connected",
            "mqtt": "disabled",
            "audio_extra": "",
        }
        self._data.update(initial)
        self._events = deque(maxlen=7)

    def set(self, **kw):
        with self._lock:
            self._data.update(kw)

    def get(self):
        with self._lock:
            d = dict(self._data)
            d["events"] = list(self._events)
            return d

    def log(self, msg):
        """Record an event for the on-screen log and print it to the console."""
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp}  {msg}"
        print(line, flush=True)
        with self._lock:
            self._events.append(line)
