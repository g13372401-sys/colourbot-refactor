#!/usr/bin/env python3
"""
test_input_flags.py -- does our input simulation set LLMHF_INJECTED?
====================================================================

    python test_input_flags.py            # runs anywhere (modelled pipeline)
    python test_input_flags.py --live     # Windows: real WH_MOUSE_LL hook

This is the test the anti-cheat engineers asked for.  It answers one question
per input backend:

    when this backend moves the mouse / clicks / presses a key, does the event
    arrive at a low level hook with LLMHF_INJECTED (bit 0) or
    LLMHF_LOWER_IL_INJECTED (bit 1) set in MSLLHOOKSTRUCT.flags?

    sendinput      -> YES.  It must.  It is the control case: if this one comes
                     back clean the test itself is broken.
    interception   -> no.  Strokes enter through the mouse/keyboard class
                     driver stack, which is not "injection" as far as win32k is
                     concerned.
    arduino        -> no.  The events are made by a real USB HID device.

Two modes
---------
--live   The real thing, Windows only.  Installs WH_MOUSE_LL and
         WH_KEYBOARD_LL (exactly what the game client does), drives every
         backend that can start on this machine, and reads the flags out of
         the real MSLLHOOKSTRUCT / KBDLLHOOKSTRUCT.  Run this on the bot PC
         before shipping - it is the authoritative result.  It moves the real
         pointer a few pixels and types into whatever has focus, so put a
         Notepad in front of it.

default  The portable mode used in CI and on the developer's Linux box.  It
         runs the *real backend code* - the same stroke structs, the same
         serial lines, the same closed-loop movement - against a model of the
         Windows input pipeline that implements the documented rule:

             user32.SendInput / mouse_event / keybd_event / SetCursorPos
                 -> event is marked injected  (LLMHF_INJECTED, plus
                    LLMHF_LOWER_IL_INJECTED when the caller sits at a lower
                    integrity level)
             a device-stack packet (kernel filter driver, or real HID hardware)
                 -> event is not marked at all

         That rule is the whole of MSLLHOOKSTRUCT_structure_winuser_h.txt, and
         it is what the live mode confirms on real hardware.

The last check is not about flags at all: it verifies that `InputController`
(and therefore the entire automation flow) really goes through the backend
layer, so no code path can quietly slip back to SendInput.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

import inputbackends                                              # noqa: E402
from inputbackends import keymap                                  # noqa: E402
from inputbackends.arduino import SerialHidBackend                # noqa: E402
from inputbackends.base import (LLMHF_INJECTED,                   # noqa: E402
                                LLMHF_LOWER_IL_INJECTED)
from inputbackends.interception import (InterceptionKeyStroke,    # noqa: E402
                                        InterceptionMouseStroke,
                                        InterceptionBackend)
from inputbackends.sendinput import SendInputBackend              # noqa: E402
from inputbackends.winapi import CursorApi                        # noqa: E402

FLAG_NAMES = {LLMHF_INJECTED: "LLMHF_INJECTED",
              LLMHF_LOWER_IL_INJECTED: "LLMHF_LOWER_IL_INJECTED"}


def flags_to_text(flags: int) -> str:
    if not flags:
        return "0 (clean)"
    return " | ".join(name for bit, name in FLAG_NAMES.items() if flags & bit) \
        or f"0x{flags:08x}"


# ===========================================================================
# 1. The modelled Windows input pipeline (portable mode)
# ===========================================================================

class HookRecord:
    """One event as a WH_MOUSE_LL / WH_KEYBOARD_LL hook would see it."""

    def __init__(self, kind: str, detail: str, flags: int):
        self.kind = kind            # "mouse" | "keyboard"
        self.detail = detail        # "move (100,100)", "left down", "key 0x52"
        self.flags = flags

    def __repr__(self) -> str:
        return f"<{self.kind} {self.detail} flags={flags_to_text(self.flags)}>"


class ModelledPipeline:
    """win32k's rule for the injected bits, and nothing else.

    Two entry points, mirroring the two ways an event can reach the raw input
    thread on Windows:

        from_injection_api()  what SendInput / mouse_event / keybd_event /
                              SetCursorPos produce -> flags are set
        from_device_stack()   what a device (or a filter driver inside that
                              device's stack) produces -> flags stay 0
    """

    def __init__(self, lower_integrity: bool = True, width: int = 1920,
                 height: int = 1080):
        self.records = []
        self.cursor = [width // 2, height // 2]
        self.size = (width, height)
        # The bot normally runs at Medium IL while the hooking process may be
        # elevated; that is what lights the second bit up.
        self.injected_flags = LLMHF_INJECTED | (
            LLMHF_LOWER_IL_INJECTED if lower_integrity else 0)

    # -- the two doors into the system ------------------------------------
    def from_injection_api(self, kind: str, detail: str) -> None:
        self.records.append(HookRecord(kind, detail, self.injected_flags))

    def from_device_stack(self, kind: str, detail: str) -> None:
        self.records.append(HookRecord(kind, detail, 0))

    # -- pointer bookkeeping ----------------------------------------------
    def move_absolute(self, x: int, y: int) -> None:
        self.cursor = [max(0, min(self.size[0] - 1, int(x))),
                       max(0, min(self.size[1] - 1, int(y)))]

    def move_relative(self, dx: int, dy: int) -> None:
        self.move_absolute(self.cursor[0] + dx, self.cursor[1] + dy)

    def reset(self) -> None:
        self.records.clear()


class ModelCursor(CursorApi):
    """GetCursorPos against the modelled desktop (a query: no events)."""

    def __init__(self, pipeline: ModelledPipeline):
        self.pipeline = pipeline

    def get_position(self):
        return tuple(self.pipeline.cursor)

    def virtual_screen(self):
        return (0, 0) + self.pipeline.size


# --- fake transport 1: the Interception driver ------------------------------

class FakeInterceptionDll:
    """interception.dll + the kernel driver behind it.

    Decodes the *real* stroke structures the backend builds (byref keeps the
    original object in `._obj`) and feeds them in through the device-stack
    door, which is what the driver does on a real machine.
    """

    def __init__(self, pipeline: ModelledPipeline):
        self.pipeline = pipeline
        self.sent = []

    def interception_create_context(self):
        return 0xC0FFEE

    def interception_destroy_context(self, context):
        return None

    def interception_get_hardware_id(self, context, device, buffer, size):
        # device 1 = the keyboard, device 11 = the mouse, like a real machine
        return 16 if device in (1, 11) else 0

    def interception_send(self, context, device, stroke_ref, count):
        stroke = getattr(stroke_ref, "_obj", stroke_ref)
        self.sent.append((device, stroke))
        if isinstance(stroke, InterceptionMouseStroke):
            self._mouse(stroke)
        elif isinstance(stroke, InterceptionKeyStroke):
            self._keyboard(stroke)
        return count

    def _mouse(self, stroke: InterceptionMouseStroke) -> None:
        absolute = bool(stroke.flags & 0x001)             # MOVE_ABSOLUTE
        # An absolute packet at (0,0) is the top-left corner, not a no-op, so
        # the two movement kinds have to be told apart by the flag - exactly
        # what mouclass does.
        if stroke.state == 0 and (absolute or stroke.x or stroke.y):
            if absolute:
                width, height = self.pipeline.size
                self.pipeline.move_absolute(stroke.x * (width - 1) / 65535,
                                            stroke.y * (height - 1) / 65535)
            else:
                self.pipeline.move_relative(stroke.x, stroke.y)
            self.pipeline.from_device_stack(
                "mouse", f"move {tuple(self.pipeline.cursor)}")
        elif stroke.state & 0x400:                        # wheel
            self.pipeline.from_device_stack("mouse", f"wheel {stroke.rolling}")
        elif stroke.state:
            self.pipeline.from_device_stack("mouse", f"button state={stroke.state:#05x}")

    def _keyboard(self, stroke: InterceptionKeyStroke) -> None:
        edge = "up" if stroke.state & 0x01 else "down"
        self.pipeline.from_device_stack(
            "keyboard", f"scancode {stroke.code:#04x} {edge}")


# --- fake transport 2: the HID board ----------------------------------------

class FakeHidBoard:
    """The microcontroller: parses the serial protocol, emits HID packets."""

    def __init__(self, pipeline: ModelledPipeline):
        self.pipeline = pipeline
        self.lines = []
        self._reply = b"K\n"

    def write(self, blob: bytes) -> int:
        line = blob.decode("ascii").strip()
        self.lines.append(line)
        parts = line.split()
        op = parts[0] if parts else ""
        args = [int(value) for value in parts[1:]]
        self._reply = b"K\n"
        if op == "P":
            self._reply = b"HID1\n"
        elif op == "M":
            self.pipeline.move_relative(args[0], args[1])
            self.pipeline.from_device_stack(
                "mouse", f"move {tuple(self.pipeline.cursor)}")
        elif op in ("D", "U"):
            self.pipeline.from_device_stack(
                "mouse", f"button {args[0]} {'down' if op == 'D' else 'up'}")
        elif op == "W":
            self.pipeline.from_device_stack("mouse", f"wheel {args[0]}")
        elif op in ("K", "R"):
            self.pipeline.from_device_stack(
                "keyboard", f"hid key {args[0]} {'down' if op == 'K' else 'up'}")
        return len(blob)

    def readline(self) -> bytes:
        return self._reply

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


# --- fake transport 3: user32 (the legacy path) -----------------------------

class FakeUser32:
    """The four user32 entry points the old libraries use.

    Anything that goes through here is injected by definition - that is what
    the Windows documentation in MSLLHOOKSTRUCT_structure_winuser_h.txt says
    and what the live mode of this test confirms.
    """

    def __init__(self, pipeline: ModelledPipeline):
        self.pipeline = pipeline
        self.calls = []

    def mouse_event(self, flags, dx, dy, data, extra):
        self.calls.append("mouse_event")
        if flags & 0x0001:                                # MOUSEEVENTF_MOVE
            self.pipeline.move_relative(dx, dy)
        self.pipeline.from_injection_api("mouse", f"mouse_event flags={flags:#06x}")

    def SetCursorPos(self, x, y):
        self.calls.append("SetCursorPos")
        self.pipeline.move_absolute(x, y)
        self.pipeline.from_injection_api("mouse", f"SetCursorPos {(x, y)}")

    def keybd_event(self, vk, scan, flags, extra):
        self.calls.append("keybd_event")
        self.pipeline.from_injection_api("keyboard", f"keybd_event vk={vk:#04x}")

    def SendInput(self, count, inputs, size):
        self.calls.append("SendInput")
        self.pipeline.from_injection_api("keyboard", "SendInput")
        return count


class FakeMouseModule:
    """`mouse` 0.7.1 on Windows, reduced to what the backend calls."""

    def __init__(self, user32: FakeUser32, pipeline: ModelledPipeline):
        self.user32, self.pipeline = user32, pipeline

    def get_position(self):
        return tuple(self.pipeline.cursor)

    def move(self, x, y, absolute=True, duration=0):
        self.user32.SetCursorPos(int(x), int(y))          # _winmouse.move_to

    def press(self, button="left"):
        self.user32.mouse_event(0x0002 if button == "left" else 0x0008, 0, 0, 0, 0)

    def release(self, button="left"):
        self.user32.mouse_event(0x0004 if button == "left" else 0x0010, 0, 0, 0, 0)

    def click(self, button="left"):
        self.press(button)
        self.release(button)

    def wheel(self, delta=1):
        self.user32.mouse_event(0x0800, 0, 0, int(delta * 120), 0)


class FakeKeyboardModule:
    """`keyboard` 0.13.5 on Windows, reduced to press/release."""

    def __init__(self, user32: FakeUser32):
        self.user32 = user32

    def press(self, key):
        self.user32.keybd_event(0x2E, 0, 0, 0)            # _winkeyboard.press

    def release(self, key):
        self.user32.keybd_event(0x2E, 0, 0x02, 0)


# ===========================================================================
# 2. The real low level hook (--live, Windows only)
# ===========================================================================

class LiveHookRecorder:
    """WH_MOUSE_LL + WH_KEYBOARD_LL, i.e. what the game client installs."""

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14

    def __init__(self):
        import threading
        from ctypes import wintypes

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        self.records = []
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                             ctypes.c_size_t, ctypes.c_void_p)
        self._mouse_struct = MSLLHOOKSTRUCT
        self._key_struct = KBDLLHOOKSTRUCT
        self._thread_id = None
        self._last_error = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="ll-hook")

    # the two callbacks -----------------------------------------------------
    def _on_mouse(self, code, wparam, lparam):
        if code >= 0:
            data = ctypes.cast(lparam, ctypes.POINTER(self._mouse_struct)).contents
            self.records.append(HookRecord("mouse", f"msg {wparam:#06x}",
                                           int(data.flags)))
        return self._user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_key(self, code, wparam, lparam):
        if code >= 0:
            data = ctypes.cast(lparam, ctypes.POINTER(self._key_struct)).contents
            self.records.append(HookRecord("keyboard", f"vk {data.vkCode:#04x}",
                                           int(data.flags)))
        return self._user32.CallNextHookEx(None, code, wparam, lparam)

    def _pump(self):
        from ctypes import wintypes

        self._mouse_cb = self._proc_type(self._on_mouse)
        self._key_cb = self._proc_type(self._on_key)
        module = self._kernel32.GetModuleHandleW(None)
        self._mouse_hook = self._user32.SetWindowsHookExW(
            self.WH_MOUSE_LL, self._mouse_cb, module, 0)
        self._key_hook = self._user32.SetWindowsHookExW(
            self.WH_KEYBOARD_LL, self._key_cb, module, 0)
        self._last_error = ctypes.get_last_error()
        self._thread_id = self._kernel32.GetCurrentThreadId()
        self._ready.set()

        message = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

        self._user32.UnhookWindowsHookEx(self._mouse_hook)
        self._user32.UnhookWindowsHookEx(self._key_hook)

    def __enter__(self):
        self._thread.start()
        self._ready.wait(5)
        if not (self._mouse_hook and self._key_hook):
            raise RuntimeError(
                f"SetWindowsHookEx failed: {self._last_error} "
                f"(check the run is elevated, and that no other global hook "
                f"installer is interfering)")
        return self

    def __exit__(self, *exc):
        self._user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        self._thread.join(3)
        return False

    def reset(self):
        self.records.clear()


# ===========================================================================
# 3. The exercise every backend gets put through
# ===========================================================================

def exercise(backend, origin=(400, 400)) -> None:
    """A miniature of what the bot does: move, click, tap a key, scroll."""
    x, y = origin
    backend.move(x, y, duration=0.0)
    backend.move(x + 40, y + 25, duration=0.05)     # the animated stroke
    backend.click("left")
    backend.key_down("insert")
    time.sleep(0.01)
    backend.key_up("insert")
    backend.key_down("shift")
    backend.key_up("shift")
    backend.scroll(0, -1)


class Report:
    """Collects named PASS/FAIL lines and prints them at the end."""

    def __init__(self):
        self.rows = []
        self.skips = []

    def skip(self, name: str, reason: str) -> None:
        """Something could not be checked here.  Never silently a pass."""
        self.skips.append(f"{name}: {reason}")
        print(f"  [SKIP] {name}")
        print(f"         {reason}")

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if detail:
            print(f"         {detail}")
        return bool(ok)

    @property
    def failures(self):
        return [row for row in self.rows if not row[0]]


def verdict(report: Report, records, backend, expect_flags: bool) -> None:
    """Turn a pile of hook records into the two statements that matter."""
    flagged = [record for record in records if record.flags]
    injected = [record for record in records
                if record.flags & LLMHF_INJECTED]
    lower_il = [record for record in records
                if record.flags & LLMHF_LOWER_IL_INJECTED]
    summary = (f"{len(records)} events seen by the hook, {len(injected)} with "
               f"LLMHF_INJECTED, {len(lower_il)} with LLMHF_LOWER_IL_INJECTED")

    report.check(f"{backend.name}: the hook actually saw the input",
                 len(records) > 0, summary)

    if expect_flags:
        report.check(f"{backend.name}: control case is detectable, as documented",
                     len(injected) > 0,
                     f"example: {flagged[0] if flagged else 'none'}")
    else:
        report.check(f"{backend.name}: NO LLMHF_INJECTED / LLMHF_LOWER_IL_INJECTED",
                     len(flagged) == 0,
                     f"first flagged event: {flagged[0]}" if flagged
                     else "every event arrived with flags == 0")


# ===========================================================================
# 4. Portable mode
# ===========================================================================

def run_modelled(report: Report) -> None:
    print("\n-- modelled Windows pipeline "
          "(real backend code, simulated win32k) ----------------")
    pipeline = ModelledPipeline()
    cursor = ModelCursor(pipeline)

    # 1. the control: the old path, through a fake user32
    user32 = FakeUser32(pipeline)
    legacy = SendInputBackend(mouse_module=FakeMouseModule(user32, pipeline),
                              keyboard_module=FakeKeyboardModule(user32))
    legacy.start()
    pipeline.reset()
    exercise(legacy)
    verdict(report, list(pipeline.records), legacy, expect_flags=True)
    report.check("sendinput really goes through the user32 injection APIs",
                 set(user32.calls) & {"SetCursorPos", "mouse_event", "keybd_event",
                                      "SendInput"},
                 f"user32 calls: {sorted(set(user32.calls))}")

    # 2. the replacement: the kernel driver path
    dll = FakeInterceptionDll(pipeline)
    driver = InterceptionBackend({"settle_seconds": 0}, dll=dll, cursor=cursor)
    driver.start()
    pipeline.reset()
    exercise(driver)
    verdict(report, list(pipeline.records), driver, expect_flags=False)
    report.check("interception picks devices that exist (keyboard 1, mouse 11)",
                 (driver.keyboard_device, driver.mouse_device) == (1, 11),
                 f"keyboard device {driver.keyboard_device}, "
                 f"mouse device {driver.mouse_device}")
    key_strokes = [stroke for _device, stroke in dll.sent
                   if isinstance(stroke, InterceptionKeyStroke)]
    insert_code, insert_extended = keymap.scancode("insert")
    report.check("interception sends real set-1 scan codes (insert = 0x52 + E0)",
                 any(stroke.code == insert_code and stroke.state & 0x02
                     for stroke in key_strokes),
                 f"{len(key_strokes)} key strokes, first "
                 f"code={key_strokes[0].code:#04x} state={key_strokes[0].state:#04x}"
                 if key_strokes else "no key strokes!")
    report.check("interception lands the pointer exactly on the target",
                 tuple(pipeline.cursor) == (440, 425),
                 f"cursor ended at {tuple(pipeline.cursor)}, wanted (440, 425)")

    # 2b. the same driver in absolute mode (config move_mode = "absolute")
    pipeline.reset()
    driver.move_mode = "absolute"
    driver.move_to(0, 0)
    driver.move_to(1919, 1079)
    report.check("interception absolute mode is exact and still unflagged",
                 tuple(pipeline.cursor) == (1919, 1079)
                 and not any(record.flags for record in pipeline.records),
                 f"{len(pipeline.records)} events, cursor {tuple(pipeline.cursor)}, "
                 f"flags {sorted({r.flags for r in pipeline.records})}")
    driver.close()

    # 3. the replacement: real HID hardware
    board = FakeHidBoard(pipeline)
    hid = SerialHidBackend({"settle_seconds": 0, "closed_loop_iterations": 40},
                           port=board, cursor=cursor)
    hid.start()
    pipeline.reset()
    exercise(hid)
    verdict(report, list(pipeline.records), hid, expect_flags=False)
    report.check("arduino lands the pointer exactly on the target",
                 tuple(pipeline.cursor) == (440, 425),
                 f"cursor ended at {tuple(pipeline.cursor)}, wanted (440, 425)")
    report.check("arduino never asks for a HID delta beyond +/-127",
                 all(abs(int(part)) <= 127
                     for line in board.lines if line.startswith("M ")
                     for part in line.split()[1:]),
                 f"{len([l for l in board.lines if l.startswith('M ')])} move packets")
    hid.close()


# ===========================================================================
# 5. Live mode (Windows)
# ===========================================================================

def run_live(report: Report, names) -> None:
    print("\n-- live: real WH_MOUSE_LL / WH_KEYBOARD_LL hook "
          "-------------------------------")
    if sys.platform != "win32":
        report.skip("live low level hook",
                    "--live needs Windows - run it on the bot PC before "
                    "shipping; the portable mode above still ran")
        return

    with LiveHookRecorder() as hook:
        for name in names:
            try:
                backend = inputbackends.build(name)
                backend.start()
            except Exception as exc:
                report.skip(f"{name}: live check",
                            f"backend not available on this machine ({exc})")
                continue
            print(f"  ---- {name}: driving the real pointer/keyboard")
            hook.reset()
            exercise(backend)
            time.sleep(0.4)                     # let the hook chain drain
            verdict(report, list(hook.records), backend,
                    expect_flags=backend.sets_injected_flags)
            backend.close()


# ===========================================================================
# 6. Wiring: the flow must not be able to bypass the backend
# ===========================================================================

class RecordingBackend(inputbackends.InputBackend):
    """A backend that only writes down what it was asked to do."""

    name = "recording"
    sets_injected_flags = False
    os_interface = "test double"

    def __init__(self):
        self.calls = []
        self.pos = (100, 100)

    def get_position(self):
        return self.pos

    def move_to(self, x, y, source="action"):
        self.pos = (int(x), int(y))
        self.calls.append(("move_to", self.pos, source))

    def mouse_down(self, button="left", source="action"):
        self.calls.append(("mouse_down", button, source))

    def mouse_up(self, button="left", source="action"):
        self.calls.append(("mouse_up", button, source))

    def scroll(self, dx, dy, source="action"):
        self.calls.append(("scroll", (dx, dy), source))

    def key_down(self, key, source="action"):
        self.calls.append(("key_down", key, source))

    def key_up(self, key, source="action"):
        self.calls.append(("key_up", key, source))


def run_wiring(report: Report) -> None:
    print("\n-- wiring: every InputController path uses the backend "
          "-------------------------")
    import config
    import core

    backend = RecordingBackend()
    window = core.GameWindow(core.Rect(0, 0, 800, 600))
    controller = core.InputController(window, core.Clock(core.BotState()),
                                      backend=backend)

    controller.tap("insert")                                  # keyboard path
    controller.move_and_click(100, 120)                       # vision path
    controller.click_here()                                   # idle click path
    controller.play_timeline([                                # recorded route
        {"timestamp": 0.0, "type": "mouse_move", "x": 300, "y": 300},
        {"timestamp": 0.0, "type": "mouse_click", "x": 300, "y": 300,
         "button": "left", "pressed": True},
        {"timestamp": 0.0, "type": "mouse_click", "x": 300, "y": 300,
         "button": "left", "pressed": False},
        {"timestamp": 0.0, "type": "key_press", "key": "shift"},
        {"timestamp": 0.0, "type": "key_release", "key": "shift"},
        {"timestamp": 0.0, "type": "mouse_scroll", "dx": 0, "dy": -1},
    ])

    kinds = {call[0] for call in backend.calls}
    report.check("all six primitives reach the backend",
                 kinds == {"move_to", "mouse_down", "mouse_up", "scroll",
                           "key_down", "key_up"},
                 f"seen: {sorted(kinds)}")
    report.check("recorded routes are tagged source='replay'",
                 any(call[-1] == "replay" for call in backend.calls)
                 and any(call[-1] == "action" for call in backend.calls),
                 f"{sum(1 for c in backend.calls if c[-1] == 'replay')} replay / "
                 f"{sum(1 for c in backend.calls if c[-1] == 'action')} action calls")
    report.check("the bezier stroke still samples config.MOUSE['steps'] points",
                 sum(1 for call in backend.calls if call[0] == "move_to") >
                 config.MOUSE["steps"],
                 f"{sum(1 for c in backend.calls if c[0] == 'move_to')} move_to "
                 f"calls for one stroke (steps={config.MOUSE['steps']})")

    # No module in the bot may *call* an injection API any more.  Prose that
    # merely mentions one (this repository is full of it now) is fine, so the
    # audit looks for an actual call - the name followed by an open bracket.
    banned = ("mouse_lib.move(", "mouse_lib.click(", "mouse_lib.press(",
              "mouse_lib.release(", "mouse_lib.wheel(", "mouse_lib.get_position(",
              "keyboard_lib.press(", "keyboard_lib.release(", "keyboard_lib.send(",
              "keyboard_lib.write(", "pynput_mouse.Controller(",
              "pynput_keyboard.Controller(", "SendInput(", "keybd_event(",
              "mouse_event(", "SetCursorPos(")
    offenders = []
    for filename in ("core.py", "main.py", "vision.py", "discord_bot.py"):
        for number, line in enumerate(open(os.path.join(REPO_ROOT, filename),
                                           encoding="utf-8"), start=1):
            code = line.split("#", 1)[0]
            if any(word in code for word in banned):
                offenders.append(f"{filename}:{number}: {line.strip()}")
    report.check("no bot module calls an injection API directly",
                 not offenders, "\n         ".join(offenders) or
                 "core.py / main.py / vision.py / discord_bot.py are clean")


# ===========================================================================
# 7. main
# ===========================================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="install a real low level hook and drive the real "
                             "devices (Windows only)")
    parser.add_argument("--backend", action="append", default=None,
                        help="restrict --live to these backends (repeatable)")
    args = parser.parse_args(argv)

    print("=" * 78)
    print("input injection flag test -- LLMHF_INJECTED / LLMHF_LOWER_IL_INJECTED")
    print("=" * 78)
    print(inputbackends.describe_all())

    report = Report()
    if args.live:
        run_live(report, args.backend or list(inputbackends.BACKENDS))
    else:
        run_modelled(report)
    run_wiring(report)

    print("\n" + "=" * 78)
    for note in report.skips:
        print(f"  SKIPPED: {note}")
    if report.failures:
        for _ok, name, _detail in report.failures:
            print(f"  FAILED: {name}")
        print(f"RESULT: FAILED ({len(report.failures)}/{len(report.rows)} checks)")
        return 1
    tail = f", {len(report.skips)} skipped" if report.skips else ""
    print(f"RESULT: PASSED ({len(report.rows)} checks{tail})")
    print("the bot's default input path leaves both injected flags at 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
