# The emulator test

`test_emulator_flow.py` runs the **unmodified** script - literally
`python main.py --route route1` - against a fake game client and a fake Discord,
both of which you can watch on screen, and records whether the automation flow
behaved the way the README says it should.

There are no other tests in this repository, and that is deliberate: the flow is
a sequence of timed, vision-driven reactions, and the only honest way to test it
is to let it react to something that behaves like the real client.

```bat
python test_emulator_flow.py
```

That is the whole invocation. It takes about **four minutes** - the same four
minutes the real flow takes, because none of the script's delays are stubbed
out - and finishes with `RESULT: PASSED` or a list of what did not hold.

---

## 1. What you see while it runs

A single window called **`colour-bot emulator`** opens with the virtual
1920x1080 desktop in it:

| on screen | what it is |
|---|---|
| left, big | the fake game client, window title `RuneLite - EmuAcct` |
| right | the fake Discord, window title `Discord - #bot-control` |
| the pointer | the desktop cursor the script is actually moving |
| yellow/blue rings | left / right clicks, drawn where they landed |
| key caps, bottom | keys the script is pressing; `shift *` means still held |
| the fading blue line | the last ~90 mouse positions, i.e. the movement path |
| top strip | live counters: moves, clicks, keys, grabs, Discord in/out |
| bottom right | the scenario: the step running now, and pass/fail so far |
| bottom left | an event log: every click, key, chat line and game event |

The Discord window is a real message list. You watch the operator type
`!status`, the bot answer, the RuneLite relay post `EmuAcct received a valuable
drop: ...`, and the bot broadcast `@everyone VALUEABLE DROP !!!` into the
channel and DM it to the operator.

If there is no display (CI, ssh), the run works exactly the same - it just skips
the live window. Force that with `--no-window`.

---

## 2. What it produces

Everything lands in `--out` (default: a `colourbot-emulator` folder in your temp
directory), and the path is printed at the end:

```
run.mp4                the entire run, 15 fps, viewer layer included
NN-<step>.png          one snapshot per scenario step, in order
bot-stdout.log         the script's stdout/stderr
colourbot.log          the script's own log file
runtime_total.json     the runtime counter the script keeps
```

The console report has four blocks: the **expectations** (each one named, with
how long the script took to satisfy it), **what the script did** (input and
vision counters plus the final game state), the **artifacts**, and the result
line. Exit code is `0` only if every expectation held.

`colourbot.log` and `runtime_total.json` are real files the script writes into
the repository. The runner snapshots both before the run and restores them
afterwards, so running the test leaves the working tree exactly as it found it.

---

## 3. Options

```
--out PATH        where to write artifacts        (default: <tmp>/colourbot-emulator)
--fps N           video frame rate                (default: 15)
--no-window       do not open the live window     (still records the mp4)
--no-video        no mp4, snapshots only
--timeout SEC     abort the run after this long   (default: 1200)
```

---

## 4. How the script is fooled

The bot only ever touches the outside world through six interfaces. The
emulator implements the other side of all six; no file the bot uses is edited,
patched or subclassed to make the test work:

| interface | used for | replaced by |
|---|---|---|
| `PIL.ImageGrab` | "what is on the screen" | monkeypatched in `emulator/shims/sitecustomize.py` |
| `mouse`, `keyboard` | human-like moves, clicks, key taps | `emulator/shims/mouse.py`, `keyboard.py` |
| `pynput` | replaying the recorded route timelines | `emulator/shims/pynput/` |
| `wmctrl -lG` | "where is the window called RuneLite" | `emulator/bin/wmctrl` |
| `pytesseract` | chat prompt + ground item labels | the real one, shelling out to `emulator/bin/tesseract` |
| `discord.py` | the control channel and the relayed game chat | `emulator/shims/discord/` |

`mouse`, `keyboard`, `pynput` and `discord` are plain modules that sit earlier on
`sys.path` than the real packages, so `import mouse` finds the emulator's.
`ImageGrab` cannot be done that way - the bot needs the rest of Pillow for real -
so `PIL.ImageGrab.grab` is replaced at interpreter start-up instead. OCR is left
alone entirely: the real `pytesseract` runs, and the `tesseract` it executes is
the emulator's.

The injection is three environment variables on the child process:

```python
PYTHONPATH = emulator/shims : <repo>        # the shims win over the real packages
PATH       = emulator/bin   : <PATH>        # the fake wmctrl / tesseract win
COLOURBOT_EMULATOR_SOCKET = /tmp/.../emu.sock
```

`emulator/shims/sitecustomize.py` is imported automatically by CPython at
interpreter start, connects to that socket, and refuses to do anything unless
`sys.argv[0]` is `main.py` - so an unrelated Python process that happens to
inherit the environment is left alone.

Every shim call is one request on a **unix domain socket** (4-byte length, JSON
header, optional raw payload; see `emulator/protocol.py`). Nothing listens on a
TCP port, nothing resolves a hostname: the test runs with no network at all,
including the Discord side.

### Credentials

The runner passes `COLOURBOT_DISCORD_TOKEN=EMULATOR.FAKE.TOKEN` and a fake user
id in the environment, which is the override path the README already documents.
`config.py` is not touched and no real token is ever needed or used.

---

## 5. The fake game client

