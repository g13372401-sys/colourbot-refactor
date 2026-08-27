"""
pynput -- drop-in replacement for the `pynput` package.
======================================================

Used by `core.InputController.play_timeline()` to replay the recorded route
timelines (routes/route1_leg1.json is 5298 events over 45 seconds) and by
`main.py --record` to capture new ones.

The real package refuses to even import without an X display, which is also why
the shim has to be a *package* rather than a monkeypatch: the bot does
`importlib.import_module("pynput.mouse")` at import time and treats a failure as
"no route playback available".
"""

from __future__ import annotations

from . import keyboard, mouse                                    # noqa: F401

__all__ = ["mouse", "keyboard"]
