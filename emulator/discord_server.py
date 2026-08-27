"""
discord_server.py -- the Discord emulator.
==========================================

Everything `discord_bot.py` talks to, minus the internet:

    * a guild with one text channel (#bot-control) and a DM channel,
    * three participants - the operator (a human typing commands), the RuneLite
      Discord relay (the plugin that mirrors game chat into the channel), and
      the bot itself,
    * a gateway: once the script logs in, every message posted here is pushed at
      it as a MESSAGE_CREATE-shaped frame, including the echo of its own sends,
      which is what real Discord does and what `on_message` depends on
      (`if message.author == bot.user: return`, and the loot-spam counter that
      counts *every* line in the channel).

It also draws itself, because the engineer running the test has to be able to
watch the conversation: injected game messages, operator commands, and the
bot's replies, in order, with who said what.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

from . import render as R
from .render import Box

WINDOW_W, WINDOW_H = 800, 680
WINDOW_TITLE = "Discord - #bot-control"

# ids are stable so the scenario/expectations can refer to them
BOT_ID = 100_000_000_000_000_001
OPERATOR_ID = 100_000_000_000_000_002
RELAY_ID = 100_000_000_000_000_003
BYSTANDER_ID = 100_000_000_000_000_004
CHANNEL_ID = 200_000_000_000_000_001
DM_CHANNEL_ID = 200_000_000_000_000_002

BOT_TOKEN = "EMULATOR.FAKE.TOKEN"          # never leaves this machine

# palette per author
BOT_COLOR = (150, 190, 255)
OPERATOR_COLOR = (120, 226, 168)
RELAY_COLOR = (232, 186, 96)
BYSTANDER_COLOR = (206, 150, 226)
SYSTEM_COLOR = (140, 150, 165)


@dataclass
class DiscordUser:
    id: int
    name: str
    color: tuple
    bot: bool = False

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "bot": self.bot}


@dataclass
class DiscordMessage:
    id: int
    author: DiscordUser
    channel_id: int
    channel_name: str
    content: str
    at: float
    dm: bool = False

    def as_dict(self) -> dict:
        return {"id": self.id, "content": self.content,
                "author": self.author.as_dict(),
                "channel": {"id": self.channel_id, "name": self.channel_name,
                            "dm": self.dm, "guild_id": 1,
                            "guild_name": "emulated-guild"},
                "created_at": self.at}


@dataclass
class DiscordChannel:
    id: int
    name: str
    dm: bool = False
    messages: List[DiscordMessage] = field(default_factory=list)


class DiscordServer:
    """The channel, its participants and the gateway push."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self._ids = itertools.count(300_000_000_000_000_001)

        self.bot_user = DiscordUser(BOT_ID, "colourbot", BOT_COLOR, bot=True)
        self.operator = DiscordUser(OPERATOR_ID, "operator", OPERATOR_COLOR)
        self.relay = DiscordUser(RELAY_ID, "RuneLite-relay", RELAY_COLOR, bot=True)
        self.bystander = DiscordUser(BYSTANDER_ID, "clanmate", BYSTANDER_COLOR)
        self.users: Dict[int, DiscordUser] = {
            u.id: u for u in (self.bot_user, self.operator, self.relay,
                              self.bystander)}

        self.channel = DiscordChannel(CHANNEL_ID, "bot-control")
        self.dm_channel = DiscordChannel(DM_CHANNEL_ID, "operator (DM)", dm=True)
        self.channels: Dict[int, DiscordChannel] = {
            self.channel.id: self.channel, self.dm_channel.id: self.dm_channel}

        self.listeners: List[Callable[[dict], None]] = []
        self.connected_at: Optional[float] = None
        self.login_token: Optional[str] = None
        self.intents: List[str] = []
        self.typing: Optional[tuple] = None            # (user, until)
        self.sent_by_bot = 0
        self.injected = 0
        self.dropped_before_login: List[str] = []

    # ------------------------------------------------------------------
    # gateway
    # ------------------------------------------------------------------
    def connect(self, token: str, intents: List[str],
                push: Callable[[dict], None]) -> dict:
        """A shim's `bot.start()` landed here.  Returns the READY payload."""
        with self.lock:
            if not token:
                return {"error": "Improper token has been passed."}
            self.login_token = token
            self.intents = list(intents or ())
            self.connected_at = self.clock()
            self.listeners.append(push)
            return {"t": "ready",
                    "user": self.bot_user.as_dict(),
                    "channels": [{"id": self.channel.id,
                                  "name": self.channel.name}],
                    "session_id": "emulator-session"}

    def disconnect(self, push: Callable[[dict], None] = None) -> None:
        with self.lock:
            if push is None:
                self.listeners.clear()
            elif push in self.listeners:
                self.listeners.remove(push)
            self.connected_at = None

    @property
    def online(self) -> bool:
        return self.connected_at is not None

    def _dispatch(self, message: DiscordMessage) -> None:
        frame = {"t": "message", "message": message.as_dict()}
        with self.lock:
            listeners = list(self.listeners)
        for push in listeners:
            try:
                push(frame)
            except Exception:                          # a dead gateway is fine
                pass

    # ------------------------------------------------------------------
    # posting
    # ------------------------------------------------------------------
    def post(self, author: DiscordUser, content: str,
             channel: DiscordChannel = None) -> DiscordMessage:
        """Put a line in the channel and push it at the logged-in bot."""
        channel = channel or self.channel
        with self.lock:
            message = DiscordMessage(next(self._ids), author, channel.id,
                                     channel.name, content, self.clock(),
                                     channel.dm)
            channel.messages.append(message)
            if author is not self.bot_user:
                self.injected += 1
            else:
                self.sent_by_bot += 1
            if not self.online and author is not self.bot_user:
                self.dropped_before_login.append(content)
            if self.typing and self.typing[0] is author:
                self.typing = None
        self._dispatch(message)
        return message

    # -- the three ways a line gets into the channel --------------------
    def operator_says(self, content: str) -> DiscordMessage:
        return self.post(self.operator, content)

    def relay_says(self, content: str) -> DiscordMessage:
        """A game chat line mirrored in by the RuneLite Discord plugin."""
        return self.post(self.relay, content)

    def bystander_says(self, content: str) -> DiscordMessage:
        return self.post(self.bystander, content)

    def bot_sends(self, channel_id: int, content: str) -> DiscordMessage:
        channel = self.channels.get(int(channel_id), self.channel)
        return self.post(self.bot_user, content, channel)

    def set_typing(self, user: DiscordUser, seconds: float = 1.2) -> None:
        with self.lock:
            self.typing = (user, self.clock() + seconds)

    # -- rpc used by the shim -------------------------------------------
    def fetch_user(self, user_id: int) -> Optional[DiscordUser]:
        user = self.users.get(int(user_id))
        if user is None and int(user_id) == 0:
            # config.DISCORD["user_id"] is 0 out of the box; the real API 404s
            # on that, and discord_bot.py logs a warning and carries on.
            return None
        return user

    def create_dm(self, user_id: int) -> DiscordChannel:
        user = self.users.get(int(user_id), self.operator)
        self.dm_channel.name = f"{user.name} (DM)"
        return self.dm_channel

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def render_window(self) -> np.ndarray:
        surface = R.new_surface(WINDOW_W, WINDOW_H, (49, 51, 56))
        R.fill(surface, Box(0, 0, WINDOW_W, 26), (32, 34, 37))
        R.text(surface, WINDOW_TITLE, (10, 18), 0.44, R.TITLE_TEXT, 1)
        for index, tint in enumerate(((150, 74, 74), (92, 96, 104), (92, 96, 104))):
            R.fill(surface, Box(WINDOW_W - 20 - index * 22, 8, 12, 10), tint)

        body = Box(0, 26, WINDOW_W, WINDOW_H - 26)
        R.fill(surface, body, (49, 51, 56))
        self._draw_sidebar(surface, Box(0, 26, 168, body.h))
        self._draw_header(surface, Box(168, 26, WINDOW_W - 168, 34))
        self._draw_messages(surface, Box(168, 60, WINDOW_W - 168, body.h - 96))
        self._draw_footer(surface, Box(168, WINDOW_H - 36, WINDOW_W - 168, 36))
        return surface

    def _draw_sidebar(self, img: np.ndarray, box: Box) -> None:
        R.fill(img, box, (43, 45, 49))
        R.text(img, "emulated-guild", (box.x + 12, box.y + 24), 0.46,
               R.TEXT_BRIGHT, 1)
        R.fill(img, Box(box.x + 8, box.y + 38, box.w - 16, 1), (58, 60, 66))

        R.text(img, "TEXT CHANNELS", (box.x + 12, box.y + 60), 0.34, R.TEXT_DIM, 1)
        R.fill(img, Box(box.x + 6, box.y + 68, box.w - 12, 24), (57, 60, 66))
        R.text(img, f"# {self.channel.name}", (box.x + 14, box.y + 85), 0.44,
               R.TEXT_BRIGHT, 1)
        R.text(img, "DIRECT MESSAGES", (box.x + 12, box.y + 120), 0.34,
               R.TEXT_DIM, 1)
        R.text(img, f"@ {self.dm_channel.name}", (box.x + 14, box.y + 142), 0.42,
               R.TEXT_DIM, 1)
        count = len(self.dm_channel.messages)
        if count:
            R.fill(img, Box(box.x + box.w - 34, box.y + 130, 20, 15), (218, 84, 84))
            R.text(img, str(count), (box.x + box.w - 28, box.y + 142), 0.4,
                   R.TEXT_BRIGHT, 1)

        R.text(img, "MEMBERS", (box.x + 12, box.y + 186), 0.34, R.TEXT_DIM, 1)
        y = box.y + 208
        for user in (self.bot_user, self.operator, self.relay, self.bystander):
            online = user is not self.bot_user or self.online
            dot = (86, 196, 128) if online else (128, 132, 142)
            cv2.circle(img, (box.x + 18, y - 4), 5, dot, -1, cv2.LINE_AA)
            label = user.name + (" [BOT]" if user.bot else "")
            R.text(img, label, (box.x + 30, y), 0.4, user.color, 1)
            y += 24

        status = "gateway: connected" if self.online else "gateway: offline"
        R.text(img, status, (box.x + 12, box.y + box.h - 40), 0.38,
               R.GOOD if self.online else R.BAD, 1)
        if self.intents:
            R.text(img, f"intents: {len(self.intents)}",
                   (box.x + 12, box.y + box.h - 22), 0.36, R.TEXT_DIM, 1)

    def _draw_header(self, img: np.ndarray, box: Box) -> None:
        R.fill(img, box, (54, 57, 63))
        R.text(img, f"# {self.channel.name}", (box.x + 14, box.y + 22), 0.5,
               R.TEXT_BRIGHT, 1)
        R.text(img, "game relay + operator control", (box.x + 160, box.y + 22),
               0.4, R.TEXT_DIM, 1)
        counts = f"in {self.injected}   out {self.sent_by_bot}"
        width = R.text_size(counts, 0.4, 1)[0]
        R.text(img, counts, (box.x + box.w - width - 14, box.y + 22), 0.4,
               R.TEXT_DIM, 1)
        R.fill(img, Box(box.x, box.y + box.h - 1, box.w, 1), (44, 46, 51))

    def _draw_messages(self, img: np.ndarray, box: Box) -> None:
        """Newest at the bottom, wrapped, coloured by author."""
        with self.lock:
            messages = list(self.channel.messages) + list(self.dm_channel.messages)
        messages.sort(key=lambda m: m.at)

        lines: List[tuple] = []                        # (text, color, indent)
        for message in messages:
            stamp = time.strftime("%H:%M:%S", time.localtime(
                time.time() - (self.clock() - message.at)))
            tag = "  [DM]" if message.dm else ""
            head = f"{message.author.name}{tag}"
            lines.append((f"{head}   {stamp}", message.author.color, 0))
            for row in R.wrap(message.content, box.w - 40, 0.42):
                lines.append((row, R.TEXT if not message.dm else R.TEXT_DIM, 12))
            lines.append(("", R.TEXT, 0))

        max_rows = (box.h - 8) // 17
        for index, (text, color, indent) in enumerate(lines[-max_rows:]):
            if not text:
                continue
            R.text(img, text, (box.x + 14 + indent, box.y + 16 + index * 17),
                   0.42, color, 1)

    def _draw_footer(self, img: np.ndarray, box: Box) -> None:
        R.fill(img, box, (49, 51, 56))
        field = Box(box.x + 10, box.y + 4, box.w - 20, box.h - 12)
        R.fill(img, field, (64, 68, 75))
        typing = self.typing
        if typing and typing[1] > self.clock():
            R.text(img, f"{typing[0].name} is typing...",
                   (field.x + 12, field.y + 18), 0.42, typing[0].color, 1)
        else:
            R.text(img, f"Message #{self.channel.name}",
                   (field.x + 12, field.y + 18), 0.42, R.TEXT_DIM, 1)
