"""
mouse -- drop-in replacement for the `mouse` PyPI package.
=========================================================

`core.InputController.move_and_click()` is a port of the original bot's
human-like stroke: 51 bezier samples, each one handed to `mouse.move(...,
duration=d)`.  The real library turns that duration into 120 interpolated
`move_to` calls per second and sleeps between them, which is *the* reason a
stroke takes ~1.5 s instead of being instant.

So this shim does not "stub out" the movement - `move()` below is the real
library's implementation with the single OS call swapped for a socket write.
Same recursion, same `steps = max(1.0, float(int(duration * 120.0)))`, same
`time.sleep(duration / steps)`.  The emulator therefore sees the same thousands
of intermediate positions the X server would see, the cursor visibly glides,
and every delay the script asks for is really slept.

Only the handful of names the bot touches are implemented; anything else raises
AttributeError rather than silently doing nothing.
"""

from __future__ import annotations

import time as _time

import _emu_client

LEFT = "left"
RIGHT = "right"
MIDDLE = "middle"
X = "x"
X2 = "x2"

UP = "up"
DOWN = "down"
DOUBLE = "double"


# ---------------------------------------------------------------------------
# the "os mouse" - one socket round trip per event
# ---------------------------------------------------------------------------

class _OsMouse:
    @staticmethod
    def move_to(x, y):
        _emu_client.request("mouse_move", x=int(x), y=int(y))

    @staticmethod
    def get_position():
        reply = _emu_client.request("mouse_pos")
        return int(reply["x"]), int(reply["y"])

    @staticmethod
    def press(button=LEFT):
        _emu_client.request("mouse_button", button=button, action="press")

    @staticmethod
    def release(button=LEFT):
        _emu_client.request("mouse_button", button=button, action="release")

    @staticmethod
    def wheel(delta=1):
        _emu_client.request("mouse_scroll", dx=0, dy=int(delta))


_os_mouse = _OsMouse()


# ---------------------------------------------------------------------------
# public api (verbatim from mouse/__init__.py where it matters)
# ---------------------------------------------------------------------------

def get_position():
    """ Returns the (x, y) mouse position. """
    return _os_mouse.get_position()


def move(x, y, absolute=True, duration=0):
    """
    Moves the mouse. If `absolute`, to position (x, y), otherwise move relative
    to the current position. If `duration` is non-zero, animates the movement.
    """
    x = int(x)
    y = int(y)

    position_x, position_y = get_position()

    if not absolute:
        x = position_x + x
        y = position_y + y

    if duration:
        start_x = position_x
        start_y = position_y
        dx = x - start_x
        dy = y - start_y

        if dx == 0 and dy == 0:
            _time.sleep(duration)
        else:
            # 120 movements per second.
            steps = max(1.0, float(int(duration * 120.0)))
            for i in range(int(steps) + 1):
                move(start_x + dx * i / steps, start_y + dy * i / steps)
                _time.sleep(duration / steps)
    else:
        _os_mouse.move_to(x, y)


def press(button=LEFT):
    """ Presses the given button (but doesn't release). """
    _os_mouse.press(button)


def release(button=LEFT):
    """ Releases the given button. """
    _os_mouse.release(button)


def click(button=LEFT):
    """ Sends a click with the given button. """
    _os_mouse.press(button)
    _os_mouse.release(button)


def double_click(button=LEFT):
    """ Sends a double click with the given button. """
    click(button)
    click(button)


def right_click():
    """ Sends a right click with the given button. """
    click(RIGHT)


def wheel(delta=1):
    """ Scrolls the wheel `delta` clicks. Sign indicates direction. """
    _os_mouse.wheel(delta)


def drag(start_x, start_y, end_x, end_y, absolute=True, duration=0):
    """ Holds left mouse button, moving from start to end position, then releases. """
    if is_pressed():
        release()
    move(start_x, start_y, absolute, 0)
    press()
    move(end_x, end_y, absolute, duration)
    release()


_pressed = set()


def is_pressed(button=LEFT):
    """ Returns True if the given button is currently pressed. """
    return button in _pressed
