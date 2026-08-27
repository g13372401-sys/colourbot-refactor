"""
viewer.py -- what the engineer watches, and the HUD that explains it.
=====================================================================

The emulator draws a full 1920x1080 desktop.  This module puts that in front of
a human:

    * a live window (`cv2.imshow`) when there is a display,
    * an mp4 recording and PNG snapshots always, so a headless CI run still
      produces something you can watch afterwards,
    * the HUD: run clock, input counters, game state, the event log and the
      scenario's progress through the flow.

The HUD is drawn into the *overlay* layer only (see desktop.py), so none of it
can leak into a screenshot the bot takes - the emulator can be as loud as it
likes without changing what the vision code sees.

If a real X display is available the pointer of the actual desktop is warped to
follow the emulated one, so the engineer sees their own cursor being driven
around the fake client.  That is cosmetic; the bot's clicks always go through
the virtual desktop.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np

from . import render as R
from .desktop import Desktop
from .render import Box

WINDOW_NAME = "colour-bot emulator"
DEFAULT_FPS = 15

TOP_PANEL = Box(20, 12, 1880, 92)
BOTTOM_LEFT = Box(20, 812, 600, 180)
BOTTOM_MID = Box(636, 812, 640, 180)
BOTTOM_RIGHT = Box(1292, 812, 608, 180)


# ---------------------------------------------------------------------------
# HUD
# ---------------------------------------------------------------------------

class Hud:
    """Draws the run's telemetry around the two emulated windows."""

    def __init__(self, server, scenario=None):
        self.server = server
        self.scenario = scenario
        self.started = time.monotonic()

    # -- entry point (installed as Desktop.hud_renderer) -------------------
    def render(self, frame: np.ndarray) -> None:
        self._top(frame)
        self._game_state(frame, BOTTOM_LEFT)
        self._event_log(frame, BOTTOM_MID)
        self._scenario(frame, BOTTOM_RIGHT)

    # -- panels ------------------------------------------------------------
    def _top(self, frame: np.ndarray) -> None:
        desktop: Desktop = self.server.desktop
        R.panel(frame, TOP_PANEL, (26, 32, 42), (62, 72, 90))
        R.text(frame, "colour-bot emulator", (TOP_PANEL.x + 18, TOP_PANEL.y + 34),
               0.78, R.TEXT_BRIGHT, 1)
        R.text(frame, "python main.py --route route1  |  the script is not "
                      "modified and cannot tell this desktop from a real one",
               (TOP_PANEL.x + 18, TOP_PANEL.y + 62), 0.45, R.TEXT_DIM, 1)

        elapsed = time.monotonic() - self.started
        pid = self.server.bot_pid
        stats: List[Tuple[str, str, tuple]] = [
            ("elapsed", f"{int(elapsed) // 60:d}:{int(elapsed) % 60:02d}", R.TEXT_BRIGHT),
            ("mouse moves", f"{desktop.moves}", R.INFO),
            ("clicks", f"{desktop.clicks}", R.WARN),
            ("keys", f"{desktop.key_presses}", R.INFO),
            ("screen grabs", f"{self.server.grabs}", R.TEXT),
            ("discord in/out",
             f"{self.server.discord.injected}/{self.server.discord.sent_by_bot}",
             R.GOOD if self.server.discord.online else R.BAD),
            ("bot pid", str(pid) if pid else "-",
             R.GOOD if pid else R.TEXT_DIM),
        ]
        x = TOP_PANEL.x + 700
        for label, value, color in stats:
            width = max(R.text_size(label, 0.4, 1)[0],
                        R.text_size(value, 0.62, 1)[0]) + 22
            R.text(frame, label, (x, TOP_PANEL.y + 30), 0.4, R.TEXT_DIM, 1)
            R.text(frame, value, (x, TOP_PANEL.y + 62), 0.62, color, 1)
            x += width

    def _game_state(self, frame: np.ndarray, box: Box) -> None:
        R.panel(frame, box, (26, 32, 42), (62, 72, 90))
        R.text(frame, "GAME CLIENT", (box.x + 12, box.y + 20), 0.44,
               R.TEXT_BRIGHT, 1)
        rows = self.server.game.summary()
        half = (len(rows) + 1) // 2
        for column, chunk in enumerate((rows[:half], rows[half:])):
            cx = box.x + 12 + column * 296
            for index, (label, value) in enumerate(chunk):
                y = box.y + 44 + index * 19
                R.text(frame, label, (cx, y), 0.4, R.TEXT_DIM, 1)
                R.text(frame, value, (cx + 132, y), 0.4, R.TEXT, 1)

    def _event_log(self, frame: np.ndarray, box: Box) -> None:
        R.panel(frame, box, (26, 32, 42), (62, 72, 90))
        R.text(frame, "WHAT THE SCRIPT IS DOING TO THE CLIENT",
               (box.x + 12, box.y + 20), 0.44, R.TEXT_BRIGHT, 1)
        desktop: Desktop = self.server.desktop
        with desktop.lock:
            events = list(desktop.events)[-7:]
        now = time.monotonic()
        for index, event in enumerate(events):
            y = box.y + 44 + index * 19
            R.text(frame, f"{now - event.at:4.1f}s", (box.x + 12, y), 0.38,
                   R.TEXT_DIM, 1)
            R.text(frame, event.kind, (box.x + 58, y), 0.38, event.color, 1)
            text = R.wrap(event.text, box.w - 150, 0.38)[0]
            R.text(frame, text, (box.x + 130, y), 0.38, R.TEXT, 1)

    def _scenario(self, frame: np.ndarray, box: Box) -> None:
        R.panel(frame, box, (26, 32, 42), (62, 72, 90))
        R.text(frame, "SCENARIO", (box.x + 12, box.y + 20), 0.44, R.TEXT_BRIGHT, 1)
        scenario = self.scenario
        if scenario is None:
            R.text(frame, "free run (no scripted events)", (box.x + 12, box.y + 46),
                   0.4, R.TEXT_DIM, 1)
            return

        state = scenario.hud_state()
        R.progress_bar(frame, Box(box.x + 12, box.y + 28, box.w - 24, 6),
                       state.get("progress", 0.0), R.ACCENT)
        for index, (label, status) in enumerate(state.get("steps", [])[-6:]):
            y = box.y + 56 + index * 19
            color = {"done": R.GOOD, "active": R.ACCENT, "failed": R.BAD}.get(
                status, R.TEXT_DIM)
            marker = {"done": "[x]", "active": "[>]", "failed": "[!]"}.get(
                status, "[ ]")
            R.text(frame, marker, (box.x + 12, y), 0.4, color, 1)
            R.text(frame, R.wrap(label, box.w - 70, 0.4)[0], (box.x + 48, y),
                   0.4, color if status != "pending" else R.TEXT_DIM, 1)

        passed, failed = state.get("checks", (0, 0))
        summary = f"checks passed {passed}   failed {failed}"
        R.text(frame, summary, (box.x + 12, box.y + box.h - 12), 0.42,
               R.BAD if failed else R.GOOD, 1)


