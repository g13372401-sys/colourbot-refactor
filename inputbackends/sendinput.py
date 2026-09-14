"""
inputbackends.sendinput -- THE OLD, DETECTABLE PATH.  Kept on purpose.
======================================================================

This is exactly what `core.InputController` used to do inline: the `mouse`,
`keyboard` and `pynput` PyPI packages.  On Windows all three end in a user-mode
injection API:

    mouse 0.7.1     mouse/_winmouse.py  -> user32.mouse_event(...)      (line 188)
                                        -> user32.SetCursorPos(...)     (line 200)
    keyboard 0.13.5 keyboard/_winkeyboard.py -> user32.keybd_event(...) (line 580)
                                             -> user32.SendInput(...)   (line 613)
    pynput 1.7.6+   pynput/_util/win32.py    -> windll.user32.SendInput (line 127)

`mouse_event` and `keybd_event` are documented as superseded by `SendInput` and
are implemented on top of the same win32k path.  Every event that arrives that
way is marked by win32k as injected, so a WH_MOUSE_LL / WH_KEYBOARD_LL hook in
the game client reads:

    MSLLHOOKSTRUCT.flags  & LLMHF_INJECTED            == 1     -> "emulated"
    MSLLHOOKSTRUCT.flags  & LLMHF_LOWER_IL_INJECTED   == 1     -> "...and from a
                                                                  lower IL process"

(The second bit appears whenever the bot runs at a lower integrity level than
the process whose hook is looking - e.g. bot at Medium IL, client started
elevated.  Running the bot as administrator removes the *second* bit only; the
first one is unavoidable on this path.)

Why keep it at all?

1. it is the **control case** in `test_input_flags.py`: the test proves the
   hook works by watching this backend light the flags up, then proves the new
   backend does not;
2. it is the fallback for a machine with neither the driver nor the board (and
   for the emulator test, which runs on a virtual desktop where the three
   libraries are shimmed) - the flow keeps working, just detectably, and the
   log says so in capitals;
3. it documents the previous behaviour precisely, which is what the anti-cheat
   team is being handed.
"""

from __future__ import annotations

import importlib
import logging
from typing import Optional, Tuple

from .base import BackendUnavailable, InputBackend, normalise_button
from . import keymap

LOG = logging.getLogger("colourbot.input.sendinput")


def _optional_import(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:                                  # pragma: no cover
        LOG.debug("optional module %s unavailable: %s", name, exc)
        return None


class SendInputBackend(InputBackend):
    """The legacy `mouse` + `keyboard` + `pynput` transport."""

    name = "sendinput"
    sets_injected_flags = True
    os_interface = ("mouse/keyboard/pynput -> user32.SendInput, mouse_event, "
                    "keybd_event, SetCursorPos")
    needs_hardware = False

    def __init__(self, settings: Optional[dict] = None, mouse_module=None,
                 keyboard_module=None, pynput_mouse=None, pynput_keyboard=None):
        self.settings = dict(settings or {})
        # All four are injectable so the flag test can drive this backend
        # against a simulated user32 on any platform.
        self.mouse = mouse_module if mouse_module is not None else _optional_import("mouse")
        self.keyboard = (keyboard_module if keyboard_module is not None
                         else _optional_import("keyboard"))
        self.pynput_mouse = (pynput_mouse if pynput_mouse is not None
                             else _optional_import("pynput.mouse"))
        self.pynput_keyboard = (pynput_keyboard if pynput_keyboard is not None
                                else _optional_import("pynput.keyboard"))
        self._mouse_ctrl = None
        self._keyboard_ctrl = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.mouse is None or self.keyboard is None:
            raise BackendUnavailable(
                "the legacy backend needs the `mouse` and `keyboard` packages "
                "- run `pip install -r requirements.txt`.")
        LOG.warning("USING THE LEGACY SendInput BACKEND: every event carries "
                    "LLMHF_INJECTED and is trivially detectable by a low level "
                    "hook.  See INPUT_INJECTION.md.")

    def close(self) -> None:
        self._mouse_ctrl = None
        self._keyboard_ctrl = None

    # -- pynput controllers, created lazily exactly like the old code ------
    def _controllers(self):
        if self.pynput_mouse is None or self.pynput_keyboard is None:
            raise BackendUnavailable(
                "pynput is required to replay a recorded route with the legacy "
                "backend - run `pip install -r requirements.txt`.")
        if self._mouse_ctrl is None:
            self._mouse_ctrl = self.pynput_mouse.Controller()
            self._keyboard_ctrl = self.pynput_keyboard.Controller()
        return self._mouse_ctrl, self._keyboard_ctrl

    # -- mouse -------------------------------------------------------------
    def get_position(self) -> Tuple[int, int]:
        return self.mouse.get_position()

    def move(self, x: int, y: int, duration: float = 0.0,
             source: str = "action") -> None:
        """Delegate to `mouse.move`, which is what produced the old timing."""
        if source == "replay":
            self.move_to(x, y, source)
            return
        self.mouse.move(int(x), int(y), absolute=True, duration=duration)

    def move_to(self, x: int, y: int, source: str = "action") -> None:
        if source == "replay":
            mouse_ctrl, _ = self._controllers()
            mouse_ctrl.position = (int(x), int(y))
        else:
            self.mouse.move(int(x), int(y), absolute=True, duration=0)

    def mouse_down(self, button: str = "left", source: str = "action") -> None:
        name = normalise_button(button)
        if source == "replay":
            mouse_ctrl, _ = self._controllers()
            mouse_ctrl.press(getattr(self.pynput_mouse.Button, name))
        else:
            self.mouse.press(name)

    def mouse_up(self, button: str = "left", source: str = "action") -> None:
        name = normalise_button(button)
        if source == "replay":
            mouse_ctrl, _ = self._controllers()
            mouse_ctrl.release(getattr(self.pynput_mouse.Button, name))
        else:
            self.mouse.release(name)

    def click(self, button: str = "left", source: str = "action") -> None:
        if source == "replay":
            super().click(button, source)
        else:
            # `mouse.click()` with no arguments: the exact call the bot made.
            self.mouse.click(normalise_button(button))

    def scroll(self, dx: int, dy: int, source: str = "action") -> None:
        if source == "replay":
            mouse_ctrl, _ = self._controllers()
            mouse_ctrl.scroll(dx, dy)
        else:
            self.mouse.wheel(dy)

    # -- keyboard ----------------------------------------------------------
    def key_down(self, key: str, source: str = "action") -> None:
        if source == "replay":
            _, keyboard_ctrl = self._controllers()
            keyboard_ctrl.press(keymap.to_pynput(key, self.pynput_keyboard))
        else:
            self.keyboard.press(keymap.to_keyboard_lib(key))

    def key_up(self, key: str, source: str = "action") -> None:
        if source == "replay":
            _, keyboard_ctrl = self._controllers()
            keyboard_ctrl.release(keymap.to_pynput(key, self.pynput_keyboard))
        else:
            self.keyboard.release(keymap.to_keyboard_lib(key))
