# Input injection, `LLMHF_INJECTED`, and what this repository does now

*Written for the anti-cheat engineers who asked the question, and for whoever
maintains this bot next.  Everything here is reproducible with
`python test_input_flags.py`.*

---

## 0. TL;DR

| | before | after |
|---|---|---|
| how mouse/keyboard events are produced | `mouse`, `keyboard`, `pynput` → `user32.SendInput` / `mouse_event` / `keybd_event` / `SetCursorPos` | a **kernel-mode filter driver** (Interception) or a **real USB HID board** |
| `MSLLHOOKSTRUCT.flags & LLMHF_INJECTED` | **1** | **0** |
| `MSLLHOOKSTRUCT.flags & LLMHF_LOWER_IL_INJECTED` | **1** whenever the bot ran below the hooking process' integrity level | **0** |
| `KBDLLHOOKSTRUCT.flags & LLKHF_INJECTED` | **1** | **0** |
| automation flow, timings, routes, Discord commands | — | **unchanged** |

The answer to the question that was asked is therefore:

> **Yes** - every input method this codebase used set `LLMHF_INJECTED`, and
> `LLMHF_LOWER_IL_INJECTED` on top of it whenever the bot was not elevated.
> The replacement pushes the events in **below** the layer that sets those
> bits, so a low level hook sees `flags == 0`.

---

## 1. The rule the flags follow

`MSLLHOOKSTRUCT_structure_winuser_h.txt` (shipped in this repo) says:

> The event-injected flags. [...] Testing `LLMHF_INJECTED` (bit 0) will tell you
> whether the event was injected. If it was, then testing
> `LLMHF_LOWER_IL_INJECTED` (bit 1) will tell you whether or not the event was
> injected from a process running at lower integrity level.

The bits are not attached by the device, and not by the hook: they are attached
by **win32k** when an event enters through a *user-mode injection API*.  The
pipeline, top to bottom:

```
    (A) user mode:  SendInput / mouse_event / keybd_event / SetCursorPos
              |                                    <-- win32k marks the event:
              |                                        LLMHF_INJECTED, and
              |                                        LLMHF_LOWER_IL_INJECTED if
              |                                        the caller's IL is lower
              v
    +--------------------------------------------------+
    |   win32k raw input thread (RIT)                   |
    +--------------------------------------------------+
              ^
              |                                    <-- nothing is marked here
    (B) kernel mode: mouclass / kbdclass  (the device stacks)
              ^
              |
        HID / i8042 miniport  <---- a real mouse, a real keyboard,
                                    or an upper filter driver inside that stack
              |
              v
    the low level hook chain (WH_MOUSE_LL / WH_KEYBOARD_LL) -> the game client
```

Path **(A)** is the only one that sets the bits.  Path **(B)** cannot: at that
level "injected" has no meaning, because that is precisely where hardware
delivers its data.  Both replacement backends use path (B).

---

## 2. Audit of the old code (what set the flags, and where)

`core.InputController` called three PyPI packages directly.  All three of them
land in path (A) on Windows:

| library | version | what it uses on Windows | evidence |
|---|---|---|---|
| `mouse` | 0.7.1 | `user32.mouse_event(...)` for buttons/wheel, `user32.SetCursorPos(...)` for movement | `mouse/_winmouse.py` lines 188, 193, 197, 200 |
| `keyboard` | 0.13.5 | `user32.keybd_event(...)`, and `user32.SendInput(...)` for unicode | `keyboard/_winkeyboard.py` lines 580-588, 613 |
| `pynput` | 1.7.6+ | `windll.user32.SendInput` | `pynput/_util/win32.py` line 127 |

Call sites that existed before this change:

