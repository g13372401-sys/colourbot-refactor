# Colour-bot harness (anti-cheat benchmarking build)

Refactor of the intern's `tester.py` / `replay.py` / `redclick.py` trio into one
configurable, restartable tool. **The in-game behaviour, the click/key order and
every timing distribution are unchanged** - only the structure, the coordinate
system (game window instead of whole desktop), the valuable-drop pick-up and the
Discord layer are new.

---

## 1. TL;DR / quick start

**a) Credentials** - `config.py`, section 11 (`DISCORD`):

```python
DISCORD = {
    "token": "BOT_TOKEN_PLACEHOLDER",   # <-- your bot token
    "user_id": 000000000000000000,      # <-- your Discord user id (for the DM ping)
    "channel_id": 0,                    # optional: id of the channel to post into
    ...
}
```

Prefer not to edit a tracked file? Use environment variables instead - they win
over `config.py`:

```bat
set COLOURBOT_DISCORD_TOKEN=your-token-here
set COLOURBOT_DISCORD_USER_ID=123456789012345678
```

**b) Install** (Windows, PowerShell/CMD, from the folder with `main.py`):

```bat
python -m pip install -r requirements.txt
winget install -e --id UB-Mannheim.TesseractOCR    :: OCR binary, needed for the drop/chat features
```

**c) Run** (game client open, unobstructed, on the 1080p monitor):

```bat
python main.py --route route1
```

**d) Other things you will want:**

```bat
python main.py --list-routes                          :: what routes exist
python main.py --calibrate                            :: check window/canvas detection
python main.py --route route1 --start common          :: skip the route replay (old redclick.py)
python main.py --route route1 --debug-drop            :: TEST THE VALUABLE-DROP PICK-UP
python main.py --route route2 --debug-drop --skip-inventory-clear
python main.py --record routes/route1_leg1.json       :: record a new leg (ESC stops)
```

**Discord commands** (prefix `!`): `!kill` `!restart` `!screenshot` `!count`
`!plus [n]` `!minus [n]` `!reset` `!status` `!runtime` `!routes`
`!run <shell command>` `!help`.

**Runtime stopwatch:** total accumulated runtime lives in `runtime_total.json`
next to the code. It is **not** reset by a crash, `!restart`, ESC or a reboot -
only `--reset-runtime` clears it. `!runtime` prints it.

Press **ESC** anywhere to kill the bot.

---

## 2. What the bot does (flow overview)

```
main.py --route route1
  |
  |-- persistent runtime stopwatch starts (runtime_total.json)
  |-- Discord bot starts  <-- NEW: up before anything else, stays up forever
  |-- ESC panic key listener
  |
  +-- supervisor loop  (restarts everything below, same configuration, on any error)
        |
        |-- PHASE 1  route replay          (was replay.py)
        |     for each leg of the route:
        |        leg_preamble  : '2' (inventory tab) -> click coin pouch (cyan)
        |                        -> click dodgy necklace (white)
        |        play the recorded .json (mouse/keyboard timeline, pynput)
        |        leg_outro     : 1 s
        |        between legs  : (route1 only) screenshot -> click the black
        |                        teleport tile -> wait
        |     after the last leg: 'j', then 'insert' (screenshot)
        |
        +-- PHASE 2  common case            (was redclick.py)
              detect the anchors once (red target, blue inventory box, yellow
              prayer orb, cyan pouch, orange brews)
              loop:
                click the red target, then three workers run in parallel
                  * idle-click loop      - keeps clicking where the cursor is
                  * target-movement watch- red blob moved > 50 px? -> chase it
                  * event watch          - relayed game chat -> flags
                whichever flag fired is handled:
                  smited        -> drink a brew, prayer back on
                  loot full     -> empty pouch, shift-drop a junk item
                  dodgy gone    -> wear a new necklace
                  veil faded    -> '4', cast Shadow Veil, '2'
                  valuable drop -> NEW pick-up routine, then restart the flow
              a chat watchdog runs in the background the whole time (NEW)
```

---

## 3. Files

