# Whistle-Controlled LEGO Robot: ME193 World Cup

A LEGO Education robot that you drive by whistling. A laptop listens through its microphone (or AirPods), finds the pitch of your whistle, turns it into a driving command, and sends it to the robot over Bluetooth. Over MQTT, two robots play a mini World Cup: a **ball** tries to reach the goal, and a **goalie** tries to catch it.

**GitHub repo:** `<link to github.com/clabar01/Lego here>` (code is in the `world_cup/` folder)

---

## 1. What the code does and how to run it

### How it works

```
 microphone ──► audio.py ──► policy.py ──► game.py ──► robot.py ──► LEGO Double Motor
 (pyaudio)     pitch +      whistle →     only drive   BLE thread     + Color Sensor
               noise        command       while                       (card 0999)
               filters                    PLAYING
                                             ▲  │
                                  mqtt_link  │  ▼  songs.py (victory / death)
                                  "start", "ceci_caught", "ceci_scored"
 display.py: live waveform, spectrum, pitch history, decision, game state
```

Each part runs on its own thread, so none of them waits on another:

| Thread | Job |
|---|---|
| Audio | Reads a 50 ms block of audio, finds the pitch, and updates the decision (20 times per second). |
| Robot | The only thread that talks Bluetooth. Sends motor speeds and reads the light sensor 20 times per second. |
| MQTT | paho-mqtt's network thread. Receives game messages and reconnects on its own if the connection drops. |
| Main | The live matplotlib window. It only *reads* shared state, so a slow redraw never delays the robot. |

### Code layout (`world_cup/`)

| File | What's in it |
|---|---|
| `main.py` | The one script you run. Command line flags pick the mode. |
| `config.py` | **Every tunable number**: frequency bands, thresholds, timing, speeds, MQTT settings. |
| `audio.py` | Microphone capture (pyaudio) and pitch detection with the noise filters (numpy FFT). |
| `policy.py` | Decision policy: debouncing, the goal whistle, the no-whistle behavior. |
| `robot.py` | Motor and light-sensor control on a background thread, plus a simulator. |
| `game.py` | Game state machine (waiting → playing → won/lost) and the ball/goalie rules. |
| `mqtt_link.py` | MQTT connection (paho-mqtt). |
| `songs.py` | Victory and death songs, synthesized with numpy and played with pyaudio. |
| `display.py` | Live display. |
| `remote.py` | Stretch goal: message format for two laptops controlling one robot. |
| `calibrate.py` | Calibration mode. |
| `tests/` | 56 automated tests (run `python -m pytest tests`). |

### Install

You need **Python 3.11 or newer**. The macOS system Python (3.9) won't work with `legoeducation`.