| file | call | used for |
|---|---|---|
| `core.py` `key_down/key_up/tap/held_key` | `keyboard.press/release` | every in-game hotkey (`2`, `4`, `insert`, `` ` ``, held `shift`) |
| `core.py` `move_and_click` | `mouse.get_position`, `mouse.move(..., duration=)`, `mouse.click()` | every vision-driven stroke and click |
| `core.py` `click_here` / `click_region` | `mouse.click()` | idle clicking, region clicking |
| `core.py` `play_timeline` | `pynput` `Controller.position/press/release/scroll`, keyboard `press/release` | replay of the recorded routes |
| `discord_bot.py` `_tap_screenshot_key` | `keyboard.press/release("insert")` | `!screenshot` before a session exists |

`mouse_event` and `keybd_event` are documented as superseded by `SendInput`;
they are the same win32k path with an older signature, so they are marked
identically.  `SetCursorPos` is worth calling out separately: it produces a
*mouse move* event that is also flagged as injected, so "just teleport the
cursor and click" was never a way around this.

Consequence, in the words of a hook: **every single event this bot produced was
one bit away from being classified as emulated**, and if the client's hook lived
in an elevated process (a launcher, an anti-cheat service) bit 1 was set too.

---

## 3. Options considered

| approach | verdict |
|---|---|
| `SendInput` with a magic `dwExtraInfo` | **no** - `dwExtraInfo` is a *separate* field; the flags are set regardless. It only helps you recognise your own events. |
| `SetCursorPos` + `mouse_event` (what the old `mouse` package does) | **no** - both are path (A), both are flagged. |
| Running the bot elevated / as SYSTEM | **partial** - clears `LLMHF_LOWER_IL_INJECTED` only. `LLMHF_INJECTED` stays. |
| Journal playback hooks (`WH_JOURNALPLAYBACK`) | **no** - removed/neutered since Vista, and the events are flagged as injected anyway. |
| Patching win32k / hooking `NtUserSendInput` to clear the bits | **no** - PatchGuard, HVCI, and it is exactly the kind of thing an anti-cheat looks for. |
| Posting `WM_MOUSEMOVE`/`WM_LBUTTONDOWN` to the client window | **no** - never reaches the hook chain at all (so "no flags" is meaningless), and any client that reads raw input or `GetAsyncKeyState` ignores it. Also breaks the flow, since the client is not the only consumer. |
| ViGEmBus / virtual gamepad | **no** - gamepad only, no mouse/keyboard HID. |
| **Interception (kernel filter driver)** | **chosen (default)** - software only, works on any Windows PC, events enter through the device stack. |
| **Microcontroller as a USB HID device** | **chosen (best)** - the events *are* hardware; nothing to detect on the PC at all. Needs a ~5 EUR board. |

---

## 4. The new method

### 4.1 `interception` - kernel filter driver (default)

[Interception](https://github.com/oblitum/Interception) is a signed kernel-mode
**upper filter driver** registered on the two human-input device classes:

```
HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E96B-E325-11CE-BFC1-08002BE10318}  keyboards
HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E96F-E325-11CE-BFC1-08002BE10318}  mice
```

It sits *inside* the device stacks, above `kbdclass`/`mouclass`.  Its user-mode
library `interception.dll` lets a process push
`InterceptionKeyStroke` / `InterceptionMouseStroke` structures into a chosen
device.  Those strokes are completed into the class driver's read queue, i.e.
they take path (B): the RIT, raw input, low level hooks, `GetAsyncKeyState`,
window messages and the game all see them as *that device* having moved.

Installation (once, on the bot PC):

```bat
:: elevated prompt, from the Interception release
install-interception.exe /install
:: reboot, then put the matching interception.dll (x64 for 64-bit python)
:: next to main.py or point config.INPUT["interception"]["dll"] at it
```

Implementation: `inputbackends/interception.py`.

* devices are picked by asking `interception_get_hardware_id` which slots are
  really populated (keyboards are 1-10, mice 11-20), so strokes come out of a
  device that actually exists;
* **movement is relative by default** - real mice send deltas, and Windows
  applies its pointer ballistics to them.  The bot thinks in absolute screen
  pixels, so the backend steers in a closed loop: read `GetCursorPos`, send the
  remaining delta, read again (`closed_loop_iterations`, default 8).  A side
  effect worth having: the acceleration curve applies to the bot exactly as it
  applies to the operator's hand;
* `move_mode: "absolute"` is available (one 0..65535 virtual-desktop packet,
  pixel exact) but it looks like a digitiser/tablet on the wire, so it is not
  the default;
* keys are sent as **PS/2 set-1 scan codes** with the `E0` extended flag where a
  real keyboard would use one (`insert` = `0x52` + `E0`) - see
  `inputbackends/keymap.py`;
* `information` is left at 0, which is what a real device's strokes carry.

### 4.2 `arduino` - a real USB HID device (the strongest option)

`inputbackends/arduino.py` + `firmware/hid_relay/hid_relay.ino`.

A board that can present a USB HID interface (Leonardo / Pro Micro / Micro /
Teensy / RP2040) is plugged into the same PC.  The bot sends one short ASCII
line per action over the board's serial port; the board replays it on its HID
interfaces:

```
    P            ping                        -> "HID1"
    M dx dy      relative mouse move         -> "K"
    D b / U b    mouse button down / up      -> "K"
    W n          wheel                       -> "K"
    K c / R c    key down / up               -> "K"
    X            release everything          -> "K"