| File | What is in it |
|---|---|
| `main.py` | entry point, CLI, supervisor/restart loop, both automation phases, valuable-drop routine, drop debug mode, route recorder |
| `core.py` | infrastructure: logging, runtime stopwatch, `Clock` (all sleeping), shared state/flags, game-window geometry + coordinate translation, human-like mouse/keyboard, recorded-timeline playback |
| `vision.py` | canvas capture, colour blob finders, chat watchdog, ground-item/valuable-drop finder (local OCR) |
| `discord_bot.py` | Discord service: `!` commands + relayed-game-message router |
| `config.py` | **all** settings: colours, delays, sequences, routes, Discord, chat, drop, geometry |
| `routes/route1_leg1.json` | route1, first leg (was `route1_2.json`) |
| `routes/route1_leg2.json` | route1, second leg (was `route1.json`) |
| `routes/route2_leg1.json` | route2, single leg (was `route2.json`) |
| `requirements.txt` | python dependencies |
| `runtime_total.json` | created at runtime: the persistent stopwatch |
| `colourbot.log` | created at runtime: rolling console log copy |

Old entry points map like this:

| Old | New |
|---|---|
| `python tester.py` | `python main.py --route route1` |
| `python replay.py --playback route1_2.json` | phase 1 of `main.py` (`Automation.run_route_phase`) |
| `python replay.py --record file.json` | `python main.py --record file.json` |
| `python redclick.py` | `python main.py --route <route> --start common` |

---

## 4. Setting up from a fresh workspace

### Windows 10/11 (the machine that actually runs the bot)

1. **Python 3.10+** - <https://www.python.org/downloads/windows/>, tick
   *"Add python.exe to PATH"*.
2. **Dependencies**

   ```bat
   cd <folder with main.py>
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

3. **Tesseract OCR** (needed for the valuable-drop OCR and the chat watchdog):

   ```bat
   winget install -e --id UB-Mannheim.TesseractOCR
   ```

   Installs to `C:\Program Files\Tesseract-OCR\tesseract.exe`. If that folder is
   not on `PATH`, point `config.py` at the binary:

   ```python
   GENERAL = {
       ...
       "tesseract_cmd": r"C:\Program Files\Tesseract-OCR\tesseract.exe",
   }
   ```

   Check with `python -c "import pytesseract;print(pytesseract.get_tesseract_version())"`.
4. **Run the terminal as Administrator.** The `keyboard`/`mouse` libraries need
   it to inject input into a game client, and the ESC panic key needs it to see
   global key presses.
5. **Game client**
   * same 1080p monitor as the terminal,
   * window size exactly as delivered (947 x 650 canvas, i.e. the default
     RuneLite window from the reference screenshots) - the *position* can be
     anywhere, the bot finds it,
   * client always on top / not covered by other windows,
   * the colour highlight plugins (red target, blue inventory anchor, yellow
     prayer orb, cyan pouch, orange brews, white necklaces, black teleport tile)
     and the Discord chat relay configured as before.
6. **Sanity check the geometry** before a long run:

   ```bat
   python main.py --calibrate --save-debug-image check.png
   ```

   `check.png` should contain only the game area with green boxes on the
   detected regions. `recorded offset` tells you how far the client moved from
   the layout the routes were recorded at - any value is fine, it is applied
   automatically.

### Ubuntu (development only)

The window lookup uses the Win32 API, so Linux is for reading/testing the code,
not for running the bot against the game.

```bash
sudo apt update
sudo apt install -y python3-pip python3-tk tesseract-ocr wmctrl scrot
python3 -m pip install -r requirements.txt
```

* `wmctrl` gives a best-effort window lookup (`--game-region X,Y,W,H` always
  works as an override).
* `keyboard`/`mouse` need root on X11 (`sudo -E python3 main.py ...`).
* Everything that does not touch the OS input layer (geometry, colour finders,
  OCR, drop finder, Discord layer) runs fine as a normal user.

---

## 5. Usage

### 5.1 Normal run

```bat
python main.py --route route1
```

Replays the route, then loops forever. Restarts itself (same configuration) when

* the valuable drop was collected,
* the relay posts `5 hitpoints!`,
* the brews ran out (`no orange!`),
* `!restart` is used,
* **or any unhandled error happens** (5 s backoff, configurable in
  `config.GENERAL["restart_backoff_seconds"]`).

### 5.2 Starting at the common case (old `redclick.py`)

```bat
python main.py --route route1 --start common
```

Use it when the character is already at the spot and you do not want the route
replayed. `--route` is **required** here (the loop still needs to know what to
replay when it restarts itself). The skip only applies to the first run; every
restart after that replays the route from the top.

### 5.3 Valuable-drop debug mode (NEW)

Valuable drops are rare, so you can test the pick-up on demand:

```bat
:: loot already lying on the floor, inventory full (production-like)
python main.py --route route1 --debug-drop