`emulator/game_client.py` is a small but real game, not a screenshot player. It
has an inventory with 28 slots and actual items, a prayer orb, a coin pouch, a
spellbook tab, a chat box, ground items, and a target NPC that wanders, takes
damage, dies and respawns. It renders itself at `947x650` inside a `955x680`
window with 26 px of chrome, and the window is placed at `(60,120)` on the virtual desktop - so the
script's window search, canvas refinement and `translate_recorded` offset all do
real work rather than being handed the answer.

Three details matter for fidelity:

* **Colours are exact.** Every highlight is painted in the exact RGB triples
  from `config.COLORS`, and text is drawn without anti-aliasing, so the masks
  the bot builds are bit-for-bit what it would build against the real client
  (`config.VISION["color_tolerance"]` is `0`, and the emulator honours that).
* **Screenshots do not contain the cursor.** The pointer, ripples, key caps and
  HUD are drawn into a separate overlay layer that only the viewer sees. Making
  the input visible therefore cannot change what the vision code reads.
* **The screen refreshes at 50 Hz.** Repeated grabs inside one 20 ms tick get
  identical pixels, exactly like a real monitor, no matter how hard the vision
  threads poll.

OCR is a constrained-vocabulary stand-in (`server.LexiconOCR`) that implements
the pytesseract contract - `image_to_string` and `image_to_data` with the same
columns - by matching rendered text against the strings the client can actually
show. The `tesseract` on PATH is a dependency-free script, so the
`--version` probe the chat watchdog does before every OCR stays cheap.

### It does not import `config.py`

Every number the emulator needs - the window title, the colours, the chat
prompt rectangle, the inventory-anchor offsets - is written out in the emulator
with a comment naming the `config.py` key it mirrors. That is on purpose: an
emulator that read the same config as the bot would agree with it by
construction, and the class of bug this repository actually has ("this config
makes the script behave differently") would be invisible.

The practical consequence: **if you change the geometry, colours or chat
settings in `config.py`, change the matching constant in `emulator/` too**, or
the test will start failing - which is the correct thing for it to do, because
that is exactly what a real client with the wrong settings would do.

---

## 6. The fake Discord

`emulator/discord_server.py` implements the parts of `discord.py` the bot layer
uses: a client with intents, a gateway login, `Bot`/`commands` with the
decorator and converter behaviour, channels, DMs, `@everyone`, and the
`on_message` path.

Messages are injected **at the integration level** - a message object arriving
through the gateway, the same way the library would deliver one - so
`discord_bot.py` runs unchanged and the whole command surface is under test:

* `!status`, `!count`, `!plus 3`, `!screenshot`, `!run <shell command>`,
  `!kill`, and an unknown command
* the RuneLite relay account posting game events into the channel, which is how
  `smited!`, `dodgy necklace crumbled`, `Shadow Veil has faded!` and the
  valuable-drop broadcast reach the bot
* a clanmate posting chatter, so the bot's filtering is exercised too

Both directions are visible in the Discord window as they happen.

---

## 7. What the run actually tests

`emulator/scenario.py` drives one full session and records the expectations. In
order:

1. the Discord bot logs in, asks for the `message_content` intent, answers
   `!status` and `!count`, converts `!plus 3` properly, and answers an unknown
   command politely
2. the game window is found through the window manager; `route1` is replayed -
   leg 1, the teleport hop (screenshot hotkey, then the black tile), leg 2, and
   the arrival sequence
3. the common case starts: the red target is found and attacked, and every click
   is checked to land inside the canvas
4. `smited!` -> a brew is drunk and the prayer orb clicked back on, and the brew
   counter goes up
5. `no space for your loot!` -> the coin pouch is emptied and junk shift-dropped
6. `dodgy necklace crumbled` -> a fresh necklace is worn
7. `Shadow Veil has faded!` -> spellbook tab, recast, inventory tab restored
8. the target wanders off -> the bot notices and clicks its new position
9. `!screenshot` -> the `insert` hotkey fires, which pops the chat box open
10. the chat watchdog OCRs the prompt and closes it again with `` ` ``
11. a valuable drop is broadcast -> the channel announcement, the operator DM,
    then the pick-up routine: free the inventory, OCR the ground label, click
    the piles, screenshots either side
12. the session restarts itself, announces it, and replays the route from the top
13. `!kill` -> the bot says goodbye with the total runtime and the process exits `0`

Each expectation is polled with a deadline against the emulator's own record of
what happened - clicks, keys, game events - never by reaching into the script's
state. A failure is recorded and the run continues, because a run that keeps
going after one broken step tells you far more than one that stops at the first.

---

## 8. Files

```
test_emulator_flow.py        the entry point
emulator/__init__.py         the module map
emulator/protocol.py         socket framing
emulator/render.py           drawing primitives (flat text, exact fills)
emulator/desktop.py          the virtual 1920x1080 desktop
emulator/game_client.py      the fake RuneLite client
emulator/discord_server.py   the fake Discord
emulator/server.py           ties them together, serves the shims, OCR
emulator/scenario.py         the scripted run and its expectations
emulator/checks.py           the expectation ledger
emulator/viewer.py           live window, mp4, snapshots
emulator/shims/              sitecustomize (ImageGrab), mouse, keyboard,
                             pynput, discord
emulator/bin/                fake wmctrl and tesseract
```

Nothing in `emulator/` is imported by the bot, and no file the bot uses was
changed to make the test work.
