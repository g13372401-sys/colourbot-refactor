"""
server.py -- the emulator process: desktop + game + Discord + the IPC socket.
============================================================================

One `EmulatorServer` owns everything the script can observe:

    * a `Desktop` with the fake RuneLite window and the Discord window on it,
    * the `GameClient` that reacts to clicks and keys,
    * the `DiscordServer` the script's Discord layer logs into,
    * a unix socket the injected shims talk to (see protocol.py).

Requests are served one connection per thread.  The only long-lived connection
is the Discord gateway: after `discord_connect` that socket stays open and the
server pushes message frames down it, exactly like the real gateway.

The OCR endpoint lives here too.  There is no Tesseract binary in this
environment (and no network to fetch one), so `emulator/bin/tesseract`
forwards the image here and this module matches it against the vocabulary of
strings the game can actually render - by re-rendering each candidate through
the *same* pipeline the bot's `vision.ocr_mask()` used and comparing pixels.
That keeps the OCR honest: it reads the picture it was given, and it returns
"" when the picture is unreadable, which is what makes the chat watchdog and
the drop finder behave like they do in production.
"""

from __future__ import annotations

import os
import random
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from . import discord_server as DS
from . import game_client as GC
from . import protocol, render as R
from .desktop import Desktop, Window

GAME_ORIGIN = (60, 120)
DISCORD_ORIGIN = (1050, 120)

# Item names the OCR stand-in knows about.  Deliberately more than the one the
# route is looking for: a vocabulary of one would make "recognition" meaningless.
LEXICON = (
    "Enhanced crystal teleport seed",
    "Enhanced crystal weapon seed",
    "Crystal teleport seed",
    "Grubby key",
    "Dragon bones",
    "Big bones",
    "Ashes",
    "Coins",
    "Zenyte shard",
    "Blood shard",
    "Rune scimitar",
    "Super restore(4)",
    "Dodgy necklace",
    "Shark",
    "Teleport tab",
    GC.CHAT_PROMPT_TEXT,
)

OCR_MATCH_THRESHOLD = 0.55          # below this the stand-in reports "no text"


# ---------------------------------------------------------------------------
# OCR stand-in
# ---------------------------------------------------------------------------

