"""
Live display (matplotlib). Runs on the MAIN thread - macOS requires GUI code
there - and only READS shared state, so it can never block audio or the robot.

Layout
    left, top      waveform of the latest audio frame
    left, middle   spectrum (dBFS) with the bandpass range, command bands,
                   volume threshold and the detected pitch
    left, bottom   pitch over the last few seconds, colored by command band
    right          current decision, game state, driving state, filter values,
                   connection status and an event log

If there is no audio (robot-side script in stretch mode) only the right-hand
status panel is drawn.
"""

import time

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.patches import Rectangle

import config

# Colors: text stays neutral ink; each command band has one fixed hue
# (validated categorical palette, slots 1-4, fixed order).
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#8a8984"
SURFACE = "#fcfcfb"
GRID = "#e6e5e0"
BAND_COLORS = {
    "STOP": "#2a78d6",        # blue
    "TURN_LEFT": "#eb6834",   # orange
    "TURN_RIGHT": "#1baf7a",  # aqua
    "SPEED_UP": "#eda100",    # yellow
}
GOOD = "#008300"
BAD = "#e34948"
REJECTED = "#b9b8b2"

GAME_COLORS = {"PLAYING": GOOD, "WON": GOOD, "LOST": BAD}


class Display:
    def __init__(self, status, audio=None, keys=None, title="ME193 World Cup"):
        """
        status  Status board to read
        audio   AudioEngine (or None for a status-only window)
        keys    {key_char: callable} extra keyboard shortcuts
        """
        self.status = status
        self.audio = audio
        self.keys = keys or {}
        self.quit_requested = False

        # Disable matplotlib's own shortcuts that clash with ours (s = save, etc.)
        for k in ("keymap.save", "keymap.quit", "keymap.fullscreen", "keymap.grid",
                  "keymap.yscale", "keymap.xscale", "keymap.home", "keymap.back",
                  "keymap.forward", "keymap.pan", "keymap.zoom"):
            if k in matplotlib.rcParams:
                matplotlib.rcParams[k] = []
        plt.rcParams.update({
            "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
            "axes.edgecolor": GRID, "axes.labelcolor": INK_2,
            "xtick.color": INK_2, "ytick.color": INK_2, "text.color": INK,
            "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
            "font.size": 9, "toolbar": "None",
        })
        if audio:
            self.fig = plt.figure(figsize=(13, 7.5))
            gs = self.fig.add_gridspec(3, 2, width_ratios=[2.3, 1], hspace=0.6, wspace=0.22,
                                       left=0.06, right=0.98, top=0.88, bottom=0.07)
            self.ax_wave = self.fig.add_subplot(gs[0, 0])
            self.ax_spec = self.fig.add_subplot(gs[1, 0])
            self.ax_hist = self.fig.add_subplot(gs[2, 0])
            self.ax_text = self.fig.add_subplot(gs[:, 1])
            self._setup_audio_axes()
        else:
            self.fig = plt.figure(figsize=(5.5, 7.5))
            self.ax_text = self.fig.add_axes([0.04, 0.03, 0.92, 0.9])
        self.fig.suptitle(title, x=0.06 if audio else 0.5, y=0.975, ha="left" if audio else "center",
                          fontsize=13, fontweight="bold", color=INK)
        self._setup_text_panel()
        try:
            self.fig.canvas.manager.set_window_title(title)
        except AttributeError:
            pass
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)
        self.fig.canvas.mpl_connect("close_event", lambda e: self._request_quit())

    # ------------------------------------------------------------------ setup
    def _setup_audio_axes(self):
        rate = self.audio.sample_rate
        n = self.audio.frame_len
        max_hz = min(config.DISPLAY_MAX_HZ, rate / 2)

        # Waveform
        ax = self.ax_wave
        ax.set_title("Microphone waveform (latest frame)", loc="left", color=INK_2)
        t_ms = [i * 1000.0 / rate for i in range(n)]
        (self.wave_line,) = ax.plot(t_ms, [0] * n, color=INK_2, lw=1)
        ax.set_xlim(0, t_ms[-1])
        ax.set_ylim(-0.1, 0.1)
        ax.set_xlabel("ms")

        # Spectrum
        ax = self.ax_spec
        ax.set_title("Spectrum: bands, volume threshold, detected pitch", loc="left", color=INK_2)
        ax.set_xlim(0, max_hz)
        ax.set_ylim(-110, 0)
        ax.set_ylabel("dBFS")
        ax.set_xlabel("Hz")
        # Outside the bandpass range = ignored (hatched gray)
        for lo, hi in [(0, config.WHISTLE_MIN_HZ), (config.WHISTLE_MAX_HZ, max_hz)]:
            if hi > lo:
                ax.axvspan(lo, hi, color="#efeeea", hatch="//", ec=GRID, lw=0, zorder=0)
        self._draw_bands(ax, vertical=True)
        (self.spec_line,) = ax.plot([], [], color=INK, lw=1, zorder=3)
        self.thresh_line = ax.axhline(-45, color=BAD, ls="--", lw=1.2, zorder=4)
        self.thresh_text = ax.text(max_hz, -45, " volume threshold", color=INK_2,
                                   ha="right", va="bottom", fontsize=8, zorder=5)
        self.pitch_vline = ax.axvline(0, color=INK, lw=1, alpha=0, zorder=4)
        (self.pitch_dot,) = ax.plot([], [], "o", ms=9, mec=SURFACE, mew=2, zorder=6)

        # Pitch history
        ax = self.ax_hist
        ax.set_title("Pitch over time (colored = valid whistle, gray = rejected)",
                     loc="left", color=INK_2)
        ax.set_xlim(-config.PITCH_HISTORY_S, 0)
        ax.set_ylim(config.WHISTLE_MIN_HZ - 200, min(config.WHISTLE_MAX_HZ + 200, max_hz))
        ax.set_xlabel("seconds ago")
        ax.set_ylabel("Hz")
        self._draw_bands(ax, vertical=False)
        self.hist_rej = ax.scatter([], [], s=12, color=REJECTED, zorder=3)
        self.hist_ok = ax.scatter([], [], s=22, zorder=4, edgecolors=SURFACE, linewidths=1)

    def _draw_bands(self, ax, vertical):
        for name, lo, hi in config.BANDS:
            c = BAND_COLORS.get(name, MUTED)
            label = config.COMMAND_LABELS[name]
            if vertical:
                ax.axvspan(lo, hi, color=c, alpha=0.14, lw=0, zorder=1)
                ax.text((lo + hi) / 2, -4, label, ha="center", va="top", fontsize=8,
                        color=INK_2, zorder=5)
            else:
                ax.axhspan(lo, hi, color=c, alpha=0.14, lw=0, zorder=1)
                # Labels sit just outside the right edge so the dots never cover them.
                ax.text(0.1, (lo + hi) / 2, label, va="center", fontsize=8,
                        color=INK_2, clip_on=False)

    def _setup_text_panel(self):
        ax = self.ax_text
        ax.axis("off")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.text(0, 0.985, "DECISION", fontsize=8, color=MUTED, va="top")
        self.chip = Rectangle((0, 0.885), 0.035, 0.07, color=MUTED)
        ax.add_patch(self.chip)
        self.decision = ax.text(0.06, 0.92, "", fontsize=24, fontweight="bold", va="center")
        self.detail = ax.text(0.06, 0.86, "", fontsize=9, color=INK_2, va="center")
        self.progress_bg = Rectangle((0.06, 0.835), 0.9, 0.008, color=GRID)
        self.progress = Rectangle((0.06, 0.835), 0.0, 0.008, color=INK_2)
        ax.add_patch(self.progress_bg)
        ax.add_patch(self.progress)

        ax.text(0, 0.79, "GAME", fontsize=8, color=MUTED, va="top")
        self.game = ax.text(0, 0.765, "", fontsize=16, fontweight="bold", va="top")
        self.game_sub = ax.text(0, 0.715, "", fontsize=9, color=INK_2, va="top")

        ax.text(0, 0.665, "DRIVING", fontsize=8, color=MUTED, va="top")
        self.drive = ax.text(0, 0.64, "", fontsize=10, va="top", family="monospace")

        ax.text(0, 0.51, "NOISE FILTERS (this frame)", fontsize=8, color=MUTED, va="top")
        self.filters = ax.text(0, 0.485, "", fontsize=9, va="top", family="monospace")

        ax.text(0, 0.32, "LINKS", fontsize=8, color=MUTED, va="top")
        self.links = ax.text(0, 0.295, "", fontsize=9, va="top", family="monospace")

        ax.text(0, 0.205, "EVENTS", fontsize=8, color=MUTED, va="top")
        self.events = ax.text(0, 0.18, "", fontsize=8, va="top", color=INK_2,
                              family="monospace")
        self.help = ax.text(0, 0.0, "", fontsize=7.5, color=MUTED, va="bottom", wrap=True)

    # ----------------------------------------------------------------- update
    def _update(self, _frame):
        s = self.status.get()
        if self.audio:
            self._update_audio(s)
        self._update_text(s)
        return []

    def _update_audio(self, s):
        snap = self.audio.snapshot()
        det = snap.detection
        if det is None:
            return
        # Waveform with a gently adapting y-range
        self.wave_line.set_ydata(det.samples)
        peak = max(0.02, float(abs(det.samples).max()) * 1.2)
        lo, hi = self.ax_wave.get_ylim()
        if peak > hi or peak < hi * 0.4:
            self.ax_wave.set_ylim(-peak, peak)

        # Spectrum
        mask = det.freqs <= self.ax_spec.get_xlim()[1]
        self.spec_line.set_data(det.freqs[mask], det.spectrum_db[mask])
        self.thresh_line.set_ydata([det.threshold_db, det.threshold_db])
        self.thresh_text.set_y(det.threshold_db)
        if det.pitch_hz and det.loud:
            color = BAND_COLORS.get(det.band, REJECTED)
            self.pitch_vline.set_xdata([det.pitch_hz, det.pitch_hz])
            self.pitch_vline.set_alpha(0.5)
            self.pitch_dot.set_data([det.pitch_hz], [det.level_db])
            self.pitch_dot.set_color(color)
            self.pitch_dot.set_markeredgecolor(SURFACE)
        else:
            self.pitch_vline.set_alpha(0)
            self.pitch_dot.set_data([], [])

        # Pitch history
        now = time.monotonic()
        ok, ok_c, rej = [], [], []
        for t, hz, band in snap.pitch_history:
            if hz is None:
                continue
            if band:
                ok.append((t - now, hz))
                ok_c.append(BAND_COLORS.get(band, MUTED))
            else:
                rej.append((t - now, hz))
        self.hist_ok.set_offsets(ok or [(99, 0)])
        self.hist_ok.set_facecolors(ok_c or [MUTED])
        self.hist_rej.set_offsets(rej or [(99, 0)])

        # Filter readout for the text panel
        def mark(passed):
            return "pass" if passed else "FAIL"
        if snap.calibrating:
            s["filters_text"] = "Measuring ambient noise...\nstay quiet"
        else:
            s["filters_text"] = (
                f"pitch     {det.pitch_hz:7.0f} Hz\n"
                f"volume    {det.level_db:6.1f} dB  (min {det.threshold_db:.1f})  {mark(det.loud)}\n"
                f"peak/avg  {det.tonality_db:6.1f} dB  (min {config.TONALITY_MIN_DB:.0f})\n"
                f"peak share {det.peak_share * 100:5.0f} %   (min {config.PEAK_SHARE_MIN * 100:.0f})  "
                f"{mark(det.tonal)}\n"
                f"-> {config.COMMAND_LABELS.get(det.band, det.reason or '-')}")
        s["audio_line"] = f"mic: {snap.device_name} @ {snap.sample_rate} Hz"

    def _update_text(self, s):
        dec = s["decision"]
        self.decision.set_text(dec)
        chip = {"GOAL": GOOD}.get(dec)
        if chip is None:
            key = next((k for k, v in config.COMMAND_LABELS.items() if v == dec), None)
            chip = BAND_COLORS.get(key, MUTED)
        self.chip.set_color(chip)
        self.detail.set_text(s.get("detail", ""))
        self.progress.set_width(0.9 * s.get("progress", 0.0))

        game = s["game"]
        self.game.set_text(game)
        self.game.set_color(GAME_COLORS.get(game, INK))
        self.game_sub.set_text(f"role: {s['role']}    mode: {s['mode']}")

        level = s["speed_level"]
        bar = "#" * level + "." * (config.SPEED_LEVELS - level)
        steer = {-1: "LEFT", 0: "straight", 1: "RIGHT"}[s["steer"]]
        wl, wr = s["wheels"]
        refl = s["reflection"]
        refl_txt = "-" if refl is None else f"{refl:.0f} %"
        if s.get("proximity_threshold") is not None and refl is not None:
            refl_txt += f" (trip at {s['proximity_threshold']:.0f})"
        self.drive.set_text(
            f"speed  [{bar}] {level}/{config.SPEED_LEVELS}\n"
            f"steer  {steer}\n"
            f"profile {s['profile']}   wheels L{wl:+4d} R{wr:+4d}\n"
            f"light sensor  {refl_txt}")

        self.filters.set_text(s.get("filters_text", "(no audio on this laptop)")
                              if self.audio else s.get("audio_extra", "") or
                              "(no audio on this laptop)")
        links = f"robot: {s['robot']}\nmqtt:  {s['mqtt']}"
        if "audio_line" in s:
            links += "\n" + s["audio_line"]
        self.links.set_text(links)
        self.events.set_text("\n".join(e[-44:] for e in s["events"]))
        keys = ["q quit"] + [f"{k} {getattr(fn, 'help', '')}".strip()
                             for k, fn in self.keys.items()]
        self.help.set_text("keys:  " + "   ".join(keys))

    # ------------------------------------------------------------------- keys
    def _on_key(self, event):
        if event.key == "q":
            self._request_quit()
            return
        fn = self.keys.get(event.key)
        if fn:
            fn()

    def _request_quit(self):
        self.quit_requested = True
        plt.close(self.fig)

    def run(self):
        """Blocks until the window is closed or 'q' is pressed."""
        self._anim = FuncAnimation(self.fig, self._update, interval=1000 / config.DISPLAY_FPS,
                                   cache_frame_data=False)
        plt.show()


def keyhelp(text):
    """Decorator-ish helper: attach a short help label to a key callback."""
    def wrap(fn):
        fn.help = text
        return fn
    return wrap
