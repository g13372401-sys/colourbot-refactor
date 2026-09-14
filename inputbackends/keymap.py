"""
inputbackends.keymap -- one key name table for every transport.
===============================================================

The bot names keys in three different dialects and always has:

    config.py / main.py     "insert", "shift", "2", "`"        (`keyboard` style)
    recorded routes .json   "shift", "ctrl_l", "page_up", "k"  (`pynput` style)
    the hardware backends   scan code 0x52+E0 / HID usage 73   (what the wire wants)

`canonical()` folds the first two dialects into one lower-case name, and the
tables below translate that canonical name into whatever the chosen backend
needs to put on the wire:

    SCANCODES   PS/2 "set 1" make codes, plus the 0xE0 extended prefix flag.
                This is what a real keyboard puts on the wire and what the
                Interception driver expects in InterceptionKeyStroke.code.
    HID_USAGES  USB HID keyboard usage IDs (HID Usage Table, page 0x07).
    ARDUINO     the codes the Arduino `Keyboard` library uses, so the firmware
                sketch can stay four lines long.
    KEYBOARD_LIB / PYNPUT   the spellings the legacy backend needs.

Only keys a bot could plausibly press are listed; anything unknown raises
`KeyError` loudly rather than silently pressing nothing.
"""

from __future__ import annotations

from typing import Dict, Tuple

# ---------------------------------------------------------------------------
# 1. canonical names
# ---------------------------------------------------------------------------
# canonical == the pynput spelling in lower case ("page_up", "ctrl_l", "esc"),
# because that is what the recorded routes already contain.

_ALIASES = {
    # keyboard-lib spelling            -> canonical
    "escape": "esc",
    "return": "enter",
    "page up": "page_up",
    "page down": "page_down",
    "pageup": "page_up",
    "pagedown": "page_down",
    "caps lock": "caps_lock",
    "capslock": "caps_lock",
    "num lock": "num_lock",
    "numlock": "num_lock",
    "scroll lock": "scroll_lock",
    "scrolllock": "scroll_lock",
    "print screen": "print_screen",
    "printscreen": "print_screen",
    "prt sc": "print_screen",
    "ins": "insert",
    "del": "delete",
    "control": "ctrl",
    "windows": "cmd",
    "win": "cmd",
    "super": "cmd",
    "left shift": "shift",
    "right shift": "shift_r",
    "left ctrl": "ctrl",
    "right ctrl": "ctrl_r",
    "left alt": "alt",
    "right alt": "alt_gr",
    "alt gr": "alt_gr",
    "shift_l": "shift",
    "ctrl_l": "ctrl",
    "alt_l": "alt",
    "cmd_l": "cmd",
    "spacebar": "space",
    "backspace": "backspace",
    "back space": "backspace",
}


def canonical(key) -> str:
    """'Insert' / 'page up' / 'Key.page_up' / 'a' -> canonical name.

    Accepts the pynput object spellings too (`Key.shift`, `KeyCode(char='a')`)
    so `play_timeline` can hand recorded names straight through.

    Single characters keep their case ('A' stays 'A'), because the case is the
    only thing that says whether shift has to be held; the lookup tables below
    fold it away again.  Named keys are lower-cased and de-aliased.
    """
    name = getattr(key, "name", None) or getattr(key, "char", None) or key
    name = str(name).strip()
    if name.startswith("Key."):
        name = name[4:]
    if len(name) == 1:                          # printable character
        return name
    lowered = name.lower()
    return _ALIASES.get(lowered, lowered)


# ---------------------------------------------------------------------------
# 2. PS/2 set-1 scan codes  (Interception, and any other device-stack driver)
# ---------------------------------------------------------------------------
# value = (make code, extended?)  - extended keys are prefixed with 0xE0 on a
# real keyboard and carry INTERCEPTION_KEY_E0 in the stroke state.