:: inventory not full - do not shift-drop the two junk items first
python main.py --route route1 --debug-drop --skip-inventory-clear
```

What it does (no Discord broadcast needed):

1. finds the game window, detects the inventory anchor,
2. starts the chat watchdog and closes the chat if it is open,
3. presses `insert` (screenshot), drops two junk items to make space
   (unless `--skip-inventory-clear`),
4. scans the game area for the ground-item label of
   `config.ROUTES[<route>].drop_item_name`, confirms it with OCR,
5. clicks the pile, waits 3 s for the player **and** the trailing camera to
   settle, re-scans, clicks again, until the label is gone,
6. presses `insert` again and reports how many take-clicks it issued.

Expected console tail:

```
ground label at (186,463) 271x16: 'Enhanced cryrkal teleport seed...' (match 0.85)
drop 'Enhanced crystal teleport seed' confirmed by OCR (0.85) at (321, 476)
taking the drop at canvas (321,476) [click 1/3]
taking the drop at canvas (318,474) [click 2/3]
no ground label left - 2 item(s) taken
=== debug run finished: 2 take-click(s) issued ===
```

If it says *"could not find the ... label on the floor"*: check that the label is
actually on screen, that `DROP["label_color"]` matches your Ground Items plugin
highlight colour, and run with `--log-level DEBUG --save-debug-image dbg.png`.

### 5.4 Recording a new route leg

```bat
python main.py --record routes/route3_leg1.json
```

Move/click/type, press **ESC** to stop. Coordinates are stored in the reference
frame, so the recording replays correctly even if the client window moves later.
Then add the route to `config.ROUTES` (see 6.3).

### 5.5 Other switches

| Switch | Meaning |
|---|---|
| `--no-discord` | run without the control channel |
| `--game-region X,Y,W,H` | skip the window search, use this canvas rectangle |
| `--reset-runtime` | zero the persistent stopwatch before starting |
| `--save-debug-image FILE` | write an annotated capture of the detected regions |
| `--list-routes` / `--calibrate` | print info and exit |
| `--log-level DEBUG` | very chatty (every wait, every OCR result) |
| `--log-file path` | log copy location (`config.GENERAL["log_file"]` by default) |

---

## 6. Configuration (`config.py`)

Everything lives in one commented file. The sections you will actually touch:

### 6.1 Timings - `DELAYS`

One entry per wait in the program, named `<phase>.<what it waits for>`:

```python
"veil.before_key":        Uniform(0.7, 1.2),   # pause before pressing '4'
"veil.spell_key_hold":    Uniform(0.05, 0.1),  # how long '4' is held
"drop.pickup_settle":     Fixed(3.0),          # player + camera settle time
```

`Fixed`, `Uniform(lo, hi)` and `Gauss(mean, stddev)` are the three delay types.
`core.Clock` samples them (clamped at 0, like the original `max(0, ...)` calls)
and is the only thing in the program allowed to sleep. Changing a timing is a
one-line edit; nothing else in the code hard-codes a number.

### 6.2 Key / click order - `SEQUENCES`

The fixed parts of the replay phase are data, not code:

```python
"leg_preamble": [
    Wait("leg.initial_pause"),
    TapKey("2", hold="leg.tab_key_hold", after="leg.after_tab_key", note="inventory tab"),
    ClickLargestSolid("cyan", "coin pouch"),
    Wait("leg.after_pouch_click"),
    ClickLargestSolid("white", "dodgy necklace"),
    Wait("leg.after_necklace_click"),
],
```

Reorder the lines to reorder the actions. Step types: `Wait`, `TapKey`,
`ClickLargestSolid`, `Log` (teach `main.StepRunner.run_step` about new ones).

### 6.3 Routes - `ROUTES`

```python
"route2": RouteProfile(
    description="Single-leg standalone route",
    legs=["routes/route2_leg1.json"],   # replayed in this order
    preamble="leg_preamble",
    between_legs=None,                  # single leg -> nothing in between
    after_last_leg="arrive_at_spot",
    drop_keyword="teleport",            # substring of the Discord broadcast
    drop_item_name="Enhanced crystal teleport seed",   # what the OCR looks for
    expected_drops=2,
),
```

`route2` currently inherits route1's drop keyword/name - **set them to route2's
real valuable drop** (there is a `TODO(route2)` comment in place).

Legs are named `<route>_leg<N>.json` and replayed in list order, which is the
fix for the old `route1_2.json`-runs-before-`route1.json` confusion.

### 6.4 Window / coordinates - `GAME_WINDOW`

```python
"title_contains": ["RuneLite"],            # window search
"reference_canvas_origin": (969, 227),     # where the canvas was when the routes were recorded
"canvas_size": (947, 650),                 # client area size (window size is assumed fixed)
"window_insets": (4, 26, 4, 4),            # RuneLite draws its own title bar inside the window
```

### 6.5 Other sections

| Section | Contains |
|---|---|
| `GENERAL` | runtime-stopwatch file, log file/level, restart backoff, panic key |
| `COLORS` / `COLOR_TOLERANCE` | the highlight colours (exact RGB match by default) |
| `DERIVED_REGIONS` | the junk slots / Shadow Veil icon offsets from the blue box |
| `VISION` | movement threshold, scan cadence, event poll interval |
| `MOUSE` | bezier movement profile, jitter, multi-click gap |
| `COMMON` | 5 % chance of the idle-click "human hitch" |
| `DISCORD` | token, prefix, authorised users, `!run` toggle, relayed message wording |
| `CHAT` | chat watchdog: strip to OCR, toggle key, `All` button rectangle |
| `DROP` | label colour/tolerance, click offset, OCR thresholds, attempt budget |

---

## 7. Discord control channel

The bot starts **before** the route replay and lives for the whole process, so
`!kill` / `!restart` work even if something goes wrong on the way to the spot -
which was the main complaint about the old build.

| Command | Aliases | Effect |
|---|---|---|
| `!kill` | `!stop`, `!quit` | flush the stopwatch, report total runtime, exit now |
| `!restart` | `!reboot` | restart the flow from the top, same configuration (runtime keeps counting) |
| `!screenshot` | `!screen`, `!shot` | press `insert` (RuneLite screenshot hotkey) |
| `!count` | | show `brew_counter` |
| `!plus [n]` | | register added brews (`brew_counter -= n`, legacy arithmetic) |
| `!minus [n]` | | register removed brews (`brew_counter += n`) |
| `!reset` | | `brew_counter = 0` |
| `!status` | | phase, route, all flags, runtime, how the run was started |
| `!runtime` | `!uptime` | total accumulated runtime + this process' share |
| `!routes` | | list the configured routes |
| `!run <cmd>` | | run a shell command on the bot machine, reply with its output |
| `!help` | | discord.py's generated help |

Restrict who may use them with `DISCORD["authorized_user_ids"] = [123..., 456...]`
(empty list = anyone in the channel, which is what the old build effectively
did). `!run` can be switched off entirely with `DISCORD["allow_run_command"]`.

Relayed **game** messages are still matched as plain text (they come from the
RuneLite relay, not from a human) and are configured in `DISCORD["messages"]`:
`There is no space for your loot!`, `smited!`,
`Your dodgy necklace has crumbled to dust.`, `Shadow Veil has faded!`,
`5 hitpoints!`, anything containing `died`, anything containing the route's
`drop_keyword`.

Adding a command: one decorated function in `discord_bot.DiscordService._register_commands`.

---

## 8. New features in detail

### 8.1 Vision and input limited to the game window

*Problem:* the old code grabbed the whole desktop, so "largest black region"
could be the terminal, and the bot occasionally clicked Windows UI (mostly
during the replay phase).

*Now:*

* the window is located through the Win32 API (`DwmGetWindowAttribute` /
  `GetWindowRect`, title match on `RuneLite`), the canvas is derived with the
  configured insets and then auto-refined by looking for RuneLite's flat window
  chrome, so an invisible resize border cannot shift the frame;
* every capture is exactly the canvas, and all vision coordinates are canvas
  relative;
* `InputController` takes canvas coordinates and converts them to screen
  coordinates itself, clamping the final landing point into the canvas
  (`GAME_WINDOW["clamp_clicks_to_canvas"]`) - it is now impossible for a
  vision-driven click to land on Windows UI;
* the recorded routes keep their original absolute coordinates: at startup the
  bot computes `current canvas origin - reference canvas origin` and shifts every
  replayed event by that offset (`recorded offset` in `--calibrate`). The .json
  files were not touched.
* the recorded moves at the very start of a leg (cursor parked over the
  terminal) are replayed as recorded, because they are pure moves with no click.
  Flip `GAME_WINDOW["clamp_replayed_moves_to_canvas"]` if you want the pointer
  glued to the client at all times.

Everything is derived from two config values (`reference_canvas_origin`,
`canvas_size`), so if the team ever changes the client size, that is the only
edit needed.

### 8.2 Valuable-drop pick-up, v2

*Old:* drop two junk items, then click the magenta box drawn on the player's own
tile twice. Fails whenever the player wandered off the tile the loot landed on
(the exact situation in `valuable_drop.png`).

*New (`Automation.collect_valuable_drop` + `vision.DropFinder`):*

1. chat watchdog makes sure the chat box is closed (it overlaps the game area
   and breaks both colour detection and OCR),
2. same `insert` screenshot + shift-drop of two junk items as before (same
   delays, same order),
3. the ground-item label RuneLite paints over the pile is found by its highlight
   colour (`DROP["label_color"]`, default the `(255,102,178)` pink from the
   reference screenshot), glyphs are glued into lines and each line is OCR'd
   **locally** with Tesseract,
4. the line whose text fuzzy-matches the route's `drop_item_name` wins - the
   stack size `(2)` and the `(6.67M gp)` value are ignored on purpose, because
   they change with every roll,
5. the click point is the label's horizontal centre plus
   `DROP["click_offset_y"]` (the sprite sits a few px below the label - measured
   from `valuable_drop.png`),
6. after each click the bot waits `drop.pickup_settle` (3 s) for the player to
   walk there and the trailing camera to catch up, re-scans, and clicks again -
   so a stack of two is collected with two clicks at two different screen
   positions, and a single item with one,
7. it stops when the label is gone (or after `expected_drops + 1` clicks) and
   logs an error if a label is still visible afterwards,
8. final `insert` screenshot, then the flow restarts exactly like before.

The magenta player-tile box is no longer used for the pick-up (it is still
detected, and is reported by `--save-debug-image`, so nothing else broke).

### 8.3 Chat watchdog

Pressing `insert` (screenshot) pops the game chat open a few seconds later, and
the chat's translucent gradient/text was the most common cause of
"No solid red area found." and of missed drops.

`vision.ChatWatcher` OCRs the `Press Enter to Chat` strip above the
`All / Game / Private` buttons:

* open? press `` ` `` (once, verify, at most twice - a third press re-opens it),
* still open? click the `All` button, which toggles the same thing, and verify,
* runs every `CHAT["check_interval_seconds"]` (15 s) during the common case, and
  explicitly before/between the drop scans,
