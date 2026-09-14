"""
inputbackends.winapi -- the *read only* pieces of user32 the backends need.
===========================================================================

Nothing in this file injects anything.  `GetCursorPos` and `GetSystemMetrics`
only ask Windows questions; they generate no input events and therefore cannot
set LLMHF_INJECTED.  They are isolated here so that:

  * the hardware backends have one obvious place to get "where is the pointer
    right now" from (they need it to steer a *relative* device onto an absolute
    target - see `interception.py`), and
  * the flag test can substitute a fake cursor and run the real backend code on
    a machine that is not Windows.
"""

from __future__ import annotations

import sys
from typing import Tuple


def is_windows() -> bool:
    return sys.platform == "win32"


class CursorApi:
    """Where the pointer is and how big the desktop is."""

    def get_position(self) -> Tuple[int, int]:
        raise NotImplementedError

    def virtual_screen(self) -> Tuple[int, int, int, int]:
        """(left, top, width, height) of the whole virtual desktop."""
        raise NotImplementedError


class WindowsCursor(CursorApi):
    """user32.GetCursorPos / GetSystemMetrics - queries only, no injection."""

    # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
    _METRICS = (76, 77, 78, 79)

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._point = wintypes.POINT
        # Exactly the same DPI call `core._screen_size()` already makes, so the
        # whole process stays in ONE coordinate space: GetCursorPos here and
        # GetWindowRect there must agree, or the bot would aim next to the
        # canvas on a scaled display.  It is a no-op when core got there first
        # (the awareness can only be set once), which is the normal case.
        try:
            self._user32.SetProcessDPIAware()
        except Exception:                                    # pragma: no cover
            pass

    def get_position(self) -> Tuple[int, int]:
        point = self._point()
        self._user32.GetCursorPos(self._ctypes.byref(point))
        return int(point.x), int(point.y)

    def virtual_screen(self) -> Tuple[int, int, int, int]:
        left, top, width, height = (self._user32.GetSystemMetrics(m)
                                    for m in self._METRICS)
        return int(left), int(top), int(width or 1), int(height or 1)
