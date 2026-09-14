"""
inputbackends.interception -- input through a kernel-mode filter driver.
========================================================================

    THIS IS THE BACKEND THAT REPLACES SendInput.  Read this file first.

What it is
----------
Interception (https://github.com/oblitum/Interception) is a signed kernel-mode
*upper filter driver* for the two device classes Windows uses for human input:

    HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4D36E96B-...}  keyboards
    HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4D36E96F-...}  mice

Once installed (`install-interception.exe /install`, then reboot) it sits *in*
the keyboard/mouse device stacks, above kbdclass/mouclass.  Its user-mode API
(`interception.dll`) lets a program push `InterceptionKeyStroke` /
`InterceptionMouseStroke` structures into a chosen device.

Why that removes LLMHF_INJECTED
-------------------------------
The two flags come from win32k, not from the driver stack:

    SendInput / mouse_event / keybd_event / SetCursorPos
        -> win32k.sys builds the event and marks it as injected
           (LLMHF_INJECTED, plus LLMHF_LOWER_IL_INJECTED when the caller's
           integrity level is below that of the hooking process)
        -> the low level hook chain (WH_MOUSE_LL / WH_KEYBOARD_LL) sees the bits

    a device stack IRP (a real mouse, or a driver in that stack)
        -> mouclass/kbdclass -> win32k's raw input thread
        -> the event is indistinguishable from hardware: it is not "injected",
           so both bits stay 0 in MSLLHOOKSTRUCT.flags / KBDLLHOOKSTRUCT.flags

Interception's strokes take the *second* path: they are queued into the class
driver's read queue for the chosen device.  As far as everything above the
driver is concerned - RIT, raw input, low level hooks, GetAsyncKeyState,
the game's own window messages - it is the device that moved.

What is still true (be honest with the anti-cheat team)
------------------------------------------------------
* the driver itself is visible: the service `interception`, the .sys file, the
  UpperFilters registry values, and the fact that a device stack has an extra
  filter object.  This backend hides the *event*, not the driver.
* the strokes carry `information = 0`, exactly like a real device's do.
* movement is sent as *relative* packets (the default), which is what a real
  mouse sends; absolute packets are supported but look like a tablet.

Geometry
--------
The bot thinks in absolute screen pixels, a mouse speaks deltas, and Windows
applies pointer ballistics ("enhance pointer precision") to those deltas.  The
backend therefore steers in a closed loop: read GetCursorPos, send the
remaining delta, read again, repeat (a couple of iterations at most).  That
also means the acceleration curve is applied to our movement exactly as it is
applied to the operator's own hand - which is one detection surface fewer.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from typing import Optional, Tuple

from .base import BackendUnavailable, InputBackend, normalise_button
from . import keymap
from .winapi import CursorApi, WindowsCursor

LOG = logging.getLogger("colourbot.input.interception")

# -- constants from interception.h -----------------------------------------
INTERCEPTION_MAX_KEYBOARD = 10          # devices 1..10 are keyboards
INTERCEPTION_MAX_MOUSE = 10             # devices 11..20 are mice

KEY_DOWN = 0x00
KEY_UP = 0x01
KEY_E0 = 0x02                           # the 0xE0 "extended key" prefix

MOUSE_LEFT_DOWN = 0x001
MOUSE_LEFT_UP = 0x002
MOUSE_RIGHT_DOWN = 0x004
MOUSE_RIGHT_UP = 0x008
MOUSE_MIDDLE_DOWN = 0x010
MOUSE_MIDDLE_UP = 0x020
MOUSE_BUTTON_4_DOWN = 0x040
MOUSE_BUTTON_4_UP = 0x080
MOUSE_BUTTON_5_DOWN = 0x100
MOUSE_BUTTON_5_UP = 0x200
MOUSE_WHEEL = 0x400
MOUSE_HWHEEL = 0x800

MOUSE_MOVE_RELATIVE = 0x000
MOUSE_MOVE_ABSOLUTE = 0x001
MOUSE_VIRTUAL_DESKTOP = 0x002

WHEEL_DELTA = 120

_BUTTON_STATES = {
    "left": (MOUSE_LEFT_DOWN, MOUSE_LEFT_UP),
    "right": (MOUSE_RIGHT_DOWN, MOUSE_RIGHT_UP),
    "middle": (MOUSE_MIDDLE_DOWN, MOUSE_MIDDLE_UP),
    "x": (MOUSE_BUTTON_4_DOWN, MOUSE_BUTTON_4_UP),
    "x2": (MOUSE_BUTTON_5_DOWN, MOUSE_BUTTON_5_UP),
}


class InterceptionKeyStroke(ctypes.Structure):
    """`InterceptionKeyStroke` from interception.h (8 bytes)."""
    _fields_ = [("code", ctypes.c_ushort),
                ("state", ctypes.c_ushort),
                ("information", ctypes.c_uint)]


class InterceptionMouseStroke(ctypes.Structure):
    """`InterceptionMouseStroke` from interception.h (20 bytes)."""
    _fields_ = [("state", ctypes.c_ushort),
                ("flags", ctypes.c_ushort),
                ("rolling", ctypes.c_short),
                ("x", ctypes.c_int),
                ("y", ctypes.c_int),
                ("information", ctypes.c_uint)]


# Where interception.dll usually is.  The config key wins over all of these.
_DLL_CANDIDATES = (
    "interception.dll",
    r"C:\Program Files\Interception\interception.dll",
    r"C:\Interception\interception.dll",
)


class InterceptionBackend(InputBackend):
    """Mouse and keyboard through the Interception kernel filter driver."""

    name = "interception"
    sets_injected_flags = False
    os_interface = ("interception.dll -> kernel filter driver on the "
                    "keyboard/mouse class stacks (device-stack input, not SendInput)")
    needs_hardware = False

    def __init__(self, settings: Optional[dict] = None,
                 dll=None, cursor: Optional[CursorApi] = None):
        settings = dict(settings or {})
        self.dll_path = settings.get("dll")
        self.keyboard_device = settings.get("keyboard_device")     # 1..10
        self.mouse_device = settings.get("mouse_device")           # 11..20
        self.move_mode = settings.get("move_mode", "relative")     # or "absolute"
        self.max_corrections = int(settings.get("closed_loop_iterations", 8))
        self.settle_seconds = float(settings.get("settle_seconds", 0.001))
        self._dll = dll                       # injectable for the flag test
        self._cursor = cursor
        self._context = None
        self._warned_convergence = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._context is not None:
            return
        if self._dll is None:
            self._dll = self._load_dll()
        self._declare_prototypes()
        if self._cursor is None:
            self._cursor = WindowsCursor()

        self._context = self._dll.interception_create_context()
        if not self._context:
            raise BackendUnavailable(
                "interception_create_context() failed - the driver is not "
                "running.  Install it with `install-interception.exe /install` "
                "from an elevated prompt and reboot.")

        if self.keyboard_device is None:
            self.keyboard_device = self._first_device(1, INTERCEPTION_MAX_KEYBOARD,
                                                      "keyboard")
        if self.mouse_device is None:
            self.mouse_device = self._first_device(INTERCEPTION_MAX_KEYBOARD + 1,
                                                   INTERCEPTION_MAX_KEYBOARD
                                                   + INTERCEPTION_MAX_MOUSE, "mouse")
        LOG.info("interception ready (keyboard device %s, mouse device %s, "
                 "%s movement)", self.keyboard_device, self.mouse_device,
                 self.move_mode)

    def close(self) -> None:
        if self._context is not None and self._dll is not None:
            try:
                self._dll.interception_destroy_context(self._context)
            except Exception:                                  # pragma: no cover
                LOG.debug("interception_destroy_context failed", exc_info=True)
        self._context = None

    # -- driver plumbing ---------------------------------------------------
    def _load_dll(self):
        if sys.platform != "win32":
            raise BackendUnavailable(
                "the interception backend needs Windows (this is a Windows "
                "first codebase; on Linux only the legacy backend exists).")
        candidates = [self.dll_path, os.environ.get("INTERCEPTION_DLL")]
        candidates += list(_DLL_CANDIDATES)
        errors = []
        for candidate in [c for c in candidates if c]:
            try:
                return ctypes.WinDLL(candidate)
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")
        raise BackendUnavailable(
            "interception.dll could not be loaded (tried: "
            + "; ".join(errors) + ").  Install the driver from "
            "https://github.com/oblitum/Interception, put the *matching* "
            "architecture dll (x64 for 64-bit python) next to main.py, or set "
            "config.INPUT['interception']['dll'].")

    def _declare_prototypes(self) -> None:
        """argtypes/restype, so 64-bit handles are not truncated to int."""
        dll = self._dll
        if getattr(dll, "_colourbot_prototyped", False):
            return
        try:
            dll.interception_create_context.restype = ctypes.c_void_p
            dll.interception_destroy_context.argtypes = [ctypes.c_void_p]
            dll.interception_send.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_void_p, ctypes.c_uint]
            dll.interception_send.restype = ctypes.c_int
            dll.interception_get_hardware_id.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                         ctypes.c_void_p, ctypes.c_uint]
            dll.interception_get_hardware_id.restype = ctypes.c_uint
            dll._colourbot_prototyped = True
        except AttributeError:            # a fake dll in the tests: nothing to do
            pass

    def _first_device(self, low: int, high: int, what: str) -> int:
        """Pick a device that actually exists, so strokes go somewhere real."""
        buffer = ctypes.create_string_buffer(512)
        for device in range(low, high + 1):
            try:
                size = self._dll.interception_get_hardware_id(
                    self._context, device, buffer, ctypes.sizeof(buffer))
            except Exception:                                  # pragma: no cover
                size = 0
            if size:
                return device
        LOG.warning("no %s reported a hardware id; falling back to device %d "
                    "(unplug/replug the device if nothing moves)", what, low)
        return low

    def _send(self, device: int, stroke) -> None:
        if self._context is None:
            raise BackendUnavailable("interception backend was not started")
        sent = self._dll.interception_send(self._context, device,
                                           ctypes.byref(stroke), 1)
        if not sent:
            LOG.debug("interception_send returned 0 for device %s", device)

    # -- mouse -------------------------------------------------------------
    def get_position(self) -> Tuple[int, int]:
        return self._cursor.get_position()

    def move_to(self, x: int, y: int, source: str = "action") -> None:
        """Put the pointer on an absolute screen pixel.

        `relative` mode (default) steers a real-mouse-looking delta packet in a
        closed loop until GetCursorPos reports the target; `absolute` mode
        sends one 0..65535 virtual-desktop packet, which is exact but looks
        like a digitiser rather than a mouse.
        """
        x, y = int(x), int(y)
        if self.move_mode == "absolute":
            self._move_absolute(x, y)
            return

        for _ in range(max(1, self.max_corrections)):
            current_x, current_y = self._cursor.get_position()
            dx, dy = x - current_x, y - current_y
            if dx == 0 and dy == 0:
                return
            stroke = InterceptionMouseStroke(state=0, flags=MOUSE_MOVE_RELATIVE,
                                             rolling=0, x=dx, y=dy, information=0)
            self._send(self.mouse_device, stroke)
            if self.settle_seconds:
                time.sleep(self.settle_seconds)

        if not self._warned_convergence:
            self._warned_convergence = True
            LOG.warning("pointer did not settle on (%d,%d) within %d corrections "
                        "- if this repeats, turn off 'enhance pointer precision' "
                        "or set move_mode='absolute'", x, y, self.max_corrections)

    def _move_absolute(self, x: int, y: int) -> None:
        left, top, width, height = self._cursor.virtual_screen()
        norm_x = int((x - left) * 65535 / max(1, width - 1))
        norm_y = int((y - top) * 65535 / max(1, height - 1))
        stroke = InterceptionMouseStroke(
            state=0, flags=MOUSE_MOVE_ABSOLUTE | MOUSE_VIRTUAL_DESKTOP,
            rolling=0, x=max(0, min(65535, norm_x)), y=max(0, min(65535, norm_y)),
            information=0)
        self._send(self.mouse_device, stroke)
        if self.settle_seconds:
            time.sleep(self.settle_seconds)

    def _button(self, button: str, pressed: bool) -> None:
        down, up = _BUTTON_STATES[normalise_button(button)]
        stroke = InterceptionMouseStroke(state=down if pressed else up,
                                         flags=MOUSE_MOVE_RELATIVE, rolling=0,
                                         x=0, y=0, information=0)
        self._send(self.mouse_device, stroke)

    def mouse_down(self, button: str = "left", source: str = "action") -> None:
        self._button(button, True)

    def mouse_up(self, button: str = "left", source: str = "action") -> None:
        self._button(button, False)

    def scroll(self, dx: int, dy: int, source: str = "action") -> None:
        if dy:
            self._send(self.mouse_device, InterceptionMouseStroke(
                state=MOUSE_WHEEL, flags=MOUSE_MOVE_RELATIVE,
                rolling=int(dy) * WHEEL_DELTA, x=0, y=0, information=0))
        if dx:
            self._send(self.mouse_device, InterceptionMouseStroke(
                state=MOUSE_HWHEEL, flags=MOUSE_MOVE_RELATIVE,
                rolling=int(dx) * WHEEL_DELTA, x=0, y=0, information=0))

    # -- keyboard ----------------------------------------------------------
    def _key(self, key: str, pressed: bool) -> None:
        code, extended = keymap.scancode(key)
        state = (KEY_DOWN if pressed else KEY_UP) | (KEY_E0 if extended else 0)
        self._send(self.keyboard_device,
                   InterceptionKeyStroke(code=code, state=state, information=0))

    def key_down(self, key: str, source: str = "action") -> None:
        if keymap.needs_shift(key):
            self._key("shift", True)
        self._key(key, True)

    def key_up(self, key: str, source: str = "action") -> None:
        self._key(key, False)
        if keymap.needs_shift(key):
            self._key("shift", False)