* if Tesseract is missing it disables itself with a warning instead of blindly
  pressing keys.

### 8.4 Persistent runtime stopwatch

`core.RuntimeTimer` accumulates total runtime in `runtime_total.json`
(flushed every 10 s and on every exit path: `!kill`, ESC, crash, ctrl-c), so a
kill or a restart never resets the benchmark number. `!runtime`, `!status`, the
"program finished" message and the final console line all report it.

### 8.5 One process, supervised

The old build restarted itself by spawning `replay.py` / `redclick.py` and
calling `os._exit(1)`, which is why the Discord bot only existed during the
common case. Now a single process owns the Discord bot, the stopwatch and the
supervisor loop; a restart just starts a fresh `Automation` session (fresh flags
and a fresh brew counter, exactly like a fresh process had).

---

## 9. Changelog

### Structure

* `tester.py`, `replay.py`, `redclick.py` -> `main.py`, `core.py`, `vision.py`,
  `discord_bot.py`, `config.py` (+ `routes/*.json`).
* No more subprocess chain and no more `os._exit(1)` as flow control; the
  supervisor loop in `main()` owns restarts.
* Route files renamed and put in `routes/`:
  `route1_2.json -> route1_leg1.json`, `route1.json -> route1_leg2.json`,
  `route2.json -> route2_leg1.json`. **Contents unchanged.**
