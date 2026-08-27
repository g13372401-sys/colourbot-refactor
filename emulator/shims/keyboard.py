"""
keyboard -- drop-in replacement for the `keyboard` PyPI package.
===============================================================

The bot uses four things from it:

    press(key) / release(key)   every in-game hotkey ('2', '4', 'insert', '`',
                                'shift' held around a shift-drop, ...)
    wait(key)                   the ESC panic key thread, which blocks forever
    (module presence)           core.keyboard_lib is None -> features disabled

`tap()` in core.py does press -> clock.wait(hold) -> release, so the hold time
is real and the emulator sees a key that is genuinely held down for as long as
the config says.
"""

from __future__ import annotations

import threading

import _emu_client


def press(key):
    """ Presses (and holds) the given key. """
    _emu_client.request("key", key=str(key), action="press")


def release(key):
    """ Releases the given key. """
    _emu_client.request("key", key=str(key), action="release")


def send(key, do_press=True, do_release=True):
    """ Presses and releases the given key. """
    if do_press:
        press(key)
    if do_release:
        release(key)


press_and_release = send


def write(text, delay=0):
    for char in text:
        send(char)


def is_pressed(key):
    reply = _emu_client.request("key_state", key=str(key))
    return bool(reply.get("pressed"))


def wait(key=None):
    """Block until `key` is pressed.

    The real library parks the calling thread on a hotkey callback and never
    comes back on its own; the emulator does the same by holding the request
    open until that key is actually pressed on the virtual desktop.  If the
    emulator disappears first (test finished) we park forever rather than
    returning, because the caller's next line is `os._exit(0)`.
    """
    try:
        _emu_client.request("wait_key", key=str(key) if key else None)
    except Exception:
        threading.Event().wait()


def add_hotkey(key, callback, *args, **kwargs):        # pragma: no cover
    def _runner():
        wait(key)
        callback()

    thread = threading.Thread(target=_runner, name=f"hotkey-{key}", daemon=True)
    thread.start()
    return thread