# ---------------------------------------------------------------------------
# real desktop pointer
# ---------------------------------------------------------------------------

class RealPointer:
    """Warps the machine's own cursor to follow the emulated one.

    Best effort and purely cosmetic: without a display (or without python-xlib)
    every call is a no-op and the run is unaffected.
    """

    def __init__(self, width: int, height: int):
        self.ok = False
        self.scale = (1.0, 1.0)
        if not os.environ.get("DISPLAY"):
            return
        if os.environ.get("COLOURBOT_EMULATOR_POINTER", "1") == "0":
            return
        try:
            from Xlib import display as xdisplay
            self.display = xdisplay.Display()
            self.root = self.display.screen().root
            geometry = self.root.get_geometry()
            self.scale = (geometry.width / width, geometry.height / height)
            self.ok = True
        except Exception:                                  # pragma: no cover
            self.ok = False

    def move(self, x: int, y: int) -> None:
        if not self.ok:
            return
        try:
            self.root.warp_pointer(int(x * self.scale[0]), int(y * self.scale[1]))
            self.display.sync()
        except Exception:                                  # pragma: no cover
            self.ok = False


# ---------------------------------------------------------------------------
# viewer
# ---------------------------------------------------------------------------

class Viewer:
    """Renders the desktop on a thread: live window, mp4, snapshots."""

    def __init__(self, desktop: Desktop, out_dir: str, fps: int = DEFAULT_FPS,
                 live: Optional[bool] = None, record: bool = True):
        self.desktop = desktop
        self.out_dir = out_dir
        self.fps = fps
        self.live = self._can_display() if live is None else live
        self.record = record
        self.frames = 0
        self.snapshots: List[str] = []
        self.pointer = RealPointer(desktop.width, desktop.height)
        self._writer: Optional[cv2.VideoWriter] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._latest: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self.video_path = os.path.join(out_dir, "run.mp4")

    @staticmethod
    def _can_display() -> bool:
        return bool(os.environ.get("DISPLAY") or os.name == "nt")

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        if self.record:
            self._writer = cv2.VideoWriter(
                self.video_path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps,
                (self.desktop.width, self.desktop.height))
            if not self._writer.isOpened():                 # pragma: no cover
                self._writer = None
        if self.live:
            try:
                cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(WINDOW_NAME, 1600, 900)
            except cv2.error:                               # pragma: no cover
                self.live = False
        self._thread = threading.Thread(target=self._loop, name="emu-viewer",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self.live:
            try:
                cv2.destroyWindow(WINDOW_NAME)
                cv2.waitKey(1)
            except cv2.error:                               # pragma: no cover
                pass

    # -- the render loop ---------------------------------------------------
    def _loop(self) -> None:
        period = 1.0 / self.fps
        next_frame = time.monotonic()
        while not self._stop.is_set():
            frame = self.desktop.view_frame()
            with self._lock:
                self._latest = frame
            self.frames += 1
            bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            if self._writer is not None:
                self._writer.write(bgr)
            if self.live:
                cv2.imshow(WINDOW_NAME, bgr)
                cv2.waitKey(1)
            self.pointer.move(*self.desktop.mouse_position())
            next_frame += period
            time.sleep(max(0.0, next_frame - time.monotonic()))
            if next_frame < time.monotonic() - 1.0:        # fell behind, resync
                next_frame = time.monotonic()

    # -- stills ------------------------------------------------------------
    def snapshot(self, name: str) -> Optional[str]:
        """Save a PNG of the current view, named after the flow step."""
        with self._lock:
            frame = self._latest
        if frame is None:
            frame = self.desktop.view_frame()
        index = len(self.snapshots) + 1
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
        path = os.path.join(self.out_dir, f"{index:02d}-{safe}.png")
        cv2.imwrite(path, cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        self.snapshots.append(path)
        return path
