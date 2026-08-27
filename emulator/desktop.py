"""
desktop.py -- the virtual 1920x1080 screen everything is drawn on.
=================================================================

The bot believes it is running on a normal desktop: it asks the window manager
where the window called "RuneLite" is, moves the *desktop* mouse to a point
inside it, clicks, and screenshots rectangles of the *screen*.  This module is
that desktop.

Two layers, and the distinction matters:

    base layer     the windows themselves.  This - and only this - is what
                   `ImageGrab.grab()` returns, because on a real X11/Windows
                   desktop a screen capture does not contain the mouse pointer.
    overlay layer  the pointer, the click ripples, the pressed-key caps, the
                   movement trail and the HUD.  Drawn only into the frame the
                   engineer watches, so making the input *visible* can never
                   change what the bot's vision code sees.

The screen refreshes at 50 Hz: repeated grabs inside one 20 ms tick get the same
pixels, exactly like a real display, and the game's `update()` is therefore
called at most 50 times a second no matter how hard the vision threads poll.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

import cv2
import numpy as np

from . import render as R
from .render import Box

SCREEN_W, SCREEN_H = 1920, 1080
REFRESH = 1.0 / 50.0                       # the virtual monitor's frame time

CURSOR_TRAIL = 90                          # points kept for the motion trail
RIPPLE_TTL = 0.55
KEY_BADGE_TTL = 0.9


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------

@dataclass
class Window:
    """One window on the virtual desktop.

    `render` returns the whole window surface (chrome included) at exactly
    `w x h`.  `on_click` / `on_key` receive *window local* coordinates; the game
    window's adapter turns those into canvas coordinates.
    """
    title: str
    x: int
    y: int
    w: int
    h: int
    render: Callable[[], np.ndarray]
    grabbable: bool = True                 # False -> HUD-only, invisible to grabs
    listed: bool = True                    # False -> hidden from wmctrl
    on_click: Optional[Callable[[int, int, str, str], None]] = None
    on_key: Optional[Callable[[str, str], None]] = None

    @property
    def box(self) -> Box:
        return Box(self.x, self.y, self.w, self.h)

    def contains(self, x: int, y: int) -> bool:
        return self.box.contains(x, y)


@dataclass
class _Ripple:
    x: int
    y: int
    born: float
    button: str
    source: str


@dataclass
class _KeyFlash:
    key: str
    born: float
    released: Optional[float] = None


@dataclass
class _Event:
    """One line for the on-screen event log."""
    kind: str
    text: str
    at: float
    color: Tuple[int, int, int] = R.TEXT


# ---------------------------------------------------------------------------
# the desktop
# ---------------------------------------------------------------------------

class Desktop:
    def __init__(self, width: int = SCREEN_W, height: int = SCREEN_H):
        self.width = width
        self.height = height
        self.lock = threading.RLock()
        self.windows: List[Window] = []          # back to front
        self.focus: Optional[Window] = None

        self.mouse = [width // 2, height // 2]
        self.buttons: Dict[str, bool] = {}
        self.keys: Dict[str, bool] = {}

        self.trail: Deque[Tuple[int, int, float]] = deque(maxlen=CURSOR_TRAIL)
        # Where every press landed, in screen coordinates.  Kept whole: it is
        # the only record of the clicks that missed the game window entirely,
        # which no other layer would ever see.
        self.click_points: List[Tuple[int, int, str]] = []
        self.ripples: List[_Ripple] = []
        self.key_flashes: List[_KeyFlash] = []
        self.events: Deque[_Event] = deque(maxlen=14)

        self.moves = 0
        self.clicks = 0
        self.key_presses = 0
        self.started = time.monotonic()

        self._background = self._make_background()
        self._frame: Optional[np.ndarray] = None
        self._frame_at = 0.0
        self.hud_renderer: Optional[Callable[[np.ndarray], None]] = None

    # -- setup -------------------------------------------------------------
    def _make_background(self) -> np.ndarray:
        """A calm, definitely-not-chrome-coloured wallpaper.

        `GameWindow._refine_canvas` probes 20 px *outside* the window looking for
        the client's chrome colour, so the wallpaper must not be near
        (30, 30, 30) or the canvas origin would be found in the wrong place.
        """
        bg = R.new_surface(self.width, self.height, R.DESKTOP_BG)
        for x in range(0, self.width, 60):
            bg[:, x:x + 1] = R.DESKTOP_GRID
        for y in range(0, self.height, 60):
            bg[y:y + 1, :] = R.DESKTOP_GRID
        R.text(bg, "colour-bot emulator - virtual desktop 1920x1080",
               (24, self.height - 18), 0.5, (70, 80, 96), 1)
        return bg

    def add_window(self, window: Window) -> Window:
        with self.lock:
            self.windows.append(window)
            if window.on_key is not None and self.focus is None:
                self.focus = window
        return window

    def window_at(self, x: int, y: int) -> Optional[Window]:
        for window in reversed(self.windows):
            if window.grabbable and window.contains(x, y):
                return window
        return None

    def list_windows(self) -> List[dict]:
        """What the fake `wmctrl -lG` prints."""
        with self.lock:
            return [{"title": w.title, "x": w.x, "y": w.y, "w": w.w, "h": w.h}
                    for w in self.windows if w.listed]

    # -- event log ---------------------------------------------------------
    def log(self, kind: str, text: str, color: Tuple[int, int, int] = R.TEXT) -> None:
        with self.lock:
            self.events.append(_Event(kind, text, time.monotonic(), color))

    # -- input -------------------------------------------------------------
    def move_mouse(self, x: int, y: int, source: str = "mouse") -> Tuple[int, int]:
        x = max(0, min(self.width - 1, int(x)))
        y = max(0, min(self.height - 1, int(y)))
        with self.lock:
            self.mouse = [x, y]
            self.moves += 1
            self.trail.append((x, y, time.monotonic()))
        return x, y

    def press_button(self, button: str, action: str,
                     source: str = "mouse") -> None:
        x, y = self.mouse_position()
        with self.lock:
            self.buttons[button] = (action == "press")
            if action == "press":
                self.clicks += 1
                self.click_points.append((x, y, button))
                self.ripples.append(_Ripple(x, y, time.monotonic(), button, source))
        window = self.window_at(x, y)
        if window is not None:
            with self.lock:
                if window.on_key is not None:
                    self.focus = window
            if window.on_click is not None:
                window.on_click(x - window.x, y - window.y, button, action)
        elif action == "press":
            self.log("click", f"{button} click on the desktop ({x},{y})", R.TEXT_DIM)

    def send_key(self, key: str, action: str, source: str = "keyboard") -> None:
        key = (key or "").lower()
        now = time.monotonic()
        with self.lock:
            self.keys[key] = (action == "press")
            if action == "press":
                self.key_presses += 1
                held = next((flash for flash in self.key_flashes
                             if flash.key == key and flash.released is None), None)
                if held is not None:
                    held.born = now          # auto-repeat of a key already down
                else:
                    self.key_flashes.append(_KeyFlash(key, now))
            else:
                for flash in reversed(self.key_flashes):
                    if flash.key == key and flash.released is None:
                        flash.released = now
                        break
            window = self.focus
        if window is not None and window.on_key is not None:
            window.on_key(key, action)

    def mouse_position(self) -> Tuple[int, int]:
        with self.lock:
            return int(self.mouse[0]), int(self.mouse[1])

    def key_is_pressed(self, key: str) -> bool:
        with self.lock:
            return bool(self.keys.get((key or "").lower()))

    # -- composition -------------------------------------------------------
    def base_frame(self, now: Optional[float] = None) -> np.ndarray:
        """The screen as a screenshot would see it (no pointer, no HUD)."""
        now = now or time.monotonic()
        with self.lock:
            fresh = self._frame is not None and (now - self._frame_at) < REFRESH
            if fresh:
                return self._frame
            windows = list(self.windows)

        frame = self._background.copy()
        for window in windows:
            if not window.grabbable:
                continue
            surface = window.render()
            R.blit(frame, surface, window.x, window.y)

        with self.lock:
            self._frame = frame
            self._frame_at = now
        return frame

    def grab(self, bbox: Optional[Tuple[int, int, int, int]] = None) -> np.ndarray:
        """`ImageGrab.grab(bbox)` - the whole screen, or a clipped rectangle."""
        frame = self.base_frame()
        if bbox is None:
            return frame
        left, top, right, bottom = (int(v) for v in bbox)
        left, top = max(0, left), max(0, top)
        right, bottom = min(self.width, right), min(self.height, bottom)
        if right <= left or bottom <= top:
            return np.zeros((1, 1, 3), dtype=np.uint8)
        return frame[top:bottom, left:right].copy()

    def view_frame(self) -> np.ndarray:
        """What the engineer watches: base + pointer + input effects + HUD."""
        now = time.monotonic()
        frame = self.base_frame(now).copy()
        self._draw_overlay_windows(frame)
        self._draw_trail(frame, now)
        self._draw_ripples(frame, now)
        if self.hud_renderer is not None:
            self.hud_renderer(frame)
        self._draw_keys(frame, now)
        x, y = self.mouse_position()
        R.draw_cursor(frame, x, y, any(self.buttons.values()))
        return frame

    def _draw_overlay_windows(self, frame: np.ndarray) -> None:
        with self.lock:
            windows = [w for w in self.windows if not w.grabbable]
        for window in windows:
            R.blit(frame, window.render(), window.x, window.y)

    def _draw_trail(self, frame: np.ndarray, now: float) -> None:
        with self.lock:
            points = list(self.trail)
        if len(points) < 2:
            return
        oldest = points[0][2]
        span = max(1e-3, now - oldest)
        previous = points[0]
        for point in points[1:]:
            age = (point[2] - oldest) / span            # 0 = oldest, 1 = newest
            tint = (int(60 + 160 * age), int(90 + 130 * age), int(200 + 40 * age))
            cv2.line(frame, previous[:2], point[:2], tint, 1, cv2.LINE_AA)
            previous = point

    def _draw_ripples(self, frame: np.ndarray, now: float) -> None:
        with self.lock:
            self.ripples = [r for r in self.ripples if now - r.born <= RIPPLE_TTL]
            ripples = list(self.ripples)
        for ripple in ripples:
            color = (255, 214, 64) if ripple.button == "left" else (120, 200, 255)
            R.draw_click_ripple(frame, ripple.x, ripple.y, now - ripple.born,
                                RIPPLE_TTL, color)

    def _draw_keys(self, frame: np.ndarray, now: float) -> None:
        with self.lock:
            self.key_flashes = [
                flash for flash in self.key_flashes
                if flash.released is None or now - flash.released <= KEY_BADGE_TTL]
            flashes = list(self.key_flashes)[-6:]
        if not flashes:
            return
        # A key that is still down keeps a marker, so a held shift is obvious.
        labels = [flash.key if flash.released is not None else f"{flash.key} *"
                  for flash in flashes]
        widths = [R.text_size(label.upper(), 0.5, 1)[0] + 18 for label in labels]
        total = sum(widths) + 6 * (len(labels) - 1)
        R.draw_key_badge(frame, labels, (self.width - total) // 2, self.height - 62)