SCANCODES: Dict[str, Tuple[int, bool]] = {
    "esc": (0x01, False),
    "1": (0x02, False), "2": (0x03, False), "3": (0x04, False),
    "4": (0x05, False), "5": (0x06, False), "6": (0x07, False),
    "7": (0x08, False), "8": (0x09, False), "9": (0x0A, False),
    "0": (0x0B, False),
    "-": (0x0C, False), "=": (0x0D, False),
    "backspace": (0x0E, False), "tab": (0x0F, False),
    "q": (0x10, False), "w": (0x11, False), "e": (0x12, False),
    "r": (0x13, False), "t": (0x14, False), "y": (0x15, False),
    "u": (0x16, False), "i": (0x17, False), "o": (0x18, False),
    "p": (0x19, False), "[": (0x1A, False), "]": (0x1B, False),
    "enter": (0x1C, False), "ctrl": (0x1D, False),
    "a": (0x1E, False), "s": (0x1F, False), "d": (0x20, False),
    "f": (0x21, False), "g": (0x22, False), "h": (0x23, False),
    "j": (0x24, False), "k": (0x25, False), "l": (0x26, False),
    ";": (0x27, False), "'": (0x28, False), "`": (0x29, False),
    "shift": (0x2A, False), "\\": (0x2B, False),
    "z": (0x2C, False), "x": (0x2D, False), "c": (0x2E, False),
    "v": (0x2F, False), "b": (0x30, False), "n": (0x31, False),
    "m": (0x32, False), ",": (0x33, False), ".": (0x34, False),
    "/": (0x35, False), "shift_r": (0x36, False),
    "alt": (0x38, False), "space": (0x39, False), "caps_lock": (0x3A, False),
    "f1": (0x3B, False), "f2": (0x3C, False), "f3": (0x3D, False),
    "f4": (0x3E, False), "f5": (0x3F, False), "f6": (0x40, False),
    "f7": (0x41, False), "f8": (0x42, False), "f9": (0x43, False),
    "f10": (0x44, False), "num_lock": (0x45, False), "scroll_lock": (0x46, False),
    "f11": (0x57, False), "f12": (0x58, False),
    # extended (0xE0-prefixed) block
    "ctrl_r": (0x1D, True), "alt_gr": (0x38, True),
    "home": (0x47, True), "up": (0x48, True), "page_up": (0x49, True),
    "left": (0x4B, True), "right": (0x4D, True), "end": (0x4F, True),
    "down": (0x50, True), "page_down": (0x51, True),
    "insert": (0x52, True), "delete": (0x53, True),
    "cmd": (0x5B, True), "cmd_r": (0x5C, True), "menu": (0x5D, True),
    "print_screen": (0x37, True),
}

# Characters that need shift on a US layout: the bot never types them today,
# but a future route might, and silently sending the unshifted key would be a
# nasty bug to chase.
SHIFTED_CHARS = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    ":": ";", '"': "'", "~": "`", "|": "\\", "<": ",", ">": ".", "?": "/",
}

# ---------------------------------------------------------------------------
# 3. USB HID usage ids (page 0x07) -- for HID-report style firmware
# ---------------------------------------------------------------------------

HID_USAGES: Dict[str, int] = {
    **{chr(ord("a") + i): 4 + i for i in range(26)},
    **{str(d): 29 + d for d in range(1, 10)}, "0": 39,
    "enter": 40, "esc": 41, "backspace": 42, "tab": 43, "space": 44,
    "-": 45, "=": 46, "[": 47, "]": 48, "\\": 49, ";": 51, "'": 52,
    "`": 53, ",": 54, ".": 55, "/": 56, "caps_lock": 57,
    **{f"f{i}": 57 + i for i in range(1, 13)},
    "print_screen": 70, "scroll_lock": 71, "pause": 72, "insert": 73,
    "home": 74, "page_up": 75, "delete": 76, "end": 77, "page_down": 78,
    "right": 79, "left": 80, "down": 81, "up": 82, "num_lock": 83,
    "ctrl": 0xE0, "shift": 0xE1, "alt": 0xE2, "cmd": 0xE3,
    "ctrl_r": 0xE4, "shift_r": 0xE5, "alt_gr": 0xE6, "cmd_r": 0xE7,
}

