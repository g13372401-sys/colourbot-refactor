"""
inputbackends.base -- the interface every input backend implements.
===================================================================

Why this file exists
--------------------
Everything the bot does to the outside world is one of six primitive actions:

    move the pointer, press a mouse button, release a mouse button,
    turn the wheel, press a key, release a key

Historically `core.InputController` called the `mouse` / `keyboard` / `pynput`
PyPI packages directly for those six things.  All three of them end up in
`user32.SendInput` (or its older cousins `mouse_event` / `keybd_event`) on
Windows, and every event that enters the system through those functions is
tagged by win32k with **LLMHF_INJECTED** (and, when the sending process runs at
a lower integrity level than the process that owns the hook,
**LLMHF_LOWER_IL_INJECTED**).  A game client that installs a WH_MOUSE_LL /
WH_KEYBOARD_LL hook sees those bits and knows the input was emulated.

This module turns those six primitives into an interface so the *transport*
underneath them can be swapped for one that does not go through the user-mode
injection APIs at all:

    interception  -- a kernel-mode filter driver: strokes are handed to the
                     keyboard/mouse class driver stack, i.e. they enter the
                     system where a real device's data enters it
    arduino       -- a real USB HID device (a microcontroller) that types and
                     clicks for us; from Windows' point of view this *is*
                     hardware
    sendinput     -- the legacy path (mouse/keyboard/pynput).  Kept only as a
                     documented, clearly flagged fallback and as the control
                     case in the flag test.

Every backend declares `sets_injected_flags`, which is the single fact the
anti-cheat engineers care about, and `describes()` a human readable summary of
the exact OS interface it uses.

Contract notes
--------------
* Coordinates are absolute, in screen pixels, top-left origin - the same
  coordinates `mouse.move(x, y, absolute=True)` took, so no caller changes.
* Key names are the canonical names in `inputbackends.keymap` (which accepts
  both `keyboard`-style "page up" and `pynput`-style "page_up" spellings).
* `source` is either "action" (vision-driven clicking and typing) or "replay"
  (playback of a recorded route).  Real hardware cannot tell the two apart and
  ignores it; the legacy backend uses it to keep using exactly the library it
  used before this change, so recorded-route playback behaves bit for bit as it
  did (this is what the emulator test asserts on).
* `move()` keeps the *timing* profile of the old code: when `duration` is
  non-zero the movement is interpolated at 120 Hz, which is precisely what
  `mouse.move(..., duration=d)` did.  The bezier path, the easing and the
  random draws all still happen in `core.InputController`; only the last mile
  changed.
"""

from __future__ import annotations

import logging
import time
from typing import Tuple

LOG = logging.getLogger("colourbot.input")

# The two bits from MSLLHOOKSTRUCT/KBDLLHOOKSTRUCT this whole exercise is about.
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002

BUTTONS = ("left", "right", "middle", "x", "x2")


class BackendUnavailable(RuntimeError):
    """Raised when a backend cannot be used on this machine.

    The message is shown to the operator, so it must say *what* is missing and
    *how* to get it (driver not installed, board not plugged in, ...).
    """


class InputBackend:
    """Abstract transport for the six input primitives.

    Subclasses implement `move_to`, `mouse_down`, `mouse_up`, `scroll`,
    `key_down`, `key_up` and `get_position`; everything else has a working
    default here.
    """

    #: short id used in config.INPUT["backend"] and on the command line
    name = "abstract"

    #: True  -> events carry LLMHF_INJECTED / LLKHF_INJECTED (detectable)
    #: False -> events enter through the device stack and carry no flags
    sets_injected_flags = True

    #: one line for the log/report: which OS interface the events go through
    os_interface = "n/a"

    #: does this backend need extra hardware on the operator's desk?
    needs_hardware = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Open the driver / serial port.  Raise BackendUnavailable if it fails."""

    def close(self) -> None:
        """Release the driver / serial port.  Must be safe to call twice."""

    # -- required primitives ----------------------------------------------
    def get_position(self) -> Tuple[int, int]:
        raise NotImplementedError

    def move_to(self, x: int, y: int, source: str = "action") -> None:
        raise NotImplementedError

    def mouse_down(self, button: str = "left", source: str = "action") -> None:
        raise NotImplementedError

    def mouse_up(self, button: str = "left", source: str = "action") -> None:
        raise NotImplementedError

    def scroll(self, dx: int, dy: int, source: str = "action") -> None:
        raise NotImplementedError

    def key_down(self, key: str, source: str = "action") -> None:
        raise NotImplementedError

    def key_up(self, key: str, source: str = "action") -> None:
        raise NotImplementedError

    # -- provided on top of the primitives --------------------------------
    def click(self, button: str = "left", source: str = "action") -> None:
        """A press immediately followed by a release, like `mouse.click()`."""
        self.mouse_down(button, source)
        self.mouse_up(button, source)

    def move(self, x: int, y: int, duration: float = 0.0,
             source: str = "action") -> None:
        """Absolute move, optionally animated over `duration` seconds.

        This is a verbatim re-implementation of `mouse.move(x, y,
        absolute=True, duration=d)`: 120 interpolated positions per second and
        a sleep of `duration / steps` between them.  Keeping it identical
        matters twice over - the stroke takes exactly as long as it used to
        (the flow's timing is unchanged), and the event *rate* an anti-cheat
        would profile is unchanged too.
        """
        x, y = int(x), int(y)
        if not duration:
            self.move_to(x, y, source)
            return

        start_x, start_y = self.get_position()
        dx, dy = x - start_x, y - start_y
        if dx == 0 and dy == 0:
            time.sleep(duration)
            return

        steps = max(1.0, float(int(duration * 120.0)))
        for i in range(int(steps) + 1):
            self.move_to(int(start_x + dx * i / steps),
                         int(start_y + dy * i / steps), source)
            time.sleep(duration / steps)

    # -- reporting ---------------------------------------------------------
    def describe(self) -> str:
        flag = ("SETS LLMHF_INJECTED (detectable)" if self.sets_injected_flags
                else "no LLMHF_INJECTED / LLMHF_LOWER_IL_INJECTED")
        return f"{self.name}: {self.os_interface} -> {flag}"

    def __repr__(self) -> str:                            # pragma: no cover
        return f"<{type(self).__name__} {self.name}>"


def normalise_button(button) -> str:
    """Accept 'left'/'Button.left'/pynput Button objects -> 'left'."""
    name = getattr(button, "name", button)
    name = str(name).lower().replace("button.", "").strip()
    if name in ("1", "primary"):
        name = "left"
    elif name in ("2", "secondary"):
        name = "right"
    elif name == "3":
        name = "middle"
    if name not in BUTTONS:
        raise ValueError(f"unsupported mouse button {button!r}")
    return name
