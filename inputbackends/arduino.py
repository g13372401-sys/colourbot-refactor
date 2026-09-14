"""
inputbackends.arduino -- input from a real USB HID device.
==========================================================

The gold standard.  A cheap microcontroller that can act as a USB HID gadget
(Arduino Leonardo / Pro Micro / Micro, Teensy, Raspberry Pi Pico with TinyUSB,
Digispark, ...) is plugged into the same PC.  The bot sends it one short ASCII
line per action over the board's *serial* interface; the board replays that
action on its *HID* interface.

Windows then receives the mouse and keyboard packets from an actual USB HID
device.  There is no injection API anywhere in the path, so:

    MSLLHOOKSTRUCT.flags == 0        (no LLMHF_INJECTED, no LLMHF_LOWER_IL_INJECTED)
    KBDLLHOOKSTRUCT.flags == 0       (no LLKHF_INJECTED)

and this is true by construction, not by trickery: the events *are* hardware
events.  A driver-level check (Interception's filter driver is visible in the
device stack, a virtual HID device is visible in device manager) finds nothing
either, because the only thing on the machine is a normal HID mouse/keyboard.

Cost: the operator must own the board and flash it once.  The firmware is in
`firmware/hid_relay/hid_relay.ino` in this repository; it is about forty lines.

Wire protocol (ASCII, newline terminated, board answers "K")
-----------------------------------------------------------
    P            ping                       -> "HID1"
    M dx dy      relative mouse move        -> "K"
    D b          mouse button down (1/2/3)  -> "K"
    U b          mouse button up            -> "K"
    W n          wheel, n clicks            -> "K"
    K c          key down, Arduino key code -> "K"
    R c          key up                     -> "K"
    X            release everything         -> "K"

Absolute positioning is done exactly like the interception backend does it: the
host reads GetCursorPos and sends the remaining delta until the pointer is on
the target.  The board only ever sends relative packets - which is what a real
mouse sends.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from .base import BackendUnavailable, InputBackend, normalise_button
from . import keymap
from .winapi import CursorApi, WindowsCursor

LOG = logging.getLogger("colourbot.input.arduino")

_BUTTON_IDS = {"left": 1, "right": 2, "middle": 3, "x": 4, "x2": 5}

# A HID mouse report carries one signed byte per axis.
_MAX_DELTA = 127

# Board descriptions/VID:PIDs worth trying when no port is configured.
_PORT_HINTS = ("arduino", "leonardo", "micro", "teensy", "pico", "rp2040",
               "usb serial device", "ch340", "sparkfun")


class SerialHidBackend(InputBackend):
    """Mouse and keyboard through a USB HID microcontroller."""

    name = "arduino"
    sets_injected_flags = False
    os_interface = ("USB HID device over serial -> real hardware packets "
                    "(no injection API involved at all)")
    needs_hardware = True

    def __init__(self, settings: Optional[dict] = None,
                 port=None, cursor: Optional[CursorApi] = None):
        settings = dict(settings or {})
        self.port_name = settings.get("port")            # e.g. "COM5"; None = auto
        self.baud = int(settings.get("baud", 115200))
        self.timeout = float(settings.get("timeout", 1.0))
        self.wait_for_ack = bool(settings.get("wait_for_ack", True))
        self.max_corrections = int(settings.get("closed_loop_iterations", 12))
        self.settle_seconds = float(settings.get("settle_seconds", 0.002))
        # An ATmega32u4 board reboots when the serial port is opened and needs
        # a moment to re-enumerate its USB HID interfaces before it can type.
        self.reset_delay = float(settings.get("reset_delay", 1.6))
        self._port = port                                # injectable for tests
        self._cursor = cursor
        self._warned_convergence = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._port is None:
            self._port = self._open_serial()
        if self._cursor is None:
            self._cursor = WindowsCursor()
        reply = self._command("P")
        if self.wait_for_ack and reply and not reply.startswith(("HID", "K")):
            raise BackendUnavailable(
                f"the device on {self.port_name!r} answered {reply!r} instead of "
                "'HID1' - is hid_relay.ino flashed onto it?")
        LOG.info("arduino HID relay ready on %s", self.port_name)

    def close(self) -> None:
        if self._port is not None:
            try:
                self._command("X")              # never leave a key/button held
                self._port.close()
            except Exception:                                  # pragma: no cover
                LOG.debug("closing the serial port failed", exc_info=True)
        self._port = None

    def _open_serial(self):
        try:
            import serial                                       # pyserial
            from serial.tools import list_ports
        except ImportError as exc:
            raise BackendUnavailable(
                "the arduino backend needs pyserial (`pip install pyserial`)"
            ) from exc

        port = self.port_name
        if not port:
            for candidate in list_ports.comports():
                blob = f"{candidate.description} {candidate.manufacturer}".lower()
                if any(hint in blob for hint in _PORT_HINTS):
                    port = candidate.device
                    break
        if not port:
            raise BackendUnavailable(
                "no HID relay board found - plug it in, or set "
                "config.INPUT['arduino']['port'] to its COM port.")
        try:
            handle = serial.Serial(port, self.baud, timeout=self.timeout,
                                   write_timeout=self.timeout)
        except Exception as exc:
            raise BackendUnavailable(f"cannot open {port}: {exc}") from exc
        self.port_name = port
        time.sleep(self.reset_delay)              # USB re-enumeration after reset
        return handle

    # -- wire --------------------------------------------------------------
    def _command(self, line: str) -> str:
        if self._port is None:
            raise BackendUnavailable("arduino backend was not started")
        self._port.write((line + "\n").encode("ascii"))
        flush = getattr(self._port, "flush", None)
        if flush:
            flush()
        if not self.wait_for_ack:
            return ""
        reply = self._port.readline()
        if isinstance(reply, bytes):
            reply = reply.decode("ascii", "replace")
        return reply.strip()

    # -- mouse -------------------------------------------------------------
    def get_position(self) -> Tuple[int, int]:
        return self._cursor.get_position()

    def move_to(self, x: int, y: int, source: str = "action") -> None:
        """Closed-loop absolute positioning with 8-bit relative HID packets."""
        x, y = int(x), int(y)
        for _ in range(max(1, self.max_corrections)):
            current_x, current_y = self._cursor.get_position()
            dx, dy = x - current_x, y - current_y
            if dx == 0 and dy == 0:
                return
            # one HID report can only carry -127..127 per axis
            step_x = max(-_MAX_DELTA, min(_MAX_DELTA, dx))
            step_y = max(-_MAX_DELTA, min(_MAX_DELTA, dy))
            self._command(f"M {step_x} {step_y}")
            if self.settle_seconds:
                time.sleep(self.settle_seconds)

        if not self._warned_convergence:
            self._warned_convergence = True
            LOG.warning("pointer did not settle on (%d,%d) in %d packets - "
                        "raise closed_loop_iterations or turn off 'enhance "
                        "pointer precision'", x, y, self.max_corrections)

    def mouse_down(self, button: str = "left", source: str = "action") -> None:
        self._command(f"D {_BUTTON_IDS[normalise_button(button)]}")

    def mouse_up(self, button: str = "left", source: str = "action") -> None:
        self._command(f"U {_BUTTON_IDS[normalise_button(button)]}")

    def scroll(self, dx: int, dy: int, source: str = "action") -> None:
        if dy:
            self._command(f"W {int(dy)}")

    # -- keyboard ----------------------------------------------------------
    # No shift bookkeeping here: the Arduino `Keyboard` library holds shift by
    # itself for ASCII characters that need it ('A', '!', ...), so the host
    # sends the character verbatim.  Named keys use the KEY_* codes from
    # keymap.ARDUINO_KEYS.
    def key_down(self, key: str, source: str = "action") -> None:
        self._command(f"K {keymap.arduino_code(key)}")

    def key_up(self, key: str, source: str = "action") -> None:
        self._command(f"R {keymap.arduino_code(key)}")
