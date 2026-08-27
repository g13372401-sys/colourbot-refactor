"""
render.py -- drawing helpers for the emulator.
==============================================

Two things live here.

1. A tiny drawing toolkit (panels, hollow highlight boxes, text, the mouse
   cursor, click ripples).  Nothing fancy - numpy + OpenCV primitives.

2. `text_mask()`, the single place where "game text" is turned into pixels.
   The fake Tesseract in `emulator/bin/tesseract` renders its candidate phrases
   through the *same* function, which is what lets a constrained-vocabulary OCR
   read the emulator's chat prompt and ground-item labels (see EMULATOR.md,
   "Why there is a fake tesseract").

A note on colours
-----------------
`config.COLORS` are matched *exactly* (COLOR_TOLERANCE == 0) and the drop label
colour is matched with a tolerance of 40, so any accidental use of one of those
colours anywhere on the game canvas would be picked up by the bot's vision as a
real game element.  `RESERVED` below lists them and `audit_canvas()` checks a
rendered frame against the elements that are *supposed* to be there - the
emulator runs that check at start-up, so a careless palette tweak fails loudly
instead of producing a mysterious flaky run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

Color = Tuple[int, int, int]

# ---------------------------------------------------------------------------
# palette
# ---------------------------------------------------------------------------
# Everything the emulator paints that is *not* a highlight the bot looks for.
# Deliberately kept away from the reserved colours below.

DESKTOP_BG = (38, 46, 58)
DESKTOP_GRID = (46, 55, 69)
WINDOW_CHROME = (30, 30, 30)          # config.GAME_WINDOW["chrome_color"]
TITLE_TEXT = (208, 212, 218)
PANEL_BG = (32, 37, 45)
PANEL_EDGE = (72, 82, 98)
TEXT = (214, 219, 226)
TEXT_DIM = (140, 150, 165)
TEXT_BRIGHT = (238, 240, 244)         # never pure white: white is a game colour
GOOD = (86, 196, 128)
WARN = (232, 178, 74)
BAD = (226, 96, 96)
INFO = (108, 168, 232)
ACCENT = (150, 122, 226)

# Reserved: exact colours config.COLORS asks the vision layer to look for.
RESERVED: Dict[str, Color] = {
    "red": (255, 0, 0),
    "yellow": (255, 250, 0),
    "blue": (0, 67, 255),
    "purple": (231, 0, 255),
    "orange": (255, 154, 0),
    "cyan": (0, 255, 241),
    "white": (255, 255, 255),
    "black": (0, 0, 0),
}
DROP_LABEL = (255, 102, 178)
DROP_LABEL_TOLERANCE = 40


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Box:
    """x/y/w/h rectangle with the handful of derived values we keep needing."""
    x: int
    y: int
    w: int
    h: int

    @property
    def x1(self) -> int:
        return self.x + self.w - 1

    @property
    def y1(self) -> int:
        return self.y + self.h - 1

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    @property
    def center(self) -> Tuple[int, int]:
        return self.cx, self.cy

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x <= self.x1 and self.y <= y <= self.y1

    def inset(self, d: int) -> "Box":
        return Box(self.x + d, self.y + d, max(1, self.w - 2 * d),
                   max(1, self.h - 2 * d))

    def moved(self, dx: int, dy: int) -> "Box":
        return Box(self.x + dx, self.y + dy, self.w, self.h)

    def centred(self, cx: int, cy: int) -> "Box":
        return Box(cx - self.w // 2, cy - self.h // 2, self.w, self.h)


def new_surface(w: int, h: int, color: Color = (0, 0, 0)) -> np.ndarray:
    surface = np.empty((h, w, 3), dtype=np.uint8)
    surface[:, :] = color
    return surface


def blit(dst: np.ndarray, src: np.ndarray, x: int, y: int) -> None:
    """Copy `src` onto `dst` at (x, y), clipped to `dst`."""
    dh, dw = dst.shape[:2]
    sh, sw = src.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(dw, x + sw), min(dh, y + sh)
    if x0 >= x1 or y0 >= y1:
        return
    dst[y0:y1, x0:x1] = src[y0 - y:y1 - y, x0 - x:x1 - x]


def blend_rect(img: np.ndarray, box: Box, color: Color, alpha: float) -> None:
    """Alpha-blend a filled rectangle (used for the translucent chat box)."""
    x0, y0 = max(0, box.x), max(0, box.y)
    x1, y1 = min(img.shape[1], box.x + box.w), min(img.shape[0], box.y + box.h)
    if x0 >= x1 or y0 >= y1:
        return
    patch = img[y0:y1, x0:x1].astype(np.float32)
    tint = np.asarray(color, dtype=np.float32)
    img[y0:y1, x0:x1] = (patch * (1.0 - alpha) + tint * alpha).astype(np.uint8)


def fill(img: np.ndarray, box: Box, color: Color) -> None:
    cv2.rectangle(img, (box.x, box.y), (box.x1, box.y1), color, -1)


def outline(img: np.ndarray, box: Box, color: Color, thickness: int = 1) -> None:
    cv2.rectangle(img, (box.x, box.y), (box.x1, box.y1), color, thickness)


def hollow_highlight(img: np.ndarray, box: Box, color: Color,
                     thickness: int = 2) -> None:
    """A RuneLite style *boxed* highlight: an outline, nothing in the middle.

    `vision.Vision.largest_boxed` finds these by looking for colour pixels that
    have a neighbour of a different colour, so the inside must stay empty.
    """
    cv2.rectangle(img, (box.x, box.y), (box.x1, box.y1), color, thickness)


def panel(img: np.ndarray, box: Box, bg: Color = PANEL_BG,
          edge: Optional[Color] = PANEL_EDGE) -> None:
    fill(img, box, bg)
    if edge is not None:
        outline(img, box, edge, 1)


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

UI_FONT = cv2.FONT_HERSHEY_DUPLEX
MONO_FONT = cv2.FONT_HERSHEY_PLAIN

# The style the *game* renders its OCR-able strings in (chat prompt, ground
# item labels).  The fake tesseract re-renders candidate phrases with exactly
# these settings, so keep them in one place.
OCR_FONT = cv2.FONT_HERSHEY_DUPLEX
OCR_SCALE = 0.42
OCR_THICKNESS = 1


def text(img: np.ndarray, message: str, org: Tuple[int, int],
         scale: float = 0.42, color: Color = TEXT, thickness: int = 1,
         font: int = UI_FONT, shadow: bool = False) -> None:
    """Draw a line of text with its *baseline-left* at `org`."""
    if shadow:
        cv2.putText(img, message, (org[0] + 1, org[1] + 1), font, scale,
                    (18, 20, 24), thickness, cv2.LINE_AA)
    cv2.putText(img, message, org, font, scale, color, thickness, cv2.LINE_AA)


def text_size(message: str, scale: float = 0.42, thickness: int = 1,
              font: int = UI_FONT) -> Tuple[int, int, int]:
    """(width, height, baseline) of a rendered line."""
    (w, h), baseline = cv2.getTextSize(message, font, scale, thickness)
    return w, h, baseline


def text_mask(message: str, scale: float = OCR_SCALE,
              thickness: int = OCR_THICKNESS, font: int = OCR_FONT,
              pad: int = 2) -> np.ndarray:
    """Render `message` as a tight binary mask (True = glyph pixel).

    This is the shared ground truth between the game renderer and the OCR
    stand-in: the game paints the mask in the chat/label colour, and the fake
    tesseract renders the same mask for each candidate phrase and correlates.
    """
    w, h, baseline = text_size(message, scale, thickness, font)
    canvas = np.zeros((h + baseline + 2 * pad, w + 2 * pad), dtype=np.uint8)
    cv2.putText(canvas, message, (pad, pad + h), font, scale, 255, thickness,
                cv2.LINE_AA)
    return canvas > 96


def draw_text_colored(img: np.ndarray, message: str, org: Tuple[int, int],
                      color: Color, scale: float = OCR_SCALE,
                      thickness: int = OCR_THICKNESS,
                      font: int = OCR_FONT) -> Box:
    """Paint `text_mask()` in a flat colour at `org` (top-left).

    Flat, because the ground-item label has to survive an exact-ish colour
    match: anti-aliased edges would be dropped by `color_mask` and eat the
    glyphs.  Returns the box the text occupies.
    """
    mask = text_mask(message, scale, thickness, font)
    h, w = mask.shape
    x, y = org
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(img.shape[1], x + w), min(img.shape[0], y + h)
    if x0 < x1 and y0 < y1:
        sub = mask[y0 - y:y1 - y, x0 - x:x1 - x]
        region = img[y0:y1, x0:x1]
        region[sub] = color
    return Box(x, y, w, h)


# ---------------------------------------------------------------------------
# cursor + input effects (drawn on the OVERLAY layer, never on the captured one)
# ---------------------------------------------------------------------------

_CURSOR_SHAPE = np.array([[0, 0], [0, 16], [4, 12], [7, 18], [10, 17],
                          [7, 11], [12, 11]], dtype=np.int32)


def draw_cursor(img: np.ndarray, x: int, y: int, pressed: bool = False) -> None:
    """The classic arrow pointer, white with a black outline."""
    shape = _CURSOR_SHAPE + np.array([x, y])
    cv2.fillPoly(img, [shape], (250, 250, 250), cv2.LINE_AA)
    cv2.polylines(img, [shape], True, (12, 12, 12), 1, cv2.LINE_AA)
    if pressed:
        cv2.circle(img, (x, y), 9, (255, 214, 64), 2, cv2.LINE_AA)


def draw_click_ripple(img: np.ndarray, x: int, y: int, age: float,
                      ttl: float = 0.55, color: Color = (255, 214, 64)) -> None:
    """Expanding ring + crosshair so a click is impossible to miss."""
    t = max(0.0, min(1.0, age / ttl))
    radius = int(8 + 30 * t)
    fade = 1.0 - t
    tint = tuple(int(c * fade + 30 * (1 - fade)) for c in color)
    cv2.circle(img, (x, y), radius, tint, 2, cv2.LINE_AA)
    cv2.line(img, (x - 12, y), (x + 12, y), tint, 1, cv2.LINE_AA)
    cv2.line(img, (x, y - 12), (x, y + 12), tint, 1, cv2.LINE_AA)


def draw_key_badge(img: np.ndarray, keys: Sequence[str], x: int, y: int) -> None:
    """A row of 'keycaps' showing what the bot is pressing right now."""
    cursor_x = x
    for key in keys:
        label = key.upper()
        w, h, _ = text_size(label, 0.5, 1, UI_FONT)
        box = Box(cursor_x, y, w + 18, h + 14)
        fill(img, box, (250, 214, 90))
        outline(img, box, (120, 92, 20), 1)
        text(img, label, (box.x + 9, box.y1 - 7), 0.5, (40, 34, 12), 1)
        cursor_x += box.w + 6


# ---------------------------------------------------------------------------
# misc widgets
# ---------------------------------------------------------------------------

def progress_bar(img: np.ndarray, box: Box, fraction: float, color: Color,
                 bg: Color = (54, 60, 72)) -> None:
    fill(img, box, bg)
    width = int(box.w * max(0.0, min(1.0, fraction)))
    if width > 0:
        fill(img, Box(box.x, box.y, width, box.h), color)
    outline(img, box, (86, 94, 110), 1)


def wrap(message: str, width: int, scale: float = 0.4,
         thickness: int = 1, font: int = UI_FONT) -> List[str]:
    """Greedy word wrap measured in real rendered pixels."""
    words = message.split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if text_size(candidate, scale, thickness, font)[0] <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


# ---------------------------------------------------------------------------
# colour audit
# ---------------------------------------------------------------------------

def find_blobs(img: np.ndarray, color: Color, tolerance: int = 0) -> List[Box]:
    """Same connected-component pass the bot's vision layer runs."""
    if tolerance <= 0:
        mask = np.all(img == np.asarray(color, dtype=img.dtype), axis=2)
    else:
        mask = (np.abs(img.astype(np.int16) - np.asarray(color, dtype=np.int16))
                .max(axis=2) <= tolerance)
    mask = mask.astype(np.uint8)
    if not mask.any():
        return []
    count, _labels, stats, _cent = cv2.connectedComponentsWithStats(
        mask, connectivity=4)
    boxes = []
    for label in range(1, count):
        x, y, w, h, _area = (int(v) for v in stats[label, :5])
        boxes.append(Box(x, y, w, h))
    return boxes


def audit_canvas(img: np.ndarray, allowed: Dict[str, Iterable[Box]]) -> List[str]:
    """Report reserved-colour pixels that are not part of a known element.

    `allowed` maps a colour name to the boxes that are *meant* to contain it.
    Anything outside those boxes would be a second "largest red blob" as far as
    the bot is concerned, i.e. a bug in the emulator's palette.
    """
    problems: List[str] = []
    for name, color in RESERVED.items():
        expected = list(allowed.get(name, ()))
        for blob in find_blobs(img, color):
            if not any(box.contains(blob.cx, blob.cy) for box in expected):
                problems.append(
                    f"stray {name} pixels at ({blob.x},{blob.y}) {blob.w}x{blob.h}")
    for blob in find_blobs(img, DROP_LABEL, DROP_LABEL_TOLERANCE):
        expected = list(allowed.get("drop_label", ()))
        if not any(box.contains(blob.cx, blob.cy) for box in expected):
            problems.append(
                f"stray drop-label pixels at ({blob.x},{blob.y}) {blob.w}x{blob.h}")
    return problems