class LexiconOCR:
    """Reads text by re-rendering candidates and comparing the pixels.

    `vision.ocr_mask()` hands Tesseract a binary text mask that has been
    dilated, upscaled 3x, blurred and inverted.  We undo nothing: each
    candidate phrase goes through the identical chain, and the two are compared
    as binary masks (intersection over union) after cropping both to their ink.
    """

    def __init__(self, phrases: Tuple[str, ...] = LEXICON):
        self.phrases = list(phrases)
        self._cache: Dict[str, np.ndarray] = {}

    # -- the pipeline vision.ocr_mask() uses -----------------------------
    @staticmethod
    def _to_ink(image: np.ndarray) -> Optional[np.ndarray]:
        """Dark-on-light image -> boolean 'this pixel is ink', cropped tight."""
        ink = image < 128
        if not ink.any():
            return None
        ys, xs = np.where(ink)
        return ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    def _render(self, phrase: str, upscale: int = 3, dilate: bool = True) -> np.ndarray:
        key = f"{phrase}|{upscale}|{dilate}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        img = (R.text_mask(phrase).astype(np.uint8) * 255)
        if dilate:
            img = cv2.dilate(img, np.ones((2, 2), np.uint8))
        if upscale > 1:
            img = cv2.resize(img, None, fx=upscale, fy=upscale,
                             interpolation=cv2.INTER_LINEAR)
        img = cv2.GaussianBlur(img, (3, 3), 0)
        img = 255 - img
        ink = self._to_ink(img)
        self._cache[key] = ink
        return ink

    # -- matching ---------------------------------------------------------
    @staticmethod
    def _score(target: np.ndarray, candidate: np.ndarray) -> float:
        th, tw = target.shape
        ch, cw = candidate.shape
        if min(th, tw, ch, cw) < 3:
            return 0.0
        aspect_t, aspect_c = tw / th, cw / ch
        if abs(aspect_t - aspect_c) > 0.35 * max(aspect_t, aspect_c):
            return 0.0                       # not even the right shape of line
        resized = cv2.resize(candidate.astype(np.uint8), (tw, th),
                             interpolation=cv2.INTER_NEAREST).astype(bool)
        union = np.logical_or(target, resized).sum()
        if not union:
            return 0.0
        return float(np.logical_and(target, resized).sum()) / float(union)

    def read(self, png: bytes, extra: List[str] = None) -> Tuple[str, float]:
        """Return (text, confidence) for one image handed to `tesseract`."""
        buffer = np.frombuffer(png, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        if image is None:
            return "", 0.0
        target = self._to_ink(image)
        if target is None:
            return "", 0.0

        best_text, best_score = "", 0.0
        for phrase in list(self.phrases) + list(extra or ()):
            score = self._score(target, self._render(phrase))
            if score > best_score:
                best_text, best_score = phrase, score
        if best_score < OCR_MATCH_THRESHOLD:
            return "", best_score
        return best_text, best_score


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------

class EmulatorServer:
    def __init__(self, socket_path: str, game_origin: Tuple[int, int] = GAME_ORIGIN,
                 discord_origin: Tuple[int, int] = DISCORD_ORIGIN,
                 seed: int = 1337):
        self.socket_path = socket_path
        self.desktop = Desktop()
        self.game = GC.GameClient(game_origin, rng=random.Random(seed))
        self.discord = DS.DiscordServer()
        self.ocr = LexiconOCR()

        self.game_window = self.desktop.add_window(Window(
            title=GC.WINDOW_TITLE, x=game_origin[0], y=game_origin[1],
            w=GC.WINDOW_W, h=GC.WINDOW_H, render=self._render_game,
            on_click=self._game_click, on_key=self._game_key))
        self.discord_window = self.desktop.add_window(Window(
            title=DS.WINDOW_TITLE, x=discord_origin[0], y=discord_origin[1],
            w=DS.WINDOW_W, h=DS.WINDOW_H, render=self.discord.render_window))

        self._sock: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self._key_waiters: Dict[str, List[threading.Event]] = {}
        self._connections: List[socket.socket] = []
        self.bot_pid: Optional[int] = None
        # Guarded: every request is served on its own thread, and `+=` is not
        # atomic - an unguarded counter silently under-reports.
        self._grab_lock = threading.Lock()
        self.grabs = 0
        self.window_queries = 0            # how often the fake wmctrl was run
        self.grab_boxes: set = set()       # the distinct rectangles asked for
        self.notes: List[Tuple[float, str, str]] = []
        self.relay_delay = 0.35            # the plugin is not instantaneous

    # ------------------------------------------------------------------
    # window plumbing
    # ------------------------------------------------------------------
    def _render_game(self) -> np.ndarray:
        self.game.update()
        return self.game.render_window()

    def _game_click(self, lx: int, ly: int, button: str, action: str) -> None:
        cx, cy = lx - GC.INSET_L, ly - GC.INSET_T
        if not (0 <= cx < GC.CANVAS_W and 0 <= cy < GC.CANVAS_H):
            if action == "press":
                self.desktop.log("click", f"{button} click on the window chrome",
                                 R.TEXT_DIM)
            return
        self.game.handle_click(cx, cy, button, action)
        if action == "press":
            what = self.game.last("click")
            detail = self._describe_last_action()
            self.desktop.log("click", f"canvas ({cx},{cy}) -> {detail}",
                             R.WARN if what else R.TEXT)

    def _game_key(self, key: str, action: str) -> None:
        self.game.handle_key(key, action)
        if action == "press":
            self.desktop.log("key", f"'{key}' -> {self._describe_last_action()}",
                             R.INFO)

    def _describe_last_action(self) -> str:
        """The most recent thing the game *did*, for the event log."""
        with self.game.lock:
            for obs in reversed(self.game.observations):
                if obs.kind not in ("click", "key"):
                    return f"{obs.kind} {obs.detail}".strip()
        return "no effect"

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        os.makedirs(os.path.dirname(self.socket_path) or ".", exist_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(32)
        self._sock.settimeout(0.5)
        self._spawn(self._accept_loop, "emu-accept")
        self._spawn(self._relay_loop, "emu-relay")

    def _spawn(self, target: Callable, name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    def stop(self) -> None:
        self._stop.set()
        self.discord.disconnect()
        for conn in list(self._connections):
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    # -- accept / serve ---------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._connections.append(conn)
            self._spawn(lambda c=conn: self._serve(c), "emu-conn")

    def _serve(self, conn: socket.socket) -> None:
        write_lock = threading.Lock()
        push: Optional[Callable[[dict], None]] = None

        def send(header: dict, payload: bytes = b"") -> None:
            with write_lock:
                protocol.send_frame(conn, header, payload)

        try:
            while not self._stop.is_set():
                try:
                    header, payload = protocol.recv_frame(conn)
                except (ConnectionError, OSError, ValueError):
                    break
                op = header.get("op")
                if op == "discord_connect":
                    push = send
                    send(self.discord.connect(header.get("token"),
                                              header.get("intents"), push))
                    continue
                try:
                    reply, blob = self._handle(op, header, payload)
                except Exception as exc:                      # never kill a client
                    reply, blob = {"error": f"{type(exc).__name__}: {exc}"}, b""
                send(reply, blob)
        finally:
            if push is not None:
                self.discord.disconnect(push)
            if conn in self._connections:
                self._connections.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # request handlers
    # ------------------------------------------------------------------
    def _handle(self, op: str, header: dict, payload: bytes) -> Tuple[dict, bytes]:
        handler = getattr(self, f"_op_{op}", None) if op else None
        if handler is None:
            return {"error": f"unknown op {op!r}"}, b""
        result = handler(header, payload)
        if isinstance(result, tuple):
            return result
        return (result or {"ok": True}), b""

    # -- input ------------------------------------------------------------
    def _op_mouse_move(self, header: dict, _payload: bytes) -> dict:
        x, y = self.desktop.move_mouse(header["x"], header["y"],
                                       header.get("source", "mouse"))
        return {"x": x, "y": y}

    def _op_mouse_pos(self, _header: dict, _payload: bytes) -> dict:
        x, y = self.desktop.mouse_position()
        return {"x": x, "y": y}

    def _op_mouse_button(self, header: dict, _payload: bytes) -> dict:
        self.desktop.press_button(header.get("button", "left"),
                                  header.get("action", "press"),
                                  header.get("source", "mouse"))
        return {"ok": True}

    def _op_mouse_scroll(self, header: dict, _payload: bytes) -> dict:
        self.desktop.log("scroll", f"wheel {header.get('dx')},{header.get('dy')}",
                         R.TEXT_DIM)
        return {"ok": True}

    def _op_key(self, header: dict, _payload: bytes) -> dict:
        key = header.get("key", "")
        action = header.get("action", "press")
        self.desktop.send_key(key, action, header.get("source", "keyboard"))
        if action == "press":
            self._wake_key_waiters(key)
        return {"ok": True}

    def _op_key_state(self, header: dict, _payload: bytes) -> dict:
        return {"pressed": self.desktop.key_is_pressed(header.get("key", ""))}

    def _op_wait_key(self, header: dict, _payload: bytes) -> dict:
        """`keyboard.wait('esc')` - the panic key thread parks here."""
        key = (header.get("key") or "").lower()
        event = threading.Event()
        self._key_waiters.setdefault(key, []).append(event)
        while not event.wait(0.25):
            if self._stop.is_set():
                return {"cancelled": True}
        return {"key": key}

    def _wake_key_waiters(self, key: str) -> None:
        for event in self._key_waiters.get((key or "").lower(), []):
            event.set()

    # -- screen -----------------------------------------------------------
    def _op_grab(self, header: dict, _payload: bytes) -> Tuple[dict, bytes]:
        bbox = header.get("bbox")
        image = self.desktop.grab(tuple(bbox) if bbox else None)
        with self._grab_lock:
            self.grabs += 1
            self.grab_boxes.add(tuple(int(v) for v in bbox) if bbox else None)
        height, width = image.shape[:2]
        return {"w": width, "h": height}, np.ascontiguousarray(image).tobytes()

    def _op_windows(self, _header: dict, _payload: bytes) -> dict:
        with self._grab_lock:
            self.window_queries += 1
        return {"windows": self.desktop.list_windows()}

    def grabbed_inside_game_window(self) -> bool:
        """True once something has screenshotted *within* the client window.

        Proof that the window manager's answer was actually used: a bot that
        never found the window would be grabbing the whole screen, or the wrong
        part of it.
        """
        window = self.game_window.box
        with self._grab_lock:
            boxes = list(self.grab_boxes)
        return any(box is not None
                   and window.contains(box[0], box[1])
                   and window.contains(box[2] - 1, box[3] - 1)
                   for box in boxes)

    def _op_note(self, header: dict, _payload: bytes) -> dict:
        kind, text = header.get("kind", "note"), header.get("text", "")
        self.notes.append((time.monotonic(), kind, text))
        self.desktop.log(kind, text, R.ACCENT)
        return {"ok": True}

    def _op_ping(self, _header: dict, _payload: bytes) -> dict:
        return {"ok": True, "t": time.monotonic()}

    # -- ocr --------------------------------------------------------------
    def _op_ocr(self, _header: dict, payload: bytes) -> dict:
        extra = [item.label for item in self.game.ground]
        text, score = self.ocr.read(payload, extra)
        self.desktop.log("ocr", f"{text!r} ({score:.2f})",
                         R.GOOD if text else R.WARN)
        return {"text": text, "score": score}

    # -- discord ----------------------------------------------------------
    def _op_discord_send(self, header: dict, _payload: bytes) -> dict:
        message = self.discord.bot_sends(header["channel_id"], header["content"])
        self.desktop.log("discord", f"bot -> {message.content[:70]}", R.INFO)
        return {"message": message.as_dict()}

    def _op_discord_fetch_user(self, header: dict, _payload: bytes) -> dict:
        user = self.discord.fetch_user(header["user_id"])
        if user is None:
            return {"error": f"404 Not Found (user {header['user_id']})"}
        return {"user": user.as_dict()}

    def _op_discord_create_dm(self, header: dict, _payload: bytes) -> dict:
        channel = self.discord.create_dm(header["user_id"])
        return {"channel": {"id": channel.id, "name": channel.name, "dm": True}}

    # ------------------------------------------------------------------
    # game chat -> Discord relay
    # ------------------------------------------------------------------
    def _relay_loop(self) -> None:
        """The RuneLite Discord plugin, mirroring game chat into the channel."""
        pending: List[Tuple[float, str]] = []
        while not self._stop.wait(0.1):
            for line in self.game.pop_relay():
                pending.append((time.monotonic() + self.relay_delay, line))
            now = time.monotonic()
            ready = [line for due, line in pending if due <= now]
            pending = [(due, line) for due, line in pending if due > now]
            for line in ready:
                self.discord.relay_says(line)
                self.desktop.log("relay", f"game -> discord: {line[:60]}", R.WARN)