PyAudio needs the PortAudio C library. The easy way is [Homebrew](https://brew.sh):

```bash
brew install portaudio

cd Lego/world_cup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**No Homebrew?** Build PortAudio into the venv instead:

```bash
cd Lego/world_cup
python3 -m venv .venv && source .venv/bin/activate
curl -L -o pa.tgz https://github.com/PortAudio/portaudio/archive/refs/tags/v19.7.0.tar.gz
tar xzf pa.tgz && cd portaudio-19.7.0
./configure --prefix="$VIRTUAL_ENV" --disable-shared --enable-static --disable-mac-universal
make && make install
cp include/pa_mac_core.h "$VIRTUAL_ENV/include/"
cd .. && rm -rf portaudio-19.7.0 pa.tgz
ARCHFLAGS="-arch $(uname -m)" CFLAGS="-I$VIRTUAL_ENV/include" \
  LDFLAGS="-L$VIRTUAL_ENV/lib -framework CoreAudio -framework AudioToolbox -framework AudioUnit -framework CoreFoundation -framework CoreServices" \
  pip install pyaudio
pip install -r requirements.txt
```

The first time you run it, macOS asks for microphone permission for your terminal (or VS Code). Click **Allow**.

### Run it

```bash
source .venv/bin/activate
python main.py --list-devices            # 1. find your mic's number
python main.py --calibrate --device 1    # 2. whistle, set the bands in config.py
python main.py --device 1 --role ball    # 3. play!
```

The first 2 seconds of every run **measure the room's background noise, so stay quiet**, and point the light sensor at open space (nothing in front of it) while it takes its baseline.

### Command line flags

| Flag | What it does |
|---|---|
| `--list-devices` | List microphones and speakers with their numbers, then exit. |
| `--device N` | Microphone to use (default: system default). AirPods work too, see the note below. |
| `--role ball` / `--role goalie` | Which robot we are in the match (default `ball`). |
| `--calibrate` | Calibration mode: whistle and see the detected pitch. The robot isn't used. |
| `--sim` | Simulated robot, no Bluetooth. Test anywhere; press `o` for a fake obstacle. |
| `--no-mqtt` | Offline. Press `s` to start the game yourself. |
| `--profile slow\|medium\|fast` | Starting speed profile (default `medium`). |
| `--broker HOST` `--port N` | MQTT broker (default `broker.hivemq.com:1883`). |
| `--card 0999` | Connection Card serial for the motor and sensor. |
| `--no-sensor` | Don't connect the light sensor. |
| `--noise-db X` | Fixed volume threshold in dBFS. Skips the ambient-noise measurement. |
| `--noise-margin X` | How many dB above the room noise a whistle must be (default 12). |
| `--output-device N` | Speaker for the songs. |
| `--no-hub-songs` | Play the victory/death songs on the laptop only. By default they also beep on the robot's Double Motor at the same time. |
| `--no-display` | Console only, no window. |
| `--mode single\|robot\|drive\|aux\|defense` | Stretch goal, see section 6. Default `single`. |

**AirPods:** in microphone mode AirPods switch to a low "call quality" sample rate (16 or 24 kHz instead of 48 kHz). The code asks the device which rates it supports and uses one of those, so nothing special is needed. The whistle range (up to 4 kHz) is well below the 8 kHz limit even at 16 kHz.

### Keys in the display window

| Key | Action |
|---|---|
| `q` | Quit (motors stop, Bluetooth disconnects). |
| `s` | Start the game locally (for testing without the professor's "start"). |
| `r` | Reset the game back to "waiting for start". |
| `n` | Re-measure the room noise. |
| `b` | Re-measure the light sensor baseline. |
| `1` `2` `3` | Speed profile slow / medium / fast. |
| `o` | (`--sim` only) toggle a fake obstacle in front of the sensor. |

### Calibration

1. Run `python main.py --calibrate --device N`.
2. Whistle your **lowest** comfortable note for about a second, a few times. Then your **highest**, then two notes in between.
3. Each whistle prints a summary like this:
   ```
   whistle 0.85 s  median 1712 Hz (1690-1741)  -31 dB  peak/avg 24 dB  share 88 %  -> TURN RIGHT
   ```
4. Edit `BANDS` in `config.py` so each of your four notes sits in the middle of its own band, with a small gap between bands.
5. If real whistles are being rejected, compare `peak/avg` and `share` with `TONALITY_MIN_DB` and `PEAK_SHARE_MIN` and lower those slightly.

### Testing on the hardware, one stage at a time

| Stage | Command | What to check |
|---|---|---|
| 1. Audio + display | `python main.py --calibrate` | The pitch follows your whistle; talking and clapping stay gray (rejected). |
| 2. Robot | `python main.py --no-mqtt`, then press `s` | Whistles drive the car. Both wheels go forward on SPEED UP (if one wheel runs backward, swap `LEFT_FLIP`/`RIGHT_FLIP` in `config.py`). Put a hand in front of the sensor: the ball stops and the death song plays. |
| 3. MQTT game | `python main.py --role ball` | "ceci_ready" is sent, nothing moves until "start", and the game ends correctly for both roles. |
| 4. Two laptops | see section 6 | The robot follows laptop 1, laptop 2 changes the profile, light and songs, and the car stops if laptop 1 quits. |

---

## 2. Decision policy: how pitch and volume become commands

Every 50 ms the code finds the **strongest pitch between 500 Hz and 4 kHz**. All command bands sit inside one octave, C6 to C7 (1047–2093 Hz). If that pitch passes the noise filters (section 4), its frequency band decides the command:

| Whistle | Frequency band (default, tune in `config.py`) | Command |
|---|---|---|
| Lowest (C6 – D6) | 1047 – 1180 Hz | **STOP**: speed goes to 0, wheels straight |
| Low middle (D#6 – E6) | 1225 – 1355 Hz | **BACKWARD**: back up slowly and straight while you hold it (stops 0.5 s after) |
| Middle (F6 – G6) | 1405 – 1555 Hz | **TURN LEFT** for as long as you hold it |
| High middle (G#6 – A6) | 1615 – 1785 Hz | **TURN RIGHT** for as long as you hold it |
| Highest (A#6 – C7) | 1860 – 2093 Hz | **SPEED UP**: one speed level faster (holding it adds another level every 0.8 s) |
| Two short high "tweets" | high band, each shorter than 0.4 s | **GOAL** (ball only): we scored |

The small gaps between bands are **guard bands**. A pitch right on a boundary counts as "no whistle" instead of flickering between two commands.

**Volume** doesn't pick the command; it's a gate. A whistle must be louder than the room's background noise plus a margin (see section 4).

**A command needs a held whistle.** A pitch must stay in the same band for **0.45 s** (9 frames in a row) before it becomes a command.

**GOAL whistle ("tweet-tweet"):** two high whistles, each 0.08–0.4 s long, with 0.08–0.6 s of silence between them, all within 1.5 s. Each tweet is shorter than the 0.45 s debounce, so a tweet can never also count as SPEED UP. All of these times are in `config.py`.

**Speed levels (0–5) and profiles.** Values are motor power in %:

| Profile | Level 1 | 2 | 3 | 4 | 5 | Turning (inner wheel) | Turn while stopped |
|---|---|---|---|---|---|---|---|
| slow | 20 | 26 | 32 | 39 | 45 | 35 % of outer wheel (gentle arc) | spin in place at 25 |
| medium | 20 | 32 | 45 | 58 | 70 | 15 % of outer wheel | spin in place at 35 |
| fast | 20 | 40 | 60 | 80 | 100 | inner wheel runs backward (tight turn) | spin in place at 45 |

A turn while driving is an arc, with the inner wheel slower. A turn while stopped spins the robot in place so you can aim it.

---

## 3. What happens when no whistle is detected

"No whistle" means the frame failed a noise filter, or no band has been held long enough to count yet. The robot does **not** stop instantly: you have to breathe, and a whistle wavers. It also never keeps driving forever with nobody in control:

| Time since the last confirmed whistle | What the robot does | Why |
|---|---|---|
| 0 – 0.5 s | Keeps turning if it was turning. | Bridges a breath or a wobble in a turn whistle. |
| After 0.5 s | Straightens out and keeps **cruising** at the current speed. | You don't have to whistle constantly to keep moving. |
| After 4 s of silence | **Slows down one level per second until it stops.** | Safety: if the whistler stops, walks away, or the mic dies, the robot stops on its own. |

A single frame that sounds like a whistle (a squeak, a clap) never changes anything, because it can't hold for 0.45 s.

Other safety stops:

- **Before "start" and after the game ends**, whistles still appear on screen but are ignored, and the motors are held stopped until a reset.
- **Two-laptop mode:** if the robot hears nothing from the drive laptop for 1 s, it stops.
- **Quitting** (`q`, closing the window, or Ctrl-C) always stops the motors before disconnecting.

---

## 4. How we masked out unwanted noise

A classroom has talking, other teams' whistles, robot motors, and laptop fans. We use five filters, and a sound has to pass **all** of them before it can drive the robot. You can watch each filter pass or fail live in the "NOISE FILTERS" part of the display.

| # | Filter | What it does | What it rejects |
|---|---|---|---|
| 1 | **Bandpass** (500 Hz – 4 kHz) | Only looks for a pitch in the range where human whistles live. Everything else in the spectrum is ignored (shown hatched gray on the display). | Motor hum, footsteps, table bumps, the low part of voices (below 500 Hz); hiss and clicks (above 4 kHz). |
| 2 | **Volume threshold** with auto-calibration | At startup it listens to the room for 2 s and sets the threshold to the typical background level **+ 12 dB**. The whistle's peak must be louder than that. Press `n` to re-measure if the room gets louder. | Quiet background sounds and distant whistles, including other teams far away. Adapts to each room automatically. |
| 3a | **Tonality: peak-to-average ratio** (≥ 14 dB) | A whistle is one narrow, strong peak. We compare the peak's power to the average power across the band. Whistles score about 20–30 dB. | Broadband noise that spreads energy everywhere: crowd noise, wind, fans, motor whine, "shhh" sounds. |
| 3b | **Tonality: peak energy share** (≥ 50 %) | The peak (±60 Hz) must hold at least half of the band's total energy. A whistle holds about 80–95 %. | Voices and other *harmonic* sounds. A voice has many evenly spaced peaks, so each one individually can look sharp, but none holds most of the energy. Our tests showed a synthetic voice passing check 3a, so we added 3b. |
| 4 | **Debouncing** (0.45 s hold) | The pitch must stay in the **same band for 9 frames in a row**. Switching band restarts the count, and a single dropped frame inside a whistle is tolerated. The guard bands between commands stop flickering at boundaries. | One-off squeaks, a door hinge, a chirp from someone else, a whistle sliding through several bands. |
| 5 | **Mute while our own song plays** | While the victory or death song is playing on the laptop speakers, the microphone input is ignored. | Our own song being "heard" as whistles. |

**Two laptops in one room (stretch goal):** each laptop's mic also hears the other person's whistle. We saw this in testing: a sound in the room reached both laptops at the same time. Fixes:

- Each whistler uses **AirPods**, whose mic is right at your mouth, so your own whistle is far louder than your partner's.
- Raise the margin on each laptop, e.g. `--noise-margin 20`, until it only reacts to its own whistler.

All thresholds live in `config.py` under **NOISE MASKING**.

---

## 5. MQTT message protocol (agreed with our opponent)

**Broker (development):** `broker.hivemq.com`, port `1883`. To switch to the course broker, change `BROKER_HOST` / `BROKER_PORT` in `config.py` (or use `--broker` / `--port`).

**Topic:** `ME193/Rogers`. The whole class shares it, so every message for our match starts with our match name, **`ceci`** (`MATCH_PREFIX` in `config.py`). Messages that aren't ours are ignored.

| Message (exact string) | Sent by | When | What the receivers do |
|---|---|---|---|
| `start` | Professor / referee | Match begins | Both robots: whistle control turns on. Also accepted: `ceci_start`. |
| `ceci_ready` | Each robot | On connecting (and after a reset) while waiting for `start` | Informational. |
| `ceci_caught` | **Ball** | Its light sensor sees the goalie right in front of it | Ball: already stopped its motors *before* sending, plays the **death** song, game **LOST**. Goalie: plays the **victory** song, game **WON**. |
| `ceci_scored` | **Ball** | We whistle the GOAL command ("tweet-tweet") | Ball: **victory** song, game **WON**. Goalie: **death** song, game **LOST**. |
| `ceci_reset` | Anyone (optional) | To play again | Both robots go back to "waiting for start". |

Payloads are plain text strings, published with QoS 1.

**Game states:** `WAITING FOR START` → (`start`) → `PLAYING` → `WON` or `LOST`. The robot only drives while `PLAYING`. After the game ends, the motors stop and driving commands are ignored until a reset. The hub light shows the state: yellow breathing = waiting, blue = playing, green blinking = won, red blinking = lost.

**How the ball "sees" the goalie:** the forward-facing color sensor measures reflected light (0–100 %). With nothing in front, little light comes back. When the goalie is close, much more of the sensor's own light bounces back. At startup we average 20 readings as a baseline, and the sensor trips when reflection ≥ baseline + 15 (and ≥ 20) for 2 readings in a row. Tune with `PROXIMITY_*` in `config.py`, and press `b` to re-take the baseline.

---

## 6. Stretch goal: two laptops, one robot

Audio processing can run on either laptop and send commands over MQTT. One robot-side script executes them.

```
 Laptop 1 (driver)  ── ME193/Rogers/ceci/drive ──┐
   python main.py --mode drive --device N        │
                                                 ▼
 Laptop 2 (DJ)      ── ME193/Rogers/ceci/aux ──► Robot laptop (Bluetooth to the robot)
   python main.py --mode aux --device N          python main.py --mode robot --role ball
                                                 │ also handles the game topic ME193/Rogers
```

The robot laptop can be one of the two whistling laptops (run two terminals) or a third computer.

**Laptop 1, driving:** the same whistle commands as section 2. It sends its full drive state 10 times per second, so a lost message is simply replaced by the next one, and the stream doubles as a heartbeat.

**Laptop 2, songs, light, and speed profile:**

| Whistle | Action |
|---|---|
| High | Faster profile (slow → medium → fast) |
| Low | Slower profile |
| Lower middle | Next hub light color |
| Upper middle | Play the "cheer" song on the robot laptop |

Keys `1` `2` `3`, `l` and `c` do the same things.

**Messages** (JSON, on our own sub-topics, so they never clutter the class topic):

| Topic | Message | Notes |
|---|---|---|
| `.../ceci/drive` | `{"type":"drive","speed_level":3,"steer":-1,"decision":"TURN LEFT"}` | 10 per second, QoS 0. The robot stops if none arrive for 1 s. |
| `.../ceci/drive` | `{"type":"goal"}` | The goal whistle (the robot checks that it is the ball and the game is playing). |
| `.../ceci/aux` | `{"type":"profile","value":"fast"}` | Retained, so a restarted robot remembers the last profile. |
| `.../ceci/aux` | `{"type":"light","color":"GREEN"}` | Hub light. |
| `.../ceci/aux` | `{"type":"song","name":"cheer"}` | Victory and death are reserved for the game and are never interrupted. |

The robot laptop still runs the full game logic (start gating, light sensor, `ceci_caught` / `ceci_scored`, songs).

### Driver + defender: one drives, one runs the defense arm

A LEGO Single Motor on top of the robot swings a defense arm. One person whistles to drive (Double Motor). The other person whistles to swing the arm.

```
 Laptop 1 (driver)    python main.py --mode robot --role ball      (terminal 1, Bluetooth to the robot)
                      python main.py --mode drive --device N       (terminal 2)
 Laptop 2 (defender)  python main.py --mode defense --device N
```

**Defense whistles** (`DEFENSE_BANDS` in `config.py`, tune with `python main.py --mode defense --calibrate`):

| Whistle | Hz | Arm |
|---|---|---|
| Low (C6 – E6) | 1047 – 1330 | Swings all the way LEFT |
| Middle (F6 – G#6) | 1400 – 1660 | Back to ZERO (straight up) |
| High (A6 – C7) | 1740 – 2093 | Swings all the way RIGHT |

Keys `j` / `u` / `k` do the same. Left and right only work while the game is PLAYING, like driving. Zero works any time.

**The arm goes back to zero by itself** when:
- the game ends (win or lose);
- the defense laptop quits, or drops off the network. Its MQTT "last will" makes the broker send "arm zero" for it;
- the robot laptop loses its MQTT connection;
- the robot laptop program shuts down (it homes the arm before disconnecting).

**Keeping the arm out of the bottom third** (with `DEFENSE_FORBIDDEN_DEG = 120`; a negative value lets the arm pass the bottom). The light sensor sits under the motor, so the arm stays out of the bottom 120° of its circle. It swings between −110° and +110° from straight up, which leaves the forbidden 120° plus a 10° safety margin on each side. It always swings over the top to reach the other side. At startup the robot reads the motor's absolute position, sets "degrees from straight up" as the motor's relative position (a counter that doesn't wrap at 360°), and then only ever moves between −110 and +110 on that counter. The arm holds its position when hit.

**Setup, once:** on the robot laptop, turn the arm straight up by hand and press `z`. The log prints `DEFENSE_UP_POSITION = N`. Put that number in `config.py` so the arm knows where "up" is on every run. Then test with `j` (left), `k` (right) and `u` (up). If `j` swings right, set `DEFENSE_LEFT_SIGN = 1`. Change `DEFENSE_FORBIDDEN_DEG`, `DEFENSE_MARGIN_DEG` and `DEFENSE_SPEED` to adjust the swing.

| Topic | Message | Notes |
|---|---|---|
| `.../<prefix>/defense` | `{"type":"defense","side":"left"}` | QoS 1, one per confirmed whistle. `side` is `left`, `right` or `up` (zero). `up` is also the defense laptop's last will. |

Use `--no-defense` on the robot laptop if the Single Motor isn't attached.

## 7. Violin driving (`violin.py`)

`violin.py` is the same program as `main.py` (same flags, display, robot, MQTT and game), but it listens for violin notes instead of whistles. `main.py` still does whistling, unchanged.

| Note | Command |
|---|---|
| D4 (open D) | STOP |
| E4 | BACKWARD. Hold it and it backs up faster every 0.8 s |
| F#4 | TURN LEFT |
| G4 | TURN RIGHT |
| A4 (open A) | SPEED UP. Hold it and it speeds up every 0.8 s |
| D5 | WE WON. Hold it for 1 s; ball only |

The car only moves **while you play**: 0.3 s after the last command note it stops. Remap the notes in `VIOLIN_NOTES` at the top of `violin.py`.

**Violin driver + whistle shield (two laptops):**

```
 Laptop 1 (violin)   terminal 1:  python main.py --mode robot --role ball     (Bluetooth to the robot)
                     terminal 2:  python violin.py --mode drive --device N
 Laptop 2 (shield)                python main.py --mode defense --device N
```

The shield laptop uses the three defense whistles from section 6 (low = left, middle = zero, high = right).
