"""pynput.mouse -- Controller/Button/Listener against the virtual desktop."""

from __future__ import annotations

import threading

import _emu_client


class Button:
    """`Button.left` etc.  `.name` is what the recorder stored in the routes."""

    def __init__(self, name: str, value: int):
        self.name = name
        self.value = value

    def __repr__(self) -> str:
        return f"Button.{self.name}"

    def __eq__(self, other) -> bool:
        return isinstance(other, Button) and other.name == self.name

    def __hash__(self) -> int:
        return hash(("button", self.name))


Button.unknown = Button("unknown", 0)
Button.left = Button("left", 1)
Button.middle = Button("middle", 2)
Button.right = Button("right", 3)


class Controller:
    """Absolute pointer control.

    `play_timeline` assigns `controller.position = (x, y)` a few thousand times
    per leg - one socket write each, exactly like the real backend does one
    XTestFakeMotionEvent each.
    """

    @property
    def position(self):
        reply = _emu_client.request("mouse_pos")
        return int(reply["x"]), int(reply["y"])

    @position.setter
    def position(self, point):
        x, y = point
        _emu_client.request("mouse_move", x=int(x), y=int(y), source="replay")

    def move(self, dx, dy):
        x, y = self.position
        self.position = (x + dx, y + dy)

    def press(self, button=Button.left):
        _emu_client.request("mouse_button", button=button.name, action="press",
                            source="replay")

    def release(self, button=Button.left):
        _emu_client.request("mouse_button", button=button.name, action="release",
                            source="replay")

    def click(self, button=Button.left, count=1):
        for _ in range(count):
            self.press(button)
            self.release(button)

    def scroll(self, dx, dy):
        _emu_client.request("mouse_scroll", dx=int(dx), dy=int(dy),
                            source="replay")


class Listener(threading.Thread):
    """Input capture.  Only `main.py --record` uses it; the emulator test does
    not record, so this is a well behaved no-op that can be started, stopped and
    used as a context manager."""

    def __init__(self, on_move=None, on_click=None, on_scroll=None, **kwargs):
        super().__init__(name="pynput-mouse-listener", daemon=True)
        self.on_move = on_move
        self.on_click = on_click
        self.on_scroll = on_scroll
        self._stop_event = threading.Event()

    def run(self):
        self._stop_event.wait()

    def stop(self):
        self._stop_event.set()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False