# ---------------------------------------------------------------------------
# 4. Arduino `Keyboard` library codes -- what the HID relay firmware speaks
# ---------------------------------------------------------------------------
# Printable characters are sent as their ASCII value; everything else uses the
# KEY_* constants from Keyboard.h.

ARDUINO_KEYS: Dict[str, int] = {
    "ctrl": 0x80, "shift": 0x81, "alt": 0x82, "cmd": 0x83,
    "ctrl_r": 0x84, "shift_r": 0x85, "alt_gr": 0x86, "cmd_r": 0x87,
    "up": 0xDA, "down": 0xD9, "left": 0xD8, "right": 0xD7,
    "backspace": 0xB2, "tab": 0xB3, "enter": 0xB0, "esc": 0xB1,
    "insert": 0xD1, "delete": 0xD4, "page_up": 0xD3, "page_down": 0xD6,
    "home": 0xD2, "end": 0xD5, "caps_lock": 0xC1,
    **{f"f{i}": 0xC1 + i for i in range(1, 13)},          # KEY_F1 = 0xC2
    "print_screen": 0xCE, "scroll_lock": 0xCF, "pause": 0xD0,
    "num_lock": 0xDB, "menu": 0xED,
}

# ---------------------------------------------------------------------------
# 5. Back to the two python libraries (legacy backend only)
# ---------------------------------------------------------------------------

_TO_KEYBOARD_LIB = {
    "page_up": "page up", "page_down": "page down", "caps_lock": "caps lock",
    "num_lock": "num lock", "scroll_lock": "scroll lock",
    "print_screen": "print screen", "shift_r": "right shift",
    "ctrl_r": "right ctrl", "alt_gr": "right alt", "cmd": "windows",
    "cmd_r": "right windows",
}


def to_keyboard_lib(key: str) -> str:
    """canonical -> the spelling the `keyboard` PyPI package understands."""
    name = canonical(key)
    return _TO_KEYBOARD_LIB.get(name, name)


def to_pynput(key: str, pynput_keyboard):
    """canonical -> a pynput `Key`/character, exactly as play_timeline did."""
    name = canonical(key)
    if len(name) == 1:
        return name
    return getattr(pynput_keyboard.Key, name, name)


def _unshifted(name: str) -> str:
    """'A' -> 'a', '!' -> '1': the key you actually have to press."""
    if len(name) == 1:
        return SHIFTED_CHARS.get(name, name.lower())
    return name


def scancode(key: str) -> Tuple[int, bool]:
    """canonical -> (set-1 make code, extended flag).  KeyError if unknown."""
    name = _unshifted(canonical(key))
    if name in SCANCODES:
        return SCANCODES[name]
    raise KeyError(f"no scan code for key {key!r}")


def needs_shift(key: str) -> bool:
    """True when the key can only be typed with shift held ('A', '!', ...)."""
    name = canonical(key)
    return len(name) == 1 and (name in SHIFTED_CHARS or name.isupper())


def arduino_code(key: str) -> int:
    """canonical -> the int the HID relay firmware expects.

    The firmware presses shift itself for anything the `Keyboard` library knows
    as a shifted ASCII character, so printable keys are sent verbatim.
    """
    name = canonical(key)
    if name in ARDUINO_KEYS:
        return ARDUINO_KEYS[name]
    if len(name) == 1:
        return ord(name)
    raise KeyError(f"no arduino key code for key {key!r}")


def hid_usage(key: str) -> int:
    """canonical -> USB HID usage id.  KeyError if unknown."""
    name = _unshifted(canonical(key))
    if name in HID_USAGES:
        return HID_USAGES[name]
    raise KeyError(f"no HID usage for key {key!r}")
