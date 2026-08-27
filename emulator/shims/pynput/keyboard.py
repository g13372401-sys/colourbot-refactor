"""pynput.keyboard -- Controller/Key/Listener against the virtual desktop."""

from __future__ import annotations

import threading

import _emu_client

# Named keys the recorded routes (and the bot) can refer to.  `play_timeline`
# does `getattr(Key, name, name)` for every multi-character key name, so a name
# that is missing here would silently be sent as a raw string - hence the
# generous list.
_KEY_NAMES = (
    "alt alt_l alt_r alt_gr backspace caps_lock cmd cmd_l cmd_r ctrl ctrl_l "
    "ctrl_r delete down end enter esc f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12 "
    "home insert left menu num_lock page_down page_up pause print_screen right "
    "scroll_lock shift shift_l shift_r space tab up"
).split()


class Key:
    """`Key.shift`, `Key.insert`, ...  Comparable and hashable like the real one."""

    def __init__(self, name: str):
        self.name = name
        self.char = None
        self.value = self

    def __repr__(self) -> str:
        return f"Key.{self.name}"

    def __eq__(self, other) -> bool:
        return isinstance(other, Key) and other.name == self.name

    def __hash__(self) -> int:
        return hash(("key", self.name))


for _name in _KEY_NAMES:
    setattr(Key, _name, Key(_name))


class KeyCode:
    """A printable key.  `Key.from_char('a')` in the real package."""

    def __init__(self, char=None, vk=None):
        self.char = char
        self.vk = vk
        self.name = char

    @classmethod
    def from_char(cls, char):
        return cls(char=char)

    def __repr__(self) -> str:
        return f"KeyCode(char={self.char!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, KeyCode) and other.char == self.char

    def __hash__(self) -> int:
        return hash(("keycode", self.char))


def _key_name(key) -> str:
    if isinstance(key, str):
        return key
    return getattr(key, "char", None) or getattr(key, "name", str(key))


class Controller:
    """Key press/release.  Accepts strings, Key and KeyCode, like the real one."""

    def press(self, key):
        _emu_client.request("key", key=_key_name(key), action="press",
                            source="replay")

    def release(self, key):
        _emu_client.request("key", key=_key_name(key), action="release",
                            source="replay")

    def tap(self, key):
        self.press(key)
        self.release(key)

    def type(self, text):
        for char in text:
            self.tap(char)


class Listener(threading.Thread):
    """Capture side; see pynput.mouse.Listener."""

    def __init__(self, on_press=None, on_release=None, **kwargs):
        super().__init__(name="pynput-keyboard-listener", daemon=True)
        self.on_press = on_press
        self.on_release = on_release
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
