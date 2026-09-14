# Always-on input flag checker (crash-safe)

A terminal utility that watches **every** low-level mouse and keyboard event on
the machine and prints an alert the moment an injected event is seen.

It installs exactly the same hooks a game client's anti-cheat uses:

- `SetWindowsHookExW(WH_MOUSE_LL, ...)`   (id 14)
- `SetWindowsHookExW(WH_KEYBOARD_LL, ...)` (id 13)

and reports the flags from `MSLLHOOKSTRUCT` / `KBDLLHOOKSTRUCT`.

## Why

Both automation refactors (colourbot-refactor-claude and
colourbot-refactor-gemini) route bot input through either a kernel driver
(Interception), a real USB HID device, or (as a documented fallback) the old
`SendInput` / `mouse_event` / `keybd_event` path. The fallback path and any
leftover direct calls (`keyboard.press`, etc.) set the injected flags that a
game client can detect.

This checker lets you **see the difference live** as you run the actual bot.

## How to run

```
python flag_checker.py            # default: quiet for clean events, alert on injected
python flag_checker.py --verbose  # also print a '.' line for every clean event
```

1. Open a terminal and start the checker.
2. Open a **second** terminal and run the automation script under test
   (`python main.py ...` or the route recorder or the emulator).
3. Clean input prints nothing (or a `.` with `--verbose`).

A line starting with `[INJECTED]` means an event carried one of:

- `LLMHF_INJECTED`           (mouse, bit 0)
- `LLMHF_LOWER_IL_INJECTED`  (mouse, bit 1)
- `LLKHF_INJECTED`           (keyboard, bit 4)
- `LLKHF_LOWER_IL_INJECTED`  (keyboard, bit 1)

...which a game client would also see.

At the end (Ctrl+C) it prints a summary (counts of checks, flags, worker
restarts) and an overall RESULT line.

## Crash-safety design (why it can never freeze your input)

The checker is split into **two processes**:

- **`hook_worker.py`** — the only process that touches `user32`. It installs the
  low-level hooks, runs a native message pump, and streams one JSON object per
  event to its stdout.
- **`flag_checker.py`** — the monitor you run. It spawns the worker, reads the
  JSON stream, prints the alerts, and **never calls `user32` itself**.

This architecture guarantees that a crash can never capture your input:

1. **Root cause found & fixed.** The original crashes were an
   `OverflowError: int too long to convert` inside the hook callback. Cause:
   `ctypes.WinDLL()` is **not** cached per name, so prototypes (`argtypes`)
   declared on a throwaway DLL object were never applied to the DLL object the
   callback used. Without `argtypes`, ctypes converts arguments as **32-bit
   ints**, so a pointer-sized `lparam` overflowed and the callback died *before*
   calling `CallNextHookEx` — swallowing that input event (and, in the worst
   case, leaving a dead process holding the hooks → frozen input → reboot).
   Prototypes are now applied to the exact DLL handles the worker uses, and the
   callback re-coerces `lparam` via `_sign64()` so pointer values pass through
   unchanged.
2. **`CallNextHookEx` is always called.** The callback's observation is
   best-effort (wrapped in try/except), and the mandatory
   `CallNextHookEx(None, code, wparam, lparam)` runs every time, so no event is
   ever dropped by the checker.
3. **Hooks live in a separate process.** The moment the worker exits — cleanly
   or by crashing (an access-violation on a bad pointer can still kill a
   process, and `try/except` cannot catch SEH faults) — the OS automatically
   removes its low-level hooks. Input is immediately released.
4. **Auto-restart.** The monitor detects the worker's exit, prints a notice,
   and starts a fresh worker, so coverage is continuous.

## Important operational notes

**Integrity level.** Low-level hooks only receive injected events from
processes at the *same or lower* integrity level than the hooking process.
Run the checker **as Administrator** to see events from any process.

**Interactive session.** Low-level hooks only function in an interactive
desktop session with a live message pump. They will not install from a
background service, a job object, or certain terminal-host sandboxes (e.g. the
agent shell this was developed in). Run it from your own console window on the
desktop where the bot also runs.

**If the checker reports a hook failure:** an environment problem, not a logic
bug. Two things to try, and what they mean:

- **GetLastError = 126 / ERROR_MOD_NOT_FOUND** — the shell you launched from is
  not attached to an interactive window station/desktop where the OS can
  register a low-level hook callback. Launch it from a normal console window
  (cmd / Windows Terminal / PowerShell) that you opened yourself on the
  desktop, not from an agent, ssh/rdesktop, scheduled task, or service host.
- **GetLastError = 5 / ERROR_ACCESS_DENIED** — run the checker as Administrator
  so it can see injected events from *any* integrity level.

**Only as good as your eyes.** This checker reports *flags*, not intent.
Anything you type yourself also passes through the hooks — but your own input
is real hardware so it carries no injected flags. Only programmatically
injected events light up the alert.
