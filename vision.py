"""
vision.py -- everything that looks at the screen.
=================================================

Three groups of things live here:

    1. `grab()` / `Vision`      - screen capture that is *clipped to the game
                                  canvas*, plus the colour-blob finders that
                                  used to be copy-pasted in replay.py and
                                  redclick.py.
    2. `ChatWatcher`            - keeps the in-game chat box closed (it kills
                                  colour detection and OCR when it pops open).
    3. `DropFinder`             - finds the valuable drop lying on the floor by
                                  its ground-item label and local OCR.

Everything here works in CANVAS coordinates: (0,0) is the top-left pixel of the
rendered game area.  The old code worked in full-screen coordinates, which is
exactly why it sometimes clicked the taskbar or the terminal window.

Algorithm note
--------------
The blob finders are vectorised (numpy + OpenCV connected components) instead of
the original per-pixel python flood fill.  The *results* are identical:

    * 4-connectivity, exact colour match (config.COLOR_TOLERANCE == 0)
    * "largest" ties are broken by raster-scan order, like the old `>` test
    * a blob's reported centre is the centre of its bounding box, not its
      centre of mass - same as before

It is however ~100x faster, so the loops that used to be rate-limited by the
scan itself now use config.VISION["scan_interval_seconds"] to keep the original
cadence.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageGrab

import config
from core import Clock, GameWindow, InputController, Rect, Region, ControlSignal

LOG = logging.getLogger("colourbot.vision")

# pytesseract is only needed for the chat watchdog and the drop OCR; the rest of
# the bot works without it, so the import is soft.
try:
    import pytesseract
    if config.GENERAL.get("tesseract_cmd"):
        pytesseract.pytesseract.tesseract_cmd = config.GENERAL["tesseract_cmd"]
except Exception as _exc:                                  # pragma: no cover
    pytesseract = None
    LOG.debug("pytesseract unavailable: %s", _exc)


# ===========================================================================
# 1. Capture + colour blobs
# ===========================================================================

def grab(rect: Rect) -> np.ndarray:
    """Grab a screen rectangle as an RGB numpy array (H, W, 3)."""
    shot = ImageGrab.grab(bbox=rect.as_bbox())
    return np.asarray(shot.convert("RGB"))


def color_mask(img: np.ndarray, color: Sequence[int],
               tolerance: int = None) -> np.ndarray:
    """uint8 mask of the pixels that are `color`.

    tolerance 0 (the default, and what the old scripts did) means an exact RGB
    match; RuneLite highlight colours are flat so that is fine and fast.
    """
    tolerance = config.COLOR_TOLERANCE if tolerance is None else tolerance
    target = np.asarray(color, dtype=np.int16)
    if tolerance <= 0:
        mask = np.all(img == np.asarray(color, dtype=img.dtype), axis=2)
    else:
        mask = np.abs(img.astype(np.int16) - target).max(axis=2) <= tolerance
    return mask.astype(np.uint8)


def _components(mask: np.ndarray):
    """4-connected components, ordered by label (== raster scan order)."""
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=4)
    return count, stats


def _region_from_stats(name: str, stat) -> Region:
    x, y, w, h, area = (int(v) for v in stat[:5])
    return Region(name=name,
                  center=((x + x + w - 1) // 2, (y + y + h - 1) // 2),
                  x_bounds=(x, x + w - 1),
                  y_bounds=(y, y + h - 1),
                  area=area)


class Vision:
    """Colour detection, clipped to the game canvas."""

    def __init__(self, window: GameWindow):
        self.window = window

    # -- capture -----------------------------------------------------------
    def capture(self) -> np.ndarray:
        """Fresh grab of the game canvas (RGB)."""
        return grab(self.window.canvas)

    def crop(self, img: np.ndarray, rect: Rect) -> np.ndarray:
        """Canvas-relative crop, clipped to the image."""
        x0, y0 = max(0, rect.x), max(0, rect.y)
        x1, y1 = min(img.shape[1], rect.x + rect.w), min(img.shape[0], rect.y + rect.h)
        return img[y0:y1, x0:x1]

    # -- blob finders ------------------------------------------------------
    def largest_solid(self, color_name: str,
                      img: np.ndarray = None) -> Optional[Region]:
        """Biggest filled blob of a colour (old `find_largest_solid_region`)."""
        img = self.capture() if img is None else img
        mask = color_mask(img, config.COLORS[color_name])
        count, stats = _components(mask)
        best_label, best_area = None, 0
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area > best_area:                 # strict '>' keeps the first one
                best_label, best_area = label, area
        if best_label is None:
            LOG.info("No solid %s area found.", color_name)
            return None
        region = _region_from_stats(f"solid {color_name}", stats[best_label])
        LOG.debug("largest solid %s -> %s", color_name, region)
        return region

    def largest_boxed(self, color_name: str,
                      img: np.ndarray = None) -> Optional[Region]:
        """Bounding box of every *outline* pixel of a colour.

        Old `find_largest_boxed_region`: a pixel counts when it has at least one
        4-neighbour (inside the image) that is a different colour, i.e. the
        outline of a hollow highlight box.  The result is the bounding box over
        all of them, which is what the inventory/prayer anchors rely on.
        """
        img = self.capture() if img is None else img
        mask = color_mask(img, config.COLORS[color_name]).astype(bool)
        if not mask.any():
            LOG.info("No boxed %s area found.", color_name)
            return None

        # Pixels whose 4 neighbours are all the same colour are interior pixels.
        # Padding with True makes out-of-image neighbours count as "same
        # colour", which is how the original loop behaved.
        padded = np.pad(mask, 1, constant_values=True)
        interior = (padded[1:-1, 1:-1] & padded[:-2, 1:-1] & padded[2:, 1:-1]
                    & padded[1:-1, :-2] & padded[1:-1, 2:])
        edges = mask & ~interior
        if not edges.any():
            LOG.info("No boxed %s area found.", color_name)
            return None

        ys, xs = np.nonzero(edges)
        min_x, max_x, min_y, max_y = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        region = Region(name=f"boxed {color_name}",
                        center=((min_x + max_x) // 2, (min_y + max_y) // 2),
                        x_bounds=(min_x, max_x), y_bounds=(min_y, max_y),
                        area=int(edges.sum()))
        LOG.info("Largest boxed %s area found at center %s", color_name, region.center)
        return region

    def equal_largest_solids(self, color_name: str, img: np.ndarray = None,
                             tolerance: float = None) -> List[Region]:
        """All blobs at least `tolerance` as big as the biggest one.

        Used for the brew doses and the spare dodgy necklaces, where the old
        code picked a random one of the equally sized inventory icons.
        """
        tolerance = (config.VISION["equal_region_tolerance"]
                     if tolerance is None else tolerance)
        img = self.capture() if img is None else img
        mask = color_mask(img, config.COLORS[color_name])
        count, stats = _components(mask)
        if count <= 1:
            LOG.info("No solid %s areas found.", color_name)
            return []
        areas = [int(stats[label, cv2.CC_STAT_AREA]) for label in range(1, count)]
        threshold = max(areas) * tolerance
        regions = [_region_from_stats(f"solid {color_name}", stats[label])
                   for label, area in zip(range(1, count), areas)
                   if area >= threshold]
        LOG.info("Found %d approximately equal largest solid %s areas.",
                 len(regions), color_name)
        return regions

    # -- debugging ---------------------------------------------------------
    def save_annotated(self, img: np.ndarray, regions: Sequence[Region],
                       path: str) -> None:
        """Dump the capture with boxes drawn on it (--save-debug-image)."""
        canvas = img.copy()
        for region in regions:
            if region is None:
                continue
            rect = region.rect
            cv2.rectangle(canvas, (rect.x, rect.y), (rect.right, rect.bottom),
                          (0, 255, 0), 2)
            cv2.putText(canvas, region.name, (rect.x, max(12, rect.y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        Image.fromarray(canvas).save(path)
        LOG.info("wrote debug image %s", path)


# ===========================================================================
# 2. Small OCR helpers
# ===========================================================================

def _normalise(text: str) -> str:
    """Lower-case, letters only - OCR loves to invent punctuation."""
    return re.sub(r"[^a-z]", "", text.lower())


def fuzzy_contains(needle: str, haystack: str) -> float:
    """How well does `needle` appear inside `haystack`?  0.0 .. 1.0.

    Slides a window the size of the needle over the haystack, because the
    ground-item label also contains the stack size and the gp value
    ("Enhanced crystal teleport seed (2) (6.65M gp)") and the OCR mangles the
    tail far more often than the item name.
    """
    n, h = _normalise(needle), _normalise(haystack)
    if not n or not h:
        return 0.0
    if n in h:
        return 1.0
    best = difflib.SequenceMatcher(None, n, h).ratio()
    for i in range(0, max(1, len(h) - len(n) + 1)):
        best = max(best, difflib.SequenceMatcher(None, n, h[i:i + len(n)]).ratio())
    return best


def ocr_mask(mask: np.ndarray, upscale: int = 3, dilate: bool = True,
             psm: int = 7) -> str:
    """OCR a binary text mask (white text on black) and return the raw text.

    The in-game font is thin and small, so we thicken it, upscale it and hand
    tesseract black-on-white, which is what it is trained for.  Everything runs
    locally - no cloud OCR (the old code even had a "Google Cloud Vision"
    comment left in it, but never called it).
    """
    if pytesseract is None:
        return ""
    img = (mask.astype(np.uint8) * 255)
    if img.size == 0:
        return ""
    if dilate:
        img = cv2.dilate(img, np.ones((2, 2), np.uint8))
    if upscale > 1:
        img = cv2.resize(img, None, fx=upscale, fy=upscale,
                         interpolation=cv2.INTER_LINEAR)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    img = 255 - img                                    # black text, white paper
    try:
        return pytesseract.image_to_string(
            Image.fromarray(img), config=f"--psm {psm}").strip()
    except Exception as exc:                            # tesseract not installed
        LOG.warning("OCR failed (%s) - is the Tesseract binary installed?", exc)
        return ""


def ocr_available() -> bool:
    """True when pytesseract *and* the tesseract binary are usable."""
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# ===========================================================================
# 3. Chat watchdog
# ===========================================================================

class ChatWatcher:
    """Keeps the in-game chat box closed.

    Why this exists: RuneLite's screenshot hotkey ('insert', which the bot
    presses for the Discord screenshots) also pops the chat open a couple of
    seconds later.  The chat draws a translucent black gradient plus text over
    the bottom-left of the game area, which

        * hides parts of the red target region  -> "No solid red area found."
        * hides / recolours the ground-item label -> the drop is never picked up

    Closing it is one press of '`'.  We confirm visually (the string
    "Press Enter to Chat" sits right above the All/Game/Private buttons while
    the chat is up), press once more if needed, and fall back to clicking the
    'All' button, which toggles the same thing.
    """

    def __init__(self, vision: Vision, controller: InputController, clock: Clock,
                 cfg: dict = None):
        self.vision = vision
        self.input = controller
        self.clock = clock
        self.cfg = cfg or config.CHAT
        self._lock = threading.RLock()
        self._guard_thread: Optional[threading.Thread] = None
        self._guard_stop = threading.Event()
        self._warned = False

    # -- detection ---------------------------------------------------------
    def is_open(self, img: np.ndarray = None) -> Optional[bool]:
        """True/False, or None when we cannot tell (no OCR available)."""
        if not self.cfg["enabled"]:
            return False
        if not ocr_available():
            if not self._warned:
                LOG.warning("Tesseract not available - the chat watchdog is "
                            "disabled (install tesseract-ocr to enable it)")
                self._warned = True
            return None

        rect = Rect(*self.cfg["prompt_rect"])
        img = self.vision.capture() if img is None else img
        strip = self.vision.crop(img, rect)
        if strip.size == 0:
            return None
        # the prompt is near-white text on the dark chat background
        mask = (strip.min(axis=2) >= self.cfg["prompt_white_threshold"])
        if mask.sum() < 12:                       # nothing bright -> no prompt
            return False
        text = ocr_mask(mask, upscale=3, dilate=True, psm=7)
        score = fuzzy_contains(self.cfg["prompt_text"], text)
        LOG.debug("chat prompt OCR %r (score %.2f)", text, score)
        return score >= self.cfg["prompt_match_ratio"]

    # -- closing -----------------------------------------------------------
    def ensure_closed(self, reason: str = "") -> bool:
        """Close the chat if it is open.  Returns True when it is closed."""
        if not self.cfg["enabled"]:
            return True
        with self._lock:
            state = self.is_open()
            if state is None:
                return True                       # cannot check -> leave it alone
            if not state:
                return True

            LOG.info("chat window is open%s - closing it",
                     f" ({reason})" if reason else "")
            for attempt in range(1, self.cfg["max_toggle_attempts"] + 1):
                self.input.tap(self.cfg["toggle_key"], hold="chat.key_hold",
                               after="chat.after_toggle",
                               note=f"close chat, attempt {attempt}")
                if not self.is_open():
                    LOG.info("chat closed after %d '%s' press(es)", attempt,
                             self.cfg["toggle_key"])
                    return True

            # Fallback: the 'All' button does exactly the same as the '`' key.
            LOG.warning("chat still open - falling back to the 'All' button")
            rect = Rect(*self.cfg["all_button_rect"])
            self.input.click_region(Region("chat 'All' button", rect.center,
                                           (rect.x, rect.right),
                                           (rect.y, rect.bottom)))
            self.clock.wait("chat.after_all_click")
            if not self.is_open():
                LOG.info("chat closed by the 'All' button")
                return True
            LOG.error("could not close the chat window - detection may suffer")
            return False

    # -- background guard --------------------------------------------------
    def start_guard(self) -> None:
        """Poll every `check_interval_seconds` while the common case runs."""
        if not self.cfg["enabled"] or self._guard_thread is not None:
            return
        self._guard_stop.clear()
        self._guard_thread = threading.Thread(target=self._guard_loop,
                                              name="chat-guard", daemon=True)
        self._guard_thread.start()
        LOG.info("chat watchdog running every %.0fs", self.cfg["check_interval_seconds"])

    def stop_guard(self) -> None:
        self._guard_stop.set()
        self._guard_thread = None

    def _guard_loop(self) -> None:
        interval = self.cfg["check_interval_seconds"]
        while not self._guard_stop.wait(interval):
            try:
                self.ensure_closed("watchdog")
            except ControlSignal:                 # kill/restart while waiting
                return
            except Exception as exc:              # never let the guard kill the run
                LOG.warning("chat watchdog hiccup: %s", exc)


# ===========================================================================
# 4. Valuable-drop finder
# ===========================================================================

@dataclass
class GroundLabel:
    """One ground-item label ("Enhanced crystal teleport seed (2) (6.65M gp)")."""
    rect: Rect
    text: str
    score: float

    @property
    def click_point(self) -> Tuple[int, int]:
        """Where to click to take the pile.

        RuneLite centres the label horizontally on the tile the items are on and
        draws it right over the sprite, so the item is at the label's horizontal
        centre and a handful of pixels below its vertical centre (measured on
        valuable_drop.png: label centre y=697, sprite centre y=702).
        """
        cx, cy = self.rect.center
        return cx, cy + config.DROP["click_offset_y"]


class DropFinder:
    """Locates the valuable drop on the floor.

    The old implementation clicked a fixed magenta box that RuneLite paints on
    the player's own tile - which only works while the player never moves.  We
    instead look for the ground-item label the Ground Items plugin paints in its
    highlight colour, confirm the item name with local OCR and click the pile
    itself.
    """

    def __init__(self, vision: Vision, item_name: str, cfg: dict = None):
        self.vision = vision
        self.item_name = item_name
        self.cfg = cfg or config.DROP

    def find_labels(self, img: np.ndarray = None) -> List[GroundLabel]:
        """Every highlight-coloured text line currently on the canvas."""
        cfg = self.cfg
        img = self.vision.capture() if img is None else img
        mask = color_mask(img, cfg["label_color"], cfg["label_tolerance"])
        if not mask.any():
            return []

        # Glue the individual glyphs of one line together so each connected
        # component is a whole label.
        kw, kh = cfg["line_kernel"]
        glued = cv2.dilate(mask, np.ones((kh, kw), np.uint8))
        count, _labels, stats, _cent = cv2.connectedComponentsWithStats(
            glued, connectivity=8)

        labels: List[GroundLabel] = []
        for label in range(1, count):
            x, y, w, h, _area = (int(v) for v in stats[label, :5])
            piece = mask[y:y + h, x:x + w]
            pixels = int(piece.sum())
            if (pixels < cfg["min_label_pixels"] or w < cfg["min_label_width"]
                    or h > cfg["max_label_height"]):
                continue
            text = ""
            score = 0.0
            if cfg["ocr_enabled"]:
                text = ocr_mask(piece.astype(bool), upscale=cfg["ocr_upscale"],
                                dilate=cfg["ocr_dilate"], psm=7)
                score = fuzzy_contains(self.item_name, text)
            labels.append(GroundLabel(Rect(x, y, w, h), text, score))
            LOG.info("ground label at %s: %r (match %.2f)", Rect(x, y, w, h),
                     text, score)
        return labels

    def find_drop(self, img: np.ndarray = None) -> Optional[GroundLabel]:
        """The label that belongs to our valuable drop, or None."""
        labels = self.find_labels(img)
        if not labels:
            return None

        cfg = self.cfg
        if not cfg["ocr_enabled"] or not ocr_available():
            LOG.warning("OCR disabled/unavailable - taking the biggest ground "
                        "label as the drop")
            return max(labels, key=lambda lbl: lbl.rect.w)

        good = [lbl for lbl in labels if lbl.score >= cfg["ocr_match_ratio"]]
        if good:
            best = max(good, key=lambda lbl: lbl.score)
            LOG.info("drop %r confirmed by OCR (%.2f) at %s", self.item_name,
                     best.score, best.click_point)
            return best

        if len(labels) == 1 and cfg["click_unverified_single_label"]:
            LOG.warning("OCR could not confirm %r (best guess %r, %.2f) but "
                        "there is exactly one ground label - going for it",
                        self.item_name, labels[0].text, labels[0].score)
            return labels[0]

        LOG.warning("%d ground labels found, none of them matched %r",
                    len(labels), self.item_name)
        return None