```

Windows receives ordinary USB HID packets.  There is no injection API in the
path and no driver on the machine to find - the only artefact is a normal HID
mouse/keyboard plus a serial port.  Absolute positioning uses the same closed
loop as above, because a HID mouse report carries a signed byte per axis
(±127 px per packet).

### 4.3 `sendinput` - the old path, kept deliberately

`inputbackends/sendinput.py` is the previous behaviour, unchanged, and it is
still the fallback when neither the driver nor the board is present (and the
transport the emulator test uses on a virtual desktop).  It exists for three
reasons: it is the **control case** of the flag test (if *it* comes back clean,
the test is broken), it keeps the bot runnable on a bare machine, and it
documents what the anti-cheat team is comparing against.  It announces itself
in capitals in the log, and `config.INPUT["allow_flagged_fallback"] = False`
turns a silent downgrade into a clean start-up failure.

---

## 5. What changed in the repository

```
inputbackends/__init__.py      backend registry, "auto" selection, singleton
inputbackends/base.py          the six primitives + the 120 Hz interpolation
inputbackends/interception.py  kernel filter driver transport      [new default]
inputbackends/arduino.py       USB HID board transport
inputbackends/sendinput.py     the old mouse/keyboard/pynput transport
inputbackends/keymap.py        one key table: canonical <-> scan code / HID / lib
inputbackends/winapi.py        GetCursorPos & friends (queries only)
firmware/hid_relay/hid_relay.ino   the board's forty lines
test_input_flags.py            the flag test (this document's evidence)
config.py         INPUT = {...} section 7b: which transport, and its settings
core.py           InputController now calls self.backend.* for all six primitives
discord_bot.py    !screenshot's fallback uses the backend, not keyboard.press
main.py           --input-backend / --list-input-backends, transport opened at
                  start-up so a missing driver fails loudly and early
```

Call flow, unchanged above the dashed line:

```
main.py / vision.py / discord_bot.py
        |  click_region(), tap(), move_and_click(), play_timeline()
        v
core.InputController      bezier path, easing, jitter, DELAYS   <- untouched
- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
inputbackends.get_backend()
        |
        +-- interception  -> interception.dll -> filter driver -> device stack
        +-- arduino       -> serial -> microcontroller -> USB HID
        +-- sendinput     -> mouse/keyboard/pynput -> user32 (FLAGGED)
```

**Nothing about the automation changed.**  The bezier curve, the 51 samples,
the `ease_in_out` easing, the random jitter draws (in the same order, from the
same ranges), every entry of `config.DELAYS`, the route `.json` files, the
vision code and the Discord commands are exactly as they were.  The backend
layer even re-implements `mouse.move(..., duration=d)`'s 120 Hz interpolation,
so a stroke produces the same number of intermediate positions, at the same
rate, over the same wall-clock time.

### Configuration

```python
INPUT = {
    "backend": "auto",              # auto | interception | arduino | sendinput
    "auto_order": ["interception", "arduino", "sendinput"],
    "allow_flagged_fallback": True, # False = never silently fall back to SendInput
    "interception": {"dll": None, "keyboard_device": None, "mouse_device": None,
                     "move_mode": "relative", "closed_loop_iterations": 8,
                     "settle_seconds": 0.001},
    "arduino": {"port": None, "baud": 115200, "timeout": 1.0,
                "wait_for_ack": True, "closed_loop_iterations": 12,
                "settle_seconds": 0.002},
}
```

```bat
python main.py --list-input-backends
python main.py --route route1 --input-backend interception
python main.py --route route1 --input-backend arduino
```

---

## 6. How to verify (the test)

```bat
python test_input_flags.py          :: runs anywhere, no driver, no hardware
python test_input_flags.py --live   :: Windows: a REAL low level hook
```

`--live` is the authoritative run and is what the anti-cheat team should
reproduce: it installs `WH_MOUSE_LL` and `WH_KEYBOARD_LL` - the same hooks the
game client uses - on a dedicated thread with its own message pump, drives each
backend that can start on the machine, and reads `flags` straight out of the
real `MSLLHOOKSTRUCT` / `KBDLLHOOKSTRUCT`.  Expected result:

```
  [PASS] sendinput: control case is detectable, as documented
         N events seen by the hook, N with LLMHF_INJECTED
  [PASS] interception: NO LLMHF_INJECTED / LLMHF_LOWER_IL_INJECTED
         every event arrived with flags == 0
```

The default mode runs the **same backend code** (the same stroke structs, the
same serial protocol, the same closed-loop movement) against a model of the
pipeline in section 1: events handed to `SendInput`/`mouse_event`/`keybd_event`/
`SetCursorPos` are marked, events handed to the device stack are not.  It also
checks the things that would silently break the property:

* every `InputController` path (hotkeys, vision clicks, idle clicks, recorded
  route replay) really goes through the backend;
* recorded routes are still tagged `source="replay"`;
* one bezier stroke still emits more than `config.MOUSE["steps"]` positions;
* **no module of the bot calls an injection API directly any more** (a grep-style
  audit of `core.py`, `main.py`, `vision.py`, `discord_bot.py`).

The end-to-end behaviour test is unchanged and still passes:
`python test_emulator_flow.py` (see `EMULATOR.md`).

---

## 7. What this does *not* hide (please detect us with these instead)

Being straight with the people receiving this deliverable - the flags are gone,
but the bot is not invisible:

1. **The driver is visible.**  With the `interception` backend the machine has a
   service named `interception`, `C:\Windows\System32\drivers\keyboard.sys` /
   `mouse.sys` from the project, and `UpperFilters` entries on the two device
   classes.  Enumerating the device stack of the keyboard/mouse (or just
   checking those registry values) finds it.  The `arduino` backend has none of
   this, but exposes a USB composite device with a HID interface and a serial
   interface with a hobbyist VID/PID - also enumerable.
2. **Timing and geometry.**  The movement is a cubic bezier with fixed easing
   and 51 samples, and the delays come from `config.DELAYS` - a distribution
   fingerprint that survives any transport change.  Closed-loop absolute
   positioning also produces short bursts of same-direction deltas that a real
   hand does not make, and the pointer arrives *exactly* on a pixel and stops.
3. **Behaviour.**  Reaction times to `smited!`, the 20-minute session cadence,
   perfect inventory geometry, clicking the same regions to the pixel - all
   untouched by this work.
4. **The process.**  A python process screen-grabbing at a fixed rate,
   `PIL.ImageGrab`/`cv2` in memory, and a Discord bot on the same machine.
5. **HID descriptors and report cadence.**  A real gaming mouse reports at
   500-1000 Hz with characteristic jitter; a relayed HID board reports only when
   the bot has something to say.  Comparing raw input device metadata
   (`GetRawInputDeviceInfo`) with the events' timing is a much stronger signal
   than `LLMHF_INJECTED` ever was.

If the next iteration of the anti-cheat wants a single, cheap, robust check to
replace the injected-flag test, the honest recommendation is: correlate
`WM_INPUT` raw-input device handles with the events, and profile inter-event
timing per device - not the flag bits.

---

## 8. Operational notes

* **Fallback:** with `"backend": "auto"` the first transport that starts wins.
  The chosen one is logged on one line at start-up; the legacy one is logged as
  a `WARNING` with `<-- DETECTABLE`.  Set `allow_flagged_fallback: False` on a
  live account so a missing driver stops the run instead of quietly going back
  to `SendInput`.
* **Failure to install the driver** (`interception_create_context() failed`)
  usually means the driver was installed but the machine was not rebooted.
* **Pointer never quite arrives** (a warning about corrections): another
  process is fighting for the cursor, or "Enhance pointer precision" is making
  small deltas non-linear - raise `closed_loop_iterations` or switch
  `move_mode` to `"absolute"`.
* **The board answers `?`**: the firmware is older than the protocol; reflash
  `firmware/hid_relay/hid_relay.ino`.
* **Rollback** is one line: `config.INPUT["backend"] = "sendinput"` (or
  `--input-backend sendinput`) restores the previous behaviour exactly,
  including the flags.

---

## Appendix A - what the new backends put on the wire

Captured by driving the backends against a stub that implements the
`interception.h` ABI byte for byte (and against the HID relay protocol parser),
so the anti-cheat team knows exactly what to look for at the device level.

`interception` (mouse device 11, keyboard device 1 - the first slots that report
a hardware id):

| action | `InterceptionMouseStroke` / `InterceptionKeyStroke` |
|---|---|
| move to (340,260) from (100,100) | `state=0x000 flags=0x000 x=+240 y=+160` (relative delta, re-read and repeated until `GetCursorPos` matches) |
| move, `move_mode="absolute"` | `flags=0x003 (MOVE_ABSOLUTE\|VIRTUAL_DESKTOP) x=32784 y=32797` for the centre of a 1920x1080 desktop |
| left button down / up | `state=0x001` / `state=0x002` |
| right button down | `state=0x004` |
| wheel, 2 clicks back | `state=0x400 rolling=-240` (2 x `WHEEL_DELTA`) |
| `insert` down / up | `code=0x52 state=0x02` / `state=0x03` (set-1 scan code + `INTERCEPTION_KEY_E0`) |
| `2` down | `code=0x03 state=0x00` |
| `shift` down | `code=0x2A state=0x00` |

`information` is 0 on every stroke, which is what a real device produces.

`arduino` (one line per action, board acknowledges each with `K`):

```
    M 127 40      relative move, capped at the +/-127 a HID report can carry
    D 1 / U 1     left button down / up
    W -2          wheel
    K 209 / R 209 insert down / up   (Arduino Keyboard.h KEY_INSERT = 0xD1)
    K 50          '2' down           (printable keys are sent as ASCII)
```

## Appendix B - files to review, in reading order

1. `INPUT_INJECTION.md` (this file) - the argument.
2. `inputbackends/base.py` - the six primitives and the 120 Hz interpolation
   that keeps the old timing.
3. `inputbackends/interception.py` - the default transport, with the pipeline
   explanation at the top.
4. `inputbackends/arduino.py` + `firmware/hid_relay/hid_relay.ino` - the
   hardware transport.
5. `inputbackends/sendinput.py` - what it replaced, and why that was detectable.
6. `core.py` section 5b - the only consumer; note that everything above the
   backend call (bezier, easing, jitter, delays) is unchanged.
7. `test_input_flags.py` - the evidence, and `test_emulator_flow.py` - proof
   that the automation still behaves identically.