* `replay.py` no longer hard-codes a two-leg route: `RouteProfile.legs` is a
  list, so route2 (single leg, no teleport hop) is served by the same code, and
  new routes are a config entry plus a .json.
* ~12 module-level globals -> `core.AutomationState` (documented, lock
  protected, resettable per session).
* The three copies of the colour-blob finders / bezier mouse mover /
  `click_region_multiple_times` collapsed into `vision.Vision` and
  `core.InputController`.
* `clear_pouch()` / `wear_dodgy()` / `prif_click()` (three copies of the same six
  lines) -> one declarative `ClickLargestSolid` step.
* Logging with levels and a log file instead of `print`; every action says why it
  happened.
* Dead code removed: the unused `bounds_x/bounds_y` parameters, the duplicated
  `human_like_mouse_move_and_click*` variants, `draw_bounding_boxes_and_labels`
  (replaced by `--save-debug-image`), the `tab.json`/`new1.json` branch, the
  commented-out Google Cloud Vision remarks, the `five_hp`/`empty_pouch` dead
  paths (the flags are kept so the commented-out low-HP logic can come back).

### Timing regime (unified)

* Two systems before: `time.sleep(random.uniform(...))` scattered through the
  code, and absolute timestamps inside the recorded .json files.
* Now one vocabulary: named delay specs (`Fixed` / `Uniform` / `Gauss`) in
  `config.DELAYS`, sampled by `core.Clock`, which is also what schedules the
  recorded timelines (`sleep_until`). Every sleep site kept its own entry, so
  every distribution is preserved individually.
