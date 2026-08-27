"""
discord -- drop-in replacement for discord.py, wired to the local emulator.
==========================================================================

`discord_bot.py` is written against the real library and is not modified: it
builds `discord.Intents`, constructs a `commands.Bot`, registers events and
commands with the usual decorators and finally `await bot.start(token)`.

Everything below is the part of that API the bot actually touches, implemented
on top of a unix socket to `emulator/discord_server.py`.  No sockets to the
internet are opened at any point - `bot.start()` connects to
$COLOURBOT_EMULATOR_SOCKET, receives a READY frame and then a stream of
MESSAGE_CREATE-shaped frames, which is precisely the shape the real gateway has.

Model objects are deliberately thin: an id, a name and the couple of methods the
bot calls.  Equality is by id (that is how `message.author == bot.user` decides
whether to ignore its own echo, so it has to behave).
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
from typing import Optional

__version__ = "2.3.0-emulator"
__all__ = ["Intents", "User", "Member", "Message", "TextChannel", "DMChannel",
           "Client", "ext", "utils", "errors"]

_HEADER = struct.Struct(">I")
SOCKET_ENV = "COLOURBOT_EMULATOR_SOCKET"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class DiscordException(Exception):
    pass


class LoginFailure(DiscordException):
    pass


class HTTPException(DiscordException):
    pass


class Forbidden(HTTPException):
    pass


class NotFound(HTTPException):
    pass


# ---------------------------------------------------------------------------
# intents / flags
# ---------------------------------------------------------------------------

class Intents:
    """A bag of booleans.  `Intents.default()` then `intents.x = True`."""

    _FLAGS = ("guilds", "members", "messages", "message_content", "reactions",
              "presences", "guild_messages", "dm_messages", "typing", "voice_states")

    def __init__(self, **kwargs):
        for flag in self._FLAGS:
            setattr(self, flag, bool(kwargs.get(flag, False)))

    @classmethod
    def default(cls) -> "Intents":
        """Everything except the two privileged intents (members/presences)."""
        intents = cls()
        for flag in cls._FLAGS:
            setattr(intents, flag, flag not in ("members", "presences",
                                                "message_content"))
        return intents

    @classmethod
    def all(cls) -> "Intents":
        return cls(**{flag: True for flag in cls._FLAGS})

    @classmethod
    def none(cls) -> "Intents":
        return cls()

    def __repr__(self) -> str:
        on = [flag for flag in self._FLAGS if getattr(self, flag)]
        return f"<Intents {' '.join(on)}>"


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

class _Snowflake:
    def __init__(self, id: int):
        self.id = int(id)

    def __eq__(self, other) -> bool:
        return isinstance(other, _Snowflake) and other.id == self.id

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.id))


class User(_Snowflake):
    def __init__(self, id: int, name: str, bot: bool = False,
                 gateway: "_Gateway" = None):
        super().__init__(id)
        self.name = name
        self.display_name = name
        self.discriminator = "0"
        self.bot = bot
        self.mention = f"<@{self.id}>"
        self.dm_channel: Optional["DMChannel"] = None
        self._gateway = gateway

    async def create_dm(self) -> "DMChannel":
        """Open (or reuse) the DM channel with this user."""
        if self.dm_channel is None:
            reply = await self._gateway.rpc("discord_create_dm", user_id=self.id)
            self.dm_channel = DMChannel(reply["channel"]["id"],
                                        reply["channel"].get("name", self.name),
                                        self, self._gateway)
        return self.dm_channel

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<User id={self.id} name={self.name!r}>"


class Member(User):
    pass


class TextChannel(_Snowflake):
    def __init__(self, id: int, name: str, gateway: "_Gateway" = None):
        super().__init__(id)
        self.name = name
        self._gateway = gateway

    async def send(self, content: str = None, **kwargs) -> "Message":
        text = content if content is not None else ""
        reply = await self._gateway.rpc("discord_send", channel_id=self.id,
                                        content=text)
        return Message(reply["message"], self._gateway)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<TextChannel id={self.id} name={self.name!r}>"


class DMChannel(TextChannel):
    def __init__(self, id: int, name: str, recipient: User = None,
                 gateway: "_Gateway" = None):
        super().__init__(id, name, gateway)
        self.recipient = recipient

    def __repr__(self) -> str:
        return f"<DMChannel id={self.id} recipient={self.recipient}>"


class Message(_Snowflake):
    def __init__(self, data: dict, gateway: "_Gateway" = None):
        super().__init__(data["id"])
        self.content = data.get("content", "")
        self.created_at = data.get("created_at")
        author = data.get("author", {})
        self.author = User(author.get("id", 0), author.get("name", "unknown"),
                           bool(author.get("bot")), gateway)
        channel = data.get("channel", {})
        cls = DMChannel if channel.get("dm") else TextChannel
        self.channel = cls(channel.get("id", 0), channel.get("name", "channel"),
                           gateway=gateway)
        self.guild = None if channel.get("dm") else _Guild(
            channel.get("guild_id", 1), channel.get("guild_name", "emulator"))
        self._gateway = gateway

    async def reply(self, content: str = None, **kwargs) -> "Message":
        return await self.channel.send(content, **kwargs)

    def __repr__(self) -> str:
        return f"<Message author={self.author} content={self.content!r}>"


class _Guild(_Snowflake):
    def __init__(self, id: int, name: str):
        super().__init__(id)
        self.name = name


# ---------------------------------------------------------------------------
# the "gateway": framed json over the emulator's unix socket
# ---------------------------------------------------------------------------

class _Gateway:
    """Two async connections: one push stream, one request/response channel."""

    def __init__(self):
        self.path = os.environ.get(SOCKET_ENV)
        self._events = None                      # (reader, writer)
        self._rpc = None
        self._rpc_lock = asyncio.Lock()

    # -- framing ----------------------------------------------------------
    @staticmethod
    async def _send(writer, header: dict) -> None:
        blob = json.dumps(header).encode("utf-8")
        writer.write(_HEADER.pack(len(blob)) + blob)
        await writer.drain()

    @staticmethod
    async def _recv(reader) -> dict:
        raw = await reader.readexactly(_HEADER.size)
        (length,) = _HEADER.unpack(raw)
        header = json.loads((await reader.readexactly(length)).decode("utf-8"))
        nbytes = header.get("nbytes")
        if nbytes:
            await reader.readexactly(int(nbytes))
        return header

    # -- connections ------------------------------------------------------
    async def connect(self, token: str, intents: Intents) -> dict:
        if not self.path:
            raise LoginFailure(f"{SOCKET_ENV} is not set")
        self._events = await asyncio.open_unix_connection(self.path)
        self._rpc = await asyncio.open_unix_connection(self.path)
        await self._send(self._events[1], {
            "op": "discord_connect", "token": token,
            "intents": [flag for flag in Intents._FLAGS
                        if getattr(intents, flag, False)]})
        ready = await self._recv(self._events[0])
        if ready.get("error"):
            raise LoginFailure(ready["error"])
        return ready

    async def rpc(self, op: str, **fields) -> dict:
        async with self._rpc_lock:
            await self._send(self._rpc[1], dict(fields, op=op))
            reply = await self._recv(self._rpc[0])
        if reply.get("error"):
            raise HTTPException(reply["error"])
        return reply

    async def next_event(self) -> dict:
        return await self._recv(self._events[0])

    async def close(self) -> None:
        for pair in (self._events, self._rpc):
            if pair is not None:
                pair[1].close()
        self._events = self._rpc = None


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class Client:
    """The event pump.  `commands.Bot` adds the command layer on top."""

    def __init__(self, intents: Intents = None, **kwargs):
        self.intents = intents or Intents.default()
        self.user: Optional[User] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._gateway = _Gateway()
        self._listeners = {}
        self._channels = {}
        self._closed = False

    # -- events -----------------------------------------------------------
    def event(self, coro):
        """`@bot.event` - registers by the function's own name."""
        self._listeners[coro.__name__] = coro
        return coro

    def dispatch(self, name: str, *args) -> None:
        handler = self._listeners.get(f"on_{name}")
        if handler is not None:
            asyncio.ensure_future(self._run_handler(handler, name, *args))

    async def _run_handler(self, handler, name, *args) -> None:
        try:
            await handler(*args)
        except Exception as exc:                              # pragma: no cover
            import traceback
            print(f"[discord-shim] on_{name} raised {exc!r}")
            traceback.print_exc()

    # -- lookups ----------------------------------------------------------
    def get_channel(self, channel_id: int):
        return self._channels.get(int(channel_id))

    async def fetch_user(self, user_id: int) -> User:
        reply = await self._gateway.rpc("discord_fetch_user", user_id=int(user_id))
        return User(reply["user"]["id"], reply["user"]["name"],
                    bool(reply["user"].get("bot")), self._gateway)

    async def fetch_channel(self, channel_id: int) -> TextChannel:
        reply = await self._gateway.rpc("discord_fetch_channel",
                                        channel_id=int(channel_id))
        return TextChannel(reply["channel"]["id"], reply["channel"]["name"],
                           self._gateway)

    # -- lifecycle --------------------------------------------------------
    async def start(self, token: str, reconnect: bool = True) -> None:
        """Log in and pump events until the connection drops."""
        self.loop = asyncio.get_event_loop()
        ready = await self._gateway.connect(token, self.intents)
        self.user = User(ready["user"]["id"], ready["user"]["name"], True,
                         self._gateway)
        for channel in ready.get("channels", []):
            self._channels[int(channel["id"])] = TextChannel(
                channel["id"], channel["name"], self._gateway)
        await self._on_ready()
        while not self._closed:
            try:
                frame = await self._gateway.next_event()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                break
            await self._handle_frame(frame)

    async def _on_ready(self) -> None:
        handler = self._listeners.get("on_ready")
        if handler is not None:
            await self._run_handler(handler, "ready")

    async def _handle_frame(self, frame: dict) -> None:
        kind = frame.get("t")
        if kind == "message":
            message = Message(frame["message"], self._gateway)
            self._channels.setdefault(message.channel.id, message.channel)
            self.dispatch("message", message)
        elif kind == "close":
            self._closed = True

    async def close(self) -> None:
        self._closed = True
        await self._gateway.close()

    def is_closed(self) -> bool:
        return self._closed

    def run(self, token: str) -> None:                        # pragma: no cover
        asyncio.run(self.start(token))


# ---------------------------------------------------------------------------
# submodules
# ---------------------------------------------------------------------------

from . import utils                                            # noqa: E402,F401
from . import ext                                              # noqa: E402,F401
