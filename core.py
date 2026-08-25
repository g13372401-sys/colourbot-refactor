"""
core.py -- the plumbing every other module stands on.
=====================================================

Five independent bits live in here, in this order:

    1. logging + control-flow exceptions
    2. RuntimeTimer  - the persistent "how long have we been alive" stopwatch
    3. Clock         - the ONE place the program is allowed to sleep
    4. AutomationState - flags/counters shared between the worker threads and
                         the Discord bot (these were global variables before)
    5. Geometry + InputController - game-window aware coordinates, human-like
                         mouse movement, key taps and recorded-timeline playback

Nothing in here knows anything about the game logic; main.py wires it together.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import config

LOG = logging.getLogger("colourbot.core")

# Everything the bot writes (log, runtime stopwatch) lands next to the code, not
# in whatever directory the operator happened to be in.
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def package_path(path: str) -> str:
    """Absolute path for a file that belongs next to the sources."""
    return path if os.path.isabs(path) else os.path.join(PACKAGE_DIR, path)


# ===========================================================================
# 1. Logging and control-flow exceptions
# ===========================================================================

def setup_logging(level: str = None, log_file: Optional[str] = "") -> None:
    """Console + optional file logging.  Call once, from main()."""
    level = level or config.GENERAL["log_level"]
    if log_file == "":                      # "" means "use the config default"
        log_file = config.GENERAL["log_file"]

    root = logging.getLogger("colourbot")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
                            datefmt="%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        log_file = package_path(log_file)
        try:
            handler = logging.FileHandler(log_file, encoding="utf-8")
            handler.setFormatter(fmt)
            root.addHandler(handler)
        except OSError as exc:              # read-only dir, locked file, ...
            root.warning("could not open log file %s (%s)", log_file, exc)

    # discord.py is extremely chatty on INFO
    logging.getLogger("discord").setLevel(logging.WARNING)


class ControlSignal(Exception):
    """Base class for the two 'please unwind the current run' signals."""


class RestartRequested(ControlSignal):
    """Start the whole flow again from the top, same configuration."""


class KillRequested(ControlSignal):
    """Shut the process down."""


class SessionError(RuntimeError):
    """Something went wrong that the supervisor should restart us for."""


# ===========================================================================
# 2. Persistent runtime stopwatch
# ===========================================================================

class RuntimeTimer:
    """Accumulates total time the bot has been running, across restarts.

    The anti-cheat benchmark is "how long did this bot survive", so the number
    must not be lost when the script is killed, crashes or restarts itself.
    We therefore keep a small json file on disk and top it up every few
    seconds (and on a clean exit).  Worst case a hard `kill -9` loses
    `runtime_flush_seconds` worth of time.
    """

    def __init__(self, path: str = None, flush_seconds: float = None):
        self.path = package_path(path or config.GENERAL["runtime_file"])
        self.flush_seconds = flush_seconds or config.GENERAL["runtime_flush_seconds"]
        self._lock = threading.Lock()
        self._stored = 0.0          # seconds accumulated by previous runs
        self._runs = 0
        self._started_at: Optional[float] = None
        self._stop = threading.Event()

    # -- persistence --------------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._stored = float(data.get("total_seconds", 0.0))
            self._runs = int(data.get("runs", 0))
        except (OSError, ValueError, TypeError):
            self._stored, self._runs = 0.0, 0

    def reset(self) -> None:
        with self._lock:
            self._stored, self._runs = 0.0, 0
            self._started_at = time.monotonic()
        self.flush()
        LOG.info("runtime stopwatch reset to 0")

    def start(self) -> None:
        """Load the previous total and start the background flusher."""
        self.load()
        self._started_at = time.monotonic()
        self._runs += 1
        LOG.info("total runtime so far: %s (run #%d)", self.formatted(), self._runs)
        threading.Thread(target=self._flush_loop, name="runtime-flush",
                         daemon=True).start()

    def _flush_loop(self) -> None:
        while not self._stop.wait(self.flush_seconds):
            self.flush()

    def flush(self) -> None:
        with self._lock:
            payload = {
                "total_seconds": round(self.total_seconds, 3),
                "total_formatted": self.formatted(),
                "runs": self._runs,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        try:
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.debug("runtime flush failed: %s", exc)

    def stop(self) -> None:
        """Fold this session into the stored total and write it out."""
        self._stop.set()
        with self._lock:
            if self._started_at is not None:
                self._stored += time.monotonic() - self._started_at
                self._started_at = None
        self.flush()

    # -- reading ------------------------------------------------------------
    @property
    def session_seconds(self) -> float:
        return 0.0 if self._started_at is None else time.monotonic() - self._started_at

    @property
    def total_seconds(self) -> float:
        return self._stored + self.session_seconds

    @staticmethod
    def _fmt(seconds: float) -> str:
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:d}:{m:02d}:{s:02d}"

    def formatted(self) -> str:
        return self._fmt(self.total_seconds)

    def formatted_session(self) -> str:
        return self._fmt(self.session_seconds)


# ===========================================================================
# 3. The clock -- the only place that sleeps
# ===========================================================================

def sample_delay(spec) -> float:
    """Turn a config delay spec into a concrete number of seconds.

    Anything negative is clamped to 0, exactly like the old
    `max(0, random.uniform(...))` calls.
    """
    if isinstance(spec, config.Fixed):
        value = spec.seconds
    elif isinstance(spec, config.Uniform):
        value = random.uniform(spec.lo, spec.hi)
    elif isinstance(spec, config.Gauss):
        value = random.gauss(spec.mean, spec.stddev)
    elif isinstance(spec, (int, float)):      # allow bare numbers in the table
        value = float(spec)
    else:
        raise TypeError(f"not a delay spec: {spec!r}")
    return max(0.0, value)


class Clock:
    """Named, interruptible waits.

    `clock.wait("veil.before_key")` looks the delay up in config.DELAYS, samples
    it and sleeps.  Because the sleep is implemented with an Event, a Discord
    !kill / !restart takes effect immediately instead of after the sleep.
    """

    def __init__(self, state: "AutomationState", delays: Dict[str, object] = None):
        self.state = state
        self.delays = delays if delays is not None else config.DELAYS

    def spec(self, name: str):
        try:
            return self.delays[name]
        except KeyError:
            raise KeyError(f"unknown delay '{name}' - add it to config.DELAYS") from None

    def sample(self, name: str) -> float:
        return sample_delay(self.spec(name))

    def wait(self, name: str) -> float:
        """Sample DELAYS[name] and sleep for it.  Returns the slept time."""
        seconds = self.sample(name)
        LOG.debug("wait %-28s %.3fs", name, seconds)
        self.sleep(seconds)
        return seconds

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            if self.state.interrupt.wait(seconds):
                self.state.raise_if_interrupted()
        else:
            self.state.raise_if_interrupted()

    def sleep_until(self, deadline_monotonic: float) -> None:
        """Used by the recorded-timeline playback (drift free scheduling)."""
        self.sleep(deadline_monotonic - time.monotonic())


# ===========================================================================
# 4. Shared state (the old pile of module level globals)
# ===========================================================================

class AutomationState:
    """Flags, counters and the relayed-chat mailbox.

    The legacy scripts used ~12 global variables that three threads poked at.
    They are collected here so the ownership is obvious; the semantics (and the
    slightly odd ones, e.g. `!plus` *decrementing* brew_counter) are unchanged.
    """

    def __init__(self):
        self.lock = threading.RLock()

        # -- interruption ---------------------------------------------------
        self.interrupt = threading.Event()   # set for kill *or* restart
        self._pending: Optional[str] = None  # "kill" | "restart"

        # -- what the bot is doing (for !status) ----------------------------
        self.phase = "starting"
        self.route_name = config.DEFAULT_ROUTE
        self.session_index = 0

        # -- relayed game chat ---------------------------------------------
        self.messages: List[str] = []
        self.recent = deque([], maxlen=config.DISCORD["recent_window"])

        # -- automation flags ----------------------------------------------
        self.brew_counter = 0
        self.reset_for_new_session()

    # -- session lifecycle -------------------------------------------------
    def reset_for_new_session(self) -> None:
        """Fresh flags for a new run.

        The legacy script achieved this by exiting the process and spawning a
        new one; we just clear the flags instead (and, like the original, the
        brew counter starts from zero again).
        """
        with self.lock:
            self.clicking = True             # red-target clicking allowed
            self.empty_pouch = False         # (legacy flag, never set)
            self.no_dodgy = False            # dodgy necklace crumbled
            self.shadow_veil_active = True
            self.full_invent = False
            self.valuable_drop = False
            self.target_move = False
            self.five_hp = False             # (legacy flag, only via commented code)
            self.no_orange = False           # out of brews
            self.program_finished = False
            self.brew_counter = 0
            self.messages.clear()
            self.recent.clear()

    # -- interruption ------------------------------------------------------
    def request_restart(self, why: str = "") -> None:
        with self.lock:
            self._pending = "restart"
        LOG.warning("restart requested %s", f"({why})" if why else "")
        self.interrupt.set()

    def request_kill(self, why: str = "") -> None:
        with self.lock:
            self._pending = "kill"
        LOG.warning("kill requested %s", f"({why})" if why else "")
        self.interrupt.set()

    def clear_interrupt(self) -> None:
        with self.lock:
            self._pending = None
        self.interrupt.clear()

    def raise_if_interrupted(self) -> None:
        """Called from every wait; turns the flag into an exception."""
        if not self.interrupt.is_set():
            return
        with self.lock:
            pending = self._pending
        if pending == "kill":
            raise KillRequested()
        raise RestartRequested()

    # -- relayed chat mailbox ---------------------------------------------
    def record_message(self, content: str) -> None:
        """Legacy `on_message` bookkeeping, verbatim.

        `recent` is a small ring buffer used for the loot-spam panic check and
        `messages` is the mailbox the watcher thread consumes.  The 'do not add
        a second copy of the loot-full line' rule is the original's.
        """
        with self.lock:
            self.recent.append(content)
            full = config.DISCORD["messages"]["loot_full"]
            if content == full and full in self.messages:
                LOG.debug("already in mailbox: %s", content)
            else:
                self.messages.append(content)

    def recent_count(self, content: str) -> int:
        with self.lock:
            return self.recent.count(content)

    def has_message(self, content: str) -> bool:
        with self.lock:
            return content in self.messages

    def drop_message(self, content: str, drain: bool = False) -> None:
        """Remove one (or all) copies of a line from the mailbox."""
        with self.lock:
            if content in self.messages:
                self.messages.remove(content)
            if drain:
                while content in self.messages:
                    LOG.debug("remove duplicate %s", content)
                    self.messages.remove(content)

    # -- counters ----------------------------------------------------------
    def bump_brews(self, delta: int) -> int:
        with self.lock:
            self.brew_counter += delta
            return self.brew_counter

    def set_brews(self, value: int) -> int:
        with self.lock:
            self.brew_counter = value
            return self.brew_counter

    def snapshot(self) -> Dict[str, object]:
        """Human readable dump for the Discord !status command."""
        with self.lock:
            return {
                "phase": self.phase,
                "route": self.route_name,
                "run": self.session_index,
                "brew_counter": self.brew_counter,
                "clicking": self.clicking,
                "target_move": self.target_move,
                "full_invent": self.full_invent,
                "no_dodgy": self.no_dodgy,
                "shadow_veil_active": self.shadow_veil_active,
                "valuable_drop": self.valuable_drop,
                "no_orange": self.no_orange,
                "mailbox": len(self.messages),
            }


# ===========================================================================
# 5a. Geometry
# ===========================================================================

@dataclass(frozen=True)
class Rect:
    """Simple x/y/w/h rectangle (top-left origin, like the screen)."""
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w - 1

    @property
    def bottom(self) -> int:
        return self.y + self.h - 1

    @property
    def center(self) -> Tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def as_bbox(self) -> Tuple[int, int, int, int]:
        """PIL/ImageGrab style (left, top, right, bottom) - right/bottom exclusive."""
        return self.x, self.y, self.x + self.w, self.y + self.h

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.bottom

    def clamp(self, x: int, y: int) -> Tuple[int, int]:
        return (min(max(x, self.x), self.right), min(max(y, self.y), self.bottom))

    def shifted(self, dx: int, dy: int) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def __str__(self) -> str:
        return f"({self.x},{self.y}) {self.w}x{self.h}"


@dataclass
class Region:
    """A detected colour blob, in CANVAS coordinates.

    center   : where the old code's `avg_x/avg_y` pointed (bounding box centre)
    x_bounds : (min_x, max_x) of the blob
    y_bounds : (min_y, max_y)
    """
    name: str
    center: Tuple[int, int]
    x_bounds: Tuple[int, int]
    y_bounds: Tuple[int, int]
    area: int = 0

    @property
    def rect(self) -> Rect:
        return Rect(self.x_bounds[0], self.y_bounds[0],
                    self.x_bounds[1] - self.x_bounds[0] + 1,
                    self.y_bounds[1] - self.y_bounds[0] + 1)

    def offset_by(self, off: config.RegionOffset, name: str = None) -> "Region":
        """Derive a neighbouring region (inventory slots, spell icon, ...).

        Same arithmetic the old code did inline on boxed_blue_*.
        """
        cx, cy = self.center
        return Region(
            name=name or off.what,
            center=(cx + off.dcx, cy + off.dcy),
            x_bounds=(self.x_bounds[0] + off.dx0, self.x_bounds[1] + off.dx1),
            y_bounds=(self.y_bounds[0] + off.dy0, self.y_bounds[1] + off.dy1),
        )

    def __str__(self) -> str:
        return (f"{self.name}@{self.center} x{self.x_bounds} y{self.y_bounds}"
                f" area={self.area}")


# -- window lookup -----------------------------------------------------------

def _screen_size() -> Tuple[int, int]:
    """Primary monitor size; falls back to 1920x1080 (office standard)."""
    try:
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        from PIL import ImageGrab                       # noqa: WPS433 (local import)
        return ImageGrab.grab().size
    except Exception:                                    # pragma: no cover
        return 1920, 1080


def _find_window_windows(patterns: Sequence[str]) -> Optional[Rect]:
    """Locate the game window with plain user32/dwmapi calls (no extra deps)."""
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    matches: List[Tuple[str, Rect]] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if not any(p.lower() in title.lower() for p in patterns):
            return True

        rect = RECT()
        # DWMWA_EXTENDED_FRAME_BOUNDS(9) gives the *visible* bounds; plain
        # GetWindowRect includes the invisible resize border on Win10/11.
        if dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 9, ctypes.byref(rect),
                                        ctypes.sizeof(rect)) != 0:
            user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        matches.append((title, Rect(rect.left, rect.top,
                                    rect.right - rect.left, rect.bottom - rect.top)))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    if not matches:
        return None
    title, rect = matches[0]
    LOG.info("found game window %r at %s", title, rect)
    return rect


def _find_window_linux(patterns: Sequence[str]) -> Optional[Rect]:
    """Best effort X11 lookup through wmctrl, for dev boxes / CI."""
    try:
        out = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        title = parts[7]
        if any(p.lower() in title.lower() for p in patterns):
            x, y, w, h = (int(v) for v in parts[2:6])
            LOG.info("found game window %r at (%d,%d) %dx%d", title, x, y, w, h)
            return Rect(x, y, w, h)
    return None


class GameWindow:
    """Where the game is on screen, plus the two coordinate conversions.

    * canvas       : the rendered game area (screen coordinates)
    * to_screen()  : canvas coordinate -> screen coordinate (what the mouse needs)
    * translate_recorded() : a coordinate from a route .json (recorded with the
      canvas at config.GAME_WINDOW["reference_canvas_origin"]) -> screen
      coordinate for wherever the canvas is right now.
    """

    def __init__(self, canvas: Rect, window: Optional[Rect] = None):
        self.canvas = canvas
        self.window = window or canvas
        ref = config.GAME_WINDOW["reference_canvas_origin"]
        self.recorded_offset = (canvas.x - ref[0], canvas.y - ref[1])
        self.screen = Rect(0, 0, *_screen_size())

    # -- construction ------------------------------------------------------
    @classmethod
    def locate(cls, region_override: Optional[Rect] = None,
               refine: bool = None) -> "GameWindow":
        """Find the game window; raise SessionError when it cannot be found.

        `region_override` (--game-region on the CLI) is taken as the canvas
        rectangle itself, which is the escape hatch for odd setups, a second
        client, or running the vision code on a machine without the window API.
        """
        cfg = config.GAME_WINDOW
        if region_override is not None:
            LOG.info("using canvas rectangle from --game-region: %s", region_override)
            return cls(region_override)

        patterns = cfg["title_contains"]
        rect = (_find_window_windows(patterns) if sys.platform == "win32"
                else _find_window_linux(patterns))
        if rect is None:
            raise SessionError(
                f"could not find a window whose title contains {patterns!r}. "
                "Start the game client first, or pass --game-region X,Y,W,H.")

        left, top, _right, _bottom = cfg["window_insets"]
        cw, ch = cfg["canvas_size"]
        canvas = Rect(rect.x + left, rect.y + top, cw, ch)

        if cfg["auto_refine_canvas"] if refine is None else refine:
            canvas = cls._refine_canvas(rect, canvas)

        window = cls(canvas, rect)
        LOG.info("game canvas %s | recorded-coordinate offset %s",
                 canvas, window.recorded_offset)
        return window

    @staticmethod
    def _refine_canvas(window_rect: Rect, guess: Rect) -> Rect:
        """Nudge the canvas origin by looking for the client's window chrome.

        RuneLite paints its frame in a flat colour (config chrome_color), so the
        first row/column that is *not* mostly chrome is the top/left of the
        rendered area.  Cheap, and it saves us from small differences in how
        Windows reports window rectangles.
        """
        try:
            import numpy as np
            from vision import grab                      # local import: avoids a cycle
        except Exception as exc:                          # pragma: no cover
            LOG.debug("canvas refine unavailable (%s)", exc)
            return guess

        cfg = config.GAME_WINDOW
        margin = cfg["auto_refine_max_shift"]
        probe = Rect(window_rect.x - margin, window_rect.y - margin,
                     window_rect.w + 2 * margin, window_rect.h + 2 * margin)
        try:
            img = grab(probe)
        except Exception as exc:                          # pragma: no cover
            LOG.debug("canvas refine grab failed (%s)", exc)
            return guess

        chrome = np.array(cfg["chrome_color"], dtype=int)
        tol = cfg["chrome_tolerance"]
        is_chrome = np.abs(img.astype(int) - chrome).max(axis=2) <= tol

        def longest_gap(flags) -> Tuple[int, int]:
            """Longest run of 'not mostly chrome' lines -> (start, length)."""
            best = (0, 0)
            start = None
            for i, mostly_chrome in enumerate(list(flags) + [True]):
                if not mostly_chrome and start is None:
                    start = i
                elif mostly_chrome and start is not None:
                    if i - start > best[1]:
                        best = (start, i - start)
                    start = None
            return best

        row_x, _ = longest_gap(is_chrome.mean(axis=1) >= 0.5)
        col_x, _ = longest_gap(is_chrome.mean(axis=0) >= 0.5)
        found = Rect(probe.x + col_x, probe.y + row_x, guess.w, guess.h)

        shift = (abs(found.x - guess.x), abs(found.y - guess.y))
        if max(shift) > margin:
            LOG.warning("canvas auto-refine wanted to move the canvas by %s px - "
                        "ignoring it and trusting config window_insets (%s)",
                        shift, guess)
            return guess
        if shift != (0, 0):
            LOG.info("canvas auto-refined from %s to %s", guess, found)
        return found

    # -- conversions -------------------------------------------------------
    def to_screen(self, x: int, y: int) -> Tuple[int, int]:
        return self.canvas.x + int(x), self.canvas.y + int(y)

    def to_canvas(self, x: int, y: int) -> Tuple[int, int]:
        return int(x) - self.canvas.x, int(y) - self.canvas.y

    def translate_recorded(self, x: int, y: int) -> Tuple[int, int]:
        dx, dy = self.recorded_offset
        return int(x) + dx, int(y) + dy

    def to_recorded(self, x: int, y: int) -> Tuple[int, int]:
        """Inverse of translate_recorded - used while *recording* a new route."""
        dx, dy = self.recorded_offset
        return int(x) - dx, int(y) - dy

    def clamp_to_canvas(self, x: int, y: int) -> Tuple[int, int]:
        return self.canvas.clamp(x, y)


# ===========================================================================
# 5b. Input
# ===========================================================================
# The three input libraries the original used are kept exactly as they were,
# because for anti-cheat work *how* the events are injected matters:
#   * `keyboard` : all key presses outside of a recorded route
#   * `mouse`    : all human-like moves/clicks driven by the vision code
#   * `pynput`   : playback of recorded routes (both mouse and keyboard)

def _optional_import(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:                              # pragma: no cover
        LOG.debug("optional module %s unavailable: %s", name, exc)
        return None


keyboard_lib = _optional_import("keyboard")
mouse_lib = _optional_import("mouse")
pynput_mouse = _optional_import("pynput.mouse")
pynput_keyboard = _optional_import("pynput.keyboard")


def ease_in_out(t: float) -> float:
    """Smooth cubic easing (slow start, slow finish)."""
    return 3 * t ** 2 - 2 * t ** 3


def cubic_bezier(start, end, control1, control2, t, intensity=0.5):
    """Cubic bezier with the control points pulled towards the straight line."""
    def lerp(p1, p2, f):
        return (p1[0] * (1 - f) + p2[0] * f, p1[1] * (1 - f) + p2[1] * f)

    control1 = lerp(start, control1, intensity)
    control2 = lerp(end, control2, intensity)
    x = ((1 - t) ** 3 * start[0] + 3 * (1 - t) ** 2 * t * control1[0]
         + 3 * (1 - t) * t ** 2 * control2[0] + t ** 3 * end[0])
    y = ((1 - t) ** 3 * start[1] + 3 * (1 - t) ** 2 * t * control1[1]
         + 3 * (1 - t) * t ** 2 * control2[1] + t ** 3 * end[1])
    return int(x), int(y)


class InputController:
    """All keyboard/mouse output of the bot.

    Every public method takes CANVAS coordinates (0,0 = top-left of the game
    area) and converts them to screen coordinates itself, so no caller can
    accidentally aim at the desktop again.  A lock serialises the threads that
    send input (target clicker, chat watchdog, Discord commands).
    """

    def __init__(self, window: GameWindow, clock: Clock, mouse_cfg: dict = None):
        self.window = window
        self.clock = clock
        self.cfg = mouse_cfg or config.MOUSE
        self.lock = threading.RLock()
        self._mouse_ctrl = None
        self._keyboard_ctrl = None

    # -- keyboard ----------------------------------------------------------
    def key_down(self, key: str) -> None:
        self._require(keyboard_lib, "keyboard")
        with self.lock:
            keyboard_lib.press(key)

    def key_up(self, key: str) -> None:
        self._require(keyboard_lib, "keyboard")
        with self.lock:
            keyboard_lib.release(key)

    def tap(self, key: str, hold: str = None, after: str = None,
            note: str = "") -> None:
        """press -> wait DELAYS[hold] -> release -> wait DELAYS[after]."""
        LOG.info("key '%s'%s", key, f" ({note})" if note else "")
        with self.lock:
            self.key_down(key)
            try:
                if hold:
                    self.clock.wait(hold)
            finally:                     # a kill/restart must not stick the key
                self.key_up(key)
        if after:
            self.clock.wait(after)

    @contextlib.contextmanager
    def held_key(self, key: str):
        """`with input.held_key('shift'): ...` - guarantees the release."""
        self.key_down(key)
        try:
            yield
        finally:
            self.key_up(key)

    # -- mouse -------------------------------------------------------------
    def position(self) -> Tuple[int, int]:
        self._require(mouse_lib, "mouse")
        return mouse_lib.get_position()

    def move_and_click(self, cx: int, cy: int, jitter_px: int = None,
                       click: bool = True) -> Tuple[int, int]:
        """Human-like bezier move to a canvas point, then (optionally) click.

        Port of the original `human_like_mouse_move_and_click`: same easing,
        same number of samples, same random ranges drawn in the same order, so
        the movement profile an anti-cheat would see is unchanged.  Only the
        target is different - it is now clamped into the game canvas.
        """
        self._require(mouse_lib, "mouse")
        cfg = self.cfg
        jitter = cfg["jitter_px"] if jitter_px is None else jitter_px

        with self.lock:
            start_x, start_y = mouse_lib.get_position()
            target_x, target_y = self.window.to_screen(cx, cy)
            target_x += random.randint(-jitter, jitter)
            target_y += random.randint(-jitter, jitter)
            if config.GAME_WINDOW["clamp_clicks_to_canvas"]:
                target_x, target_y = self.window.clamp_to_canvas(target_x, target_y)

            spread = cfg["control_spread_px"]
            control1 = ((start_x + target_x) // 2 + random.randint(-spread, spread),
                        (start_y + target_y) // 2 + random.randint(-spread, spread))
            control2 = ((start_x + target_x) // 2 + random.randint(-spread, spread),
                        (start_y + target_y) // 2 + random.randint(-spread, spread))

            steps = cfg["steps"]
            for i in range(steps):
                eased = ease_in_out(i / (steps - 1))
                nx, ny = cubic_bezier((start_x, start_y), (target_x, target_y),
                                      control1, control2, eased,
                                      cfg["bezier_intensity"])
                # slower at the start and the end of the stroke
                duration = sample_delay(cfg["step_duration"]) * (1 - abs(0.5 - eased))
                mouse_lib.move(nx, ny, absolute=True, duration=duration)

            mouse_lib.move(target_x, target_y, absolute=True,
                           duration=sample_delay(cfg["final_duration"]))
            if click:
                mouse_lib.click()
        return target_x, target_y

    def click_here(self) -> None:
        """Click wherever the cursor already is (the idle-clicking loop)."""
        self._require(mouse_lib, "mouse")
        with self.lock:
            mouse_lib.click()

    def click_region(self, region: Region, clicks: int = 1,
                     jitter_px: int = None) -> None:
        """Click a random point inside the central 20% of `region`.

        Port of `click_region_multiple_times`, including the gaussian gap
        between the extra clicks.
        """
        shrink = self.cfg["region_shrink"]
        (min_x, max_x), (min_y, max_y) = region.x_bounds, region.y_bounds
        avg_x, avg_y = region.center
        box_w = int((max_x - min_x) * shrink)
        box_h = int((max_y - min_y) * shrink)
        x0, x1 = max(min_x, avg_x - box_w // 2), min(max_x, avg_x + box_w // 2)
        y0, y1 = max(min_y, avg_y - box_h // 2), min(max_y, avg_y + box_h // 2)
        click_x = random.randint(min(x0, x1), max(x0, x1))
        click_y = random.randint(min(y0, y1), max(y0, y1))

        LOG.info("click %s at canvas (%d,%d)%s", region.name, click_x, click_y,
                 f" x{clicks}" if clicks > 1 else "")
        self.move_and_click(click_x, click_y, jitter_px=jitter_px)
        for _ in range(clicks - 1):
            self.clock.sleep(sample_delay(self.cfg["extra_click_interval"]))
            self.click_here()

    # -- recorded timeline -------------------------------------------------
    def play_timeline(self, events: Sequence[dict], translate: bool = True) -> int:
        """Replay a recorded route.

        The recorded absolute timestamps are turned into deadlines against one
        monotonic start time (exactly like the original, but drift free), and
        every coordinate is shifted by the game window offset so the .json
        files stay valid no matter where the client window sits.
        """
        if not events:
            LOG.warning("timeline is empty, nothing to replay")
            return 0

        self._require(pynput_mouse, "pynput")
        if self._mouse_ctrl is None:
            self._mouse_ctrl = pynput_mouse.Controller()
            self._keyboard_ctrl = pynput_keyboard.Controller()
        mouse_ctrl = self._mouse_ctrl
        keyboard_ctrl = self._keyboard_ctrl

        events = sorted(events, key=lambda e: e["timestamp"])
        first = events[0]["timestamp"]
        started = time.monotonic()

        for event in events:
            self.clock.sleep_until(started + (event["timestamp"] - first))
            kind = event["type"]

            if kind == "mouse_move":
                mouse_ctrl.position = self._event_point(event, translate)
            elif kind == "mouse_click":
                button = getattr(pynput_mouse.Button, event["button"])
                if event["pressed"]:
                    mouse_ctrl.press(button)
                else:
                    mouse_ctrl.release(button)
            elif kind == "mouse_scroll":
                mouse_ctrl.scroll(event["dx"], event["dy"])
            elif kind in ("key_press", "key_release"):
                key = event["key"]
                key_obj = key if len(key) == 1 else getattr(
                    pynput_keyboard.Key, key, key)
                if kind == "key_press":
                    keyboard_ctrl.press(key_obj)
                else:
                    keyboard_ctrl.release(key_obj)
            else:
                LOG.debug("ignoring unknown event type %r", kind)

        return len(events)

    def _event_point(self, event: dict, translate: bool) -> Tuple[int, int]:
        x, y = event["x"], event["y"]
        if not translate:
            return int(x), int(y)
        x, y = self.window.translate_recorded(x, y)
        if config.GAME_WINDOW["clamp_replayed_moves_to_canvas"]:
            x, y = self.window.clamp_to_canvas(x, y)
        return x, y

    # -- misc --------------------------------------------------------------
    @staticmethod
    def _require(module, name: str) -> None:
        if module is None:
            raise SessionError(
                f"python module '{name}' is not installed - run "
                "`pip install -r requirements.txt` (Linux also needs root for "
                "the keyboard/mouse libraries).")


# ===========================================================================
# Panic key
# ===========================================================================

def start_panic_key_listener(state: AutomationState, timer: RuntimeTimer = None,
                             key: str = None) -> None:
    """ESC anywhere on the desktop kills the process (legacy behaviour)."""
    key = key or config.GENERAL["panic_key"]
    if keyboard_lib is None:
        LOG.warning("keyboard module missing - the '%s' panic key is disabled", key)
        return

    def _wait():
        keyboard_lib.wait(key)
        LOG.warning("%s pressed, exiting program", key.upper())
        if timer is not None:
            timer.stop()
            LOG.warning("total runtime: %s", timer.formatted())
        os._exit(0)

    threading.Thread(target=_wait, name="panic-key", daemon=True).start()
    LOG.info("press %s at any time to exit the program", key.upper())