* `Clock` waits on an Event, so `!kill` / `!restart` take effect immediately
  instead of after the current sleep, and a `TapKey` can no longer leave a key
  stuck down if it is interrupted mid-hold.
* Recorded playback is now scheduled against one monotonic start time (drift
  free) instead of `time.time()` deltas.

### Discord

* Bare-word triggers (`kill`, `screen`, `count`, `plus`, `minus`, `reset`)
  replaced by real prefixed commands with aliases, help text and an
  authorisation check.
* New `!restart` (unattended restart from the top) and `!run <cmd>` (arbitrary
  shell command from the channel).
* New `!status`, `!runtime`, `!routes`.
* The bot starts before the replay phase and survives restarts.
* Blocking work (`insert` key, shell commands, the legacy 1 s waits) moved off
  the event loop with `asyncio.to_thread`, so the bot stays responsive.
* The bot can now post on its own initiative (crash notice, session finished).

### Behaviour / robustness

* Vision and clicks limited to the game canvas; recorded coordinates translated
  automatically (see 8.1).
* Valuable-drop pick-up rewritten (see 8.2) + `--debug-drop` mode.
* Chat watchdog added (see 8.3).
* Persistent runtime stopwatch added (see 8.4).
* Colour finders vectorised with numpy/OpenCV: identical results
  (4-connectivity, exact colour match, bounding-box centres, raster-order tie
  break), roughly 100x faster.
