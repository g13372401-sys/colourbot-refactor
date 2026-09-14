"""
overlay.py -- the --debug live overlay for the game window.
============================================================

A borderless, always-on-top Tkinter window positioned exactly over the game
canvas.  Three tricks make it useful:

    1. Transparent background (Tk `-transparentcolor`) so you can *see* the
       game underneath.
    2. WS_EX_TRANSPARENT + WS_EX_NOACTIVATE extended window styles (set with
       ctypes) so every mouse click passes straight through to the game -
       you can still drive RuneLite normally while the overlay is up.
    3. A tiny redraw loop that renders, on top of the game:
         * the live cursor position in *canvas* coordinates (0,0 = top-left
           of the game area, exactly how the rest of the script works), and
         * a scrolling feed of the actions the bot just made the input take
           (clicks, key taps, route replay events) pushed in via
           `push_action()`.

Everything is additive: the overlay never blocks, never sleeps the clock and
never changes what the automation does.  When `--debug` is not given, none of
this runs and the input sink stays a no-op.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections import deque
from typing import Optional

from core import GameWindow

try:
    import tkinter as tk
except Exception as _exc:                              # pragma: no cover
    tk = None

LOG = logging.getLogger("colourbot.overlay")

# The magic background colour.  Every pixel of this exact RGB in the Tk canvas
# becomes transparent, so the game shows through and only the HUD text/boxes
# (drawn in other colours) are visible.
TRANSPARENT = "#010203"

# Extended window styles applied to the overlay so it never intercepts input:
#   WS_EX_TRANSPARENT(0x20)   -> clicks/events pass through to windows below
#   WS_EX_LAYERED(0x80000)    -> required for the per-pixel transparency
#   WS_EX_NOACTIVATE(0x8000000) -> never steal focus from the game
#   WS_EX_TOOLWINDOW(0x80)    -> don't show up in the taskbar / alt-tab
#   WS_EX_TOP(0x8) / WS_EX_LEFT(0x0)
_CLICK_THROUGH_STYLES = 0x20 | 0x80000 | 0x08000000 | 0x80
GWL_EXSTYLE = -20

_REFRESH_MS = 50      # ~20 FPS redraw for the cursor readout
_MAX_ACTIONS = 60     # how many recent actions the feed keeps


class DebugOverlay:
    """Transparent, click-through Tk overlay over the game canvas.

    Owns a single daemon thread that runs the Tk mainloop (all Tk calls happen
    on that one thread, which is what makes Tk happy).  `push_action()` may be
    called from any bot thread: it just appends to a small thread-safe deque
    that the redraw loop drains.
    """

    def __init__(self, window: GameWindow):
        if tk is None:                                 # pragma: no cover
            raise RuntimeError("tkinter is required for --debug")
        self.window = window
        self._root: Optional[tk.Tk] = None
        self._canvas: Optional[tk.Canvas] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._actions: deque = deque(maxlen=_MAX_ACTIONS)
        self._running = False
        self._mouse = None            # lazily imported in the overlay thread
        self._last_cursor = (0, 0)

    # -- public API ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="debug-overlay",
                                        daemon=True)
        self._thread.start()
        LOG.info("debug overlay started over canvas %s", self.window.canvas)

    def stop(self) -> None:
        self._running = False
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)   # schedule teardown on the Tk thread
            except Exception:                  # pragma: no cover
                pass
        self._thread = None

    def push_action(self, text: str) -> None:
        """Record one bot action to show in the feed (any thread)."""
        if not text:
            return
        with self._lock:
            self._actions.append((time.strftime("%H:%M:%S"), text))

    # -- the Tk thread ------------------------------------------------------
    def _run(self) -> None:
        self._ensure_dpi_aware()
        try:
            import mouse                              # reuse the same lib as input
            self._mouse = mouse
        except Exception:                              # pragma: no cover
            self._mouse = None

        root = tk.Tk()
        root.withdraw()
        self._root = root

        # Position + size exactly over the game canvas, borderless + topmost.
        canvas_rect = self.window.canvas
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{canvas_rect.w}x{canvas_rect.h}"
                      f"+{canvas_rect.x}+{canvas_rect.y}")
        root.attributes("-transparentcolor", TRANSPARENT)
        root.configure(bg=TRANSPARENT)

        view = tk.Canvas(root, width=canvas_rect.w, height=canvas_rect.h,
                         bg=TRANSPARENT, highlightthickness=0, bd=0)
        view.pack(fill="both", expand=True)

        self._canvas = view
        root.deiconify()
        root.update_idletasks()
        self._apply_click_through(root)

        LOG.info("debug overlay window live at %s", canvas_rect)
        self._tick()
        root.mainloop()
        self._root = None

    def _tick(self) -> None:
        if not self._running or self._canvas is None:
            return
        canvas = self._canvas
        canvas.delete("all")

        # Live cursor readout in canvas coordinates (0,0 = top-left of game).
        canvas_x, canvas_y = self._cursor_position()

        # Human readable crosshair near the cursor, and a fixed HUD corner box.
        box_w, corner_pad = 230, 8
        box_x = self.window.canvas.w - box_w - corner_pad
        box_y = corner_pad

        screen_x, screen_y = self.window.to_screen(canvas_x, canvas_y)

        canvas.create_rectangle(box_x, box_y, box_x + box_w, box_y + 58,
                                fill="#081018", stipple="gray50",
                                outline="#33ccff", width=1)
        canvas.create_text(box_x + 8, box_y + 6, anchor="nw", fill="#33ccff",
                           font=("Consolas", 10, "bold"),
                           text=f"cursor canvas: {canvas_x},{canvas_y}")
        canvas.create_text(box_x + 8, box_y + 24, anchor="nw", fill="#bbbbbb",
                           font=("Consolas", 9),
                           text=f"screen: {screen_x},{screen_y}")
        canvas.create_text(box_x + 8, box_y + 40, anchor="nw", fill="#88ff88",
                           font=("Consolas", 9), text="bot input preview")

        with self._lock:
            actions = list(self._actions)

        # Scrolling action feed (most recent on top), bottom-left corner.
        line_h = 14
        feed_x, feed_y = corner_pad, self.window.canvas.h - corner_pad
        feed_w = self.window.canvas.w - 2 * corner_pad
        shown = actions[-12:]
        feed_h = line_h * len(shown) + 6
        canvas.create_rectangle(feed_x, feed_y - feed_h, feed_x + feed_w + 30,
                                feed_y, fill="#081018", stipple="gray50",
                                outline="#33ccff", width=1)
        for i, (ts, text) in enumerate(shown):
            y = feed_y - feed_h + 6 + i * line_h
            canvas.create_text(feed_x + 6, y, anchor="nw", fill="#cccccc",
                               font=("Consolas", 9),
                               text=f"[{ts}] {text}")

        self._root.after(_REFRESH_MS, self._tick)

    def _cursor_position(self) -> tuple:
        """Current cursor in canvas coords; falls back to the last known."""
        if self._mouse is not None:
            try:
                sx, sy = self._mouse.get_position()
                cx, cy = self.window.to_canvas(sx, sy)
                self._last_cursor = (cx, cy)
                return cx, cy
            except Exception:                          # pragma: no cover
                pass
        return self._last_cursor

    @staticmethod
    def _ensure_dpi_aware() -> None:
        """Match core.py's DPI awareness so Tk geometry aligns with the mouse
        and vision coordinate space (which is what ties everything together)."""
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
        except Exception:                              # pragma: no cover
            pass

    @staticmethod
    def _apply_click_through(root) -> None:
        """Give the overlay the WS_EX_TRANSPARENT style so clicks pass through
        to the game underneath."""
        if sys.platform != "win32":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            if not hwnd:
                hwnd = root.winfo_id()
            cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, cur | _CLICK_THROUGH_STYLES)
        except Exception as exc:                       # pragma: no cover
            LOG.debug("could not enable click-through on the overlay: %s", exc)