* The old code died with `os._exit(1)` when the red target vanished, and crashed
  the movement-watch thread on the same condition. Now: one missing frame is
  logged and retried, a genuinely lost target raises and the supervisor restarts
  the flow from the top.
* `finally`-protected `shift` handling (`with input.held_key("shift")`), so a
  crash cannot leave shift down.

### Intentionally *not* changed (parity)

* Click/key order, jitter ranges, bezier profile, 51-sample movement, the 5 %
  idle-click hitch, the `(2)`-then-`(1)` inventory-tab presses, the double
  preamble (the coin-pouch/necklace block runs before *both* legs, exactly as
  `playback_events` did), all sleep distributions.
* `!plus` decrements `brew_counter` and `!minus` increments it (that is what
  "plus"/"minus" did before - it counts brews *used*).
* The loot-spam panic check keeps its off-by-two (`recent_window` 6 vs
  `loot_spam_threshold` 8 means it never fires). Set the window to 8 in
  `config.DISCORD` if you want it live.
* `no_orange` ("out of brews") still only acts when the *next* line arrives in
  the Discord channel, like the original.
* The three input libraries are still used for the same things
  (`keyboard` for key presses, `mouse` for vision-driven clicks, `pynput` for
  recorded playback) - for anti-cheat work *how* input is injected matters.

### Two deliberate, documented timing knobs

1. `VISION["scan_interval_seconds"] = 0.8` - the old movement watcher had no
   sleep at all; it was rate-limited by its own slow full-screen python scan
   (~1 frame/s on the reference machine). The vectorised scan would spin ~30x
   faster and react to target movement much sooner, so the cadence is now
   explicit. Set it to `0.0` for maximum responsiveness.
2. `VISION["event_poll_seconds"] = 0.01` - the message watcher used to be a busy
   spin at 100 % of a core; 10 ms is invisible in game. Set `0.0` for the exact
   legacy behaviour.

Startup detection is also much faster than before (the old boxed-region scans
took tens of seconds on a 1920x1080 grab), so the clicking loop begins sooner
after the route finishes. No sleep was removed to achieve that.

---

## 10. Troubleshooting

| Symptom | Look at |
|---|---|
| `could not find a window whose title contains ['RuneLite']` | client not running, or renamed - `GAME_WINDOW["title_contains"]`, or use `--game-region` |
| `no solid red target region inside the game window` | wrong window found (`--calibrate`), highlight plugin off, or the chat box is covering the target |
| Clicks land slightly off in the replay phase | client size differs from `canvas_size`, or `--calibrate` reports a suspicious `recorded offset`; re-record the leg if the client was resized |
| Drop label never found | `DROP["label_color"]` vs your Ground Items highlight colour; `--log-level DEBUG`; `--save-debug-image` |
| `Tesseract not available - the chat watchdog is disabled` | install the Tesseract binary (section 4) |
| Discord silent | token/intents (Message Content Intent must be on in the Developer Portal), `--no-discord` not passed by accident |
| Keys/clicks do nothing | terminal not elevated (Administrator) |
| Bot keeps restarting | read `colourbot.log`: the crash reason is logged before every restart |

---

## 11. Extending it

* **New route:** record the legs, drop them in `routes/`, add a `RouteProfile`
  to `config.ROUTES` (set `drop_keyword` / `drop_item_name` / `expected_drops`).
* **New fixed step in the replay phase:** add a line to `config.SEQUENCES`
  (and a step type in `main.StepRunner.run_step` if it is a new kind of action).
* **New reaction in the common case:** add the relay wording to
  `DISCORD["messages"]`, a flag to `AutomationState.reset_for_new_session`, a
  check in `Automation.watch_game_events`, a handler method, and a branch in
  `run_common_case`.
* **New Discord command:** one decorated function in
  `DiscordService._register_commands`.
* **New timing:** one entry in `config.DELAYS`, referenced by name.
