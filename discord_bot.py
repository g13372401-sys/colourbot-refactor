"""
discord_bot.py -- the Discord control channel.
==============================================

Two jobs, cleanly separated:

    1. COMMANDS the operator types.  These are now proper discord.py commands
       with a prefix (`!kill`, `!screenshot`, `!restart`, `!run tasklist`, ...)
       instead of the old bare words ("kill", "screen", "count") that fired on
       any random line of chat.  Adding a command is one decorated function in
       `_register_commands`.

    2. GAME MESSAGES relayed into the channel by the RuneLite Discord plugin
       ("There is no space for your loot!", "Shadow Veil has faded!", the
       valuable-drop broadcast, ...).  Those are still matched as plain text -
       they come from the game, not from a human - and they drive the flags the
       automation threads watch.  The wording lives in config.DISCORD["messages"].

The service owns nothing about the game itself: it talks to `AutomationState`
(flags/counters) and to a small `BotContext` that main.py keeps up to date with
the currently active route and input controller.  That is what lets the bot come
up *before* the first route is replayed and stay up for the whole life of the
process, including across automatic restarts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from typing import Optional

import config
import core

LOG = logging.getLogger("colourbot.discord")

# discord.py is optional so the automation can be developed/tested offline.
try:
    import discord
    from discord.ext import commands
except Exception as _exc:                                  # pragma: no cover
    discord = None
    commands = None
    LOG.debug("discord.py unavailable: %s", _exc)

if sys.platform == "win32":
    # Legacy requirement kept: the proactor loop chokes on discord.py's ssl use
    # when it is started from a background thread.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class BotContext:
    """The handful of live objects the bot needs from the automation side.

    main.py creates one of these and refreshes `input`/`route` whenever a new
    session starts, so the commands always act on the current run.
    """

    def __init__(self, state: core.AutomationState, clock: core.Clock,
                 timer: core.RuntimeTimer):
        self.state = state
        self.clock = clock
        self.timer = timer
        self.input: Optional[core.InputController] = None
        self.route = None                 # config.RouteProfile of the active run
        self.route_name = config.DEFAULT_ROUTE
        self.args_line = ""               # how the operator started us (for !status)


class DiscordService:
    """Runs the bot in a daemon thread for the entire life of the process."""

    def __init__(self, ctx: BotContext, cfg: dict = None):
        self.ctx = ctx
        self.cfg = cfg or config.DISCORD
        self.state = ctx.state
        self.bot = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._last_channel = None
        self._user = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def token(self) -> str:
        return os.environ.get("COLOURBOT_DISCORD_TOKEN") or self.cfg["token"]

    @property
    def user_id(self) -> int:
        env = os.environ.get("COLOURBOT_DISCORD_USER_ID")
        return int(env) if env else int(self.cfg["user_id"])

    def start(self) -> bool:
        """Spin the bot up.  Returns False when it is not configured/installed."""
        if commands is None:
            LOG.warning("discord.py is not installed - running without the "
                        "Discord control channel")
            return False
        token = self.token
        if not token or token == "BOT_TOKEN_PLACEHOLDER":
            LOG.warning("no Discord token configured (config.DISCORD['token'] or "
                        "COLOURBOT_DISCORD_TOKEN) - running without the "
                        "Discord control channel")
            return False

        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True        # needed to read the relayed lines
        intents.guilds = True
        self.bot = commands.Bot(command_prefix=self.cfg["command_prefix"],
                               intents=intents, help_command=commands.DefaultHelpCommand())
        self._register_events()
        self._register_commands()

        self._thread = threading.Thread(target=self._run, name="discord",
                                        daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        """Thread body: own event loop so `notify()` can post from anywhere."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.bot.start(self.token))
        except Exception as exc:                       # bad token, no network...
            LOG.error("Discord bot stopped: %s", exc)

    # ------------------------------------------------------------------
    # outbound helpers (callable from any thread)
    # ------------------------------------------------------------------
    def _channel(self):
        channel_id = self.cfg.get("channel_id") or 0
        if channel_id and self.bot is not None:
            channel = self.bot.get_channel(int(channel_id))
            if channel is not None:
                return channel
        return self._last_channel

    def notify(self, text: str) -> None:
        """Fire-and-forget message into the control channel."""
        if self.bot is None or self.loop is None:
            LOG.info("[discord notify skipped] %s", text)
            return
        channel = self._channel()
        if channel is None:
            LOG.info("[discord notify, no channel yet] %s", text)
            return

        async def _send():
            try:
                await channel.send(text)
            except Exception as exc:                   # missing perms, rate limit
                LOG.warning("could not send to Discord: %s", exc)

        try:
            asyncio.run_coroutine_threadsafe(_send(), self.loop)
        except Exception as exc:                       # loop already closed
            LOG.debug("notify dropped: %s", exc)

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def _register_events(self) -> None:
        bot, cfg, state = self.bot, self.cfg, self.state

        @bot.event
        async def on_ready():
            LOG.info("Logged in as %s", bot.user.name)
            LOG.info("Bot is now listening for messages...")
            try:
                self._user = await bot.fetch_user(self.user_id)
            except Exception as exc:
                LOG.warning("could not fetch user %s for DMs: %s",
                            self.user_id, exc)

        @bot.event
        async def on_message(message):
            self._last_channel = message.channel
            content = message.content

            # Mailbox bookkeeping first - the legacy code did this even for the
            # bot's own messages, and the watcher thread relies on the order.
            state.record_message(content)

            msgs = cfg["messages"]
            if state.recent_count(msgs["loot_full"]) == cfg["loot_spam_threshold"]:
                await message.channel.send("@everyone coin pouch error!")
                self._hard_exit("coin pouch error")

            if message.author == bot.user:
                return

            if content.startswith(cfg["command_prefix"]):
                await bot.process_commands(message)
                return

            await self._handle_game_message(message)

    async def _handle_game_message(self, message) -> None:
        """Relayed game chat -> automation flags.  Order matches the original."""
        cfg, state = self.cfg, self.state
        msgs = cfg["messages"]
        content = message.content

        # --- death: nothing to salvage, stop everything -------------------
        if msgs["death_substring"].lower() in content.lower():
            await message.channel.send("@everyone dead lmao")
            self._hard_exit("player died")
            return

        # --- out of food: restart the whole flow (bank trip) --------------
        if content == msgs["low_hp"]:
            await message.channel.send("current brew_counter: "
                                       + str(state.brew_counter))
            await self._wait("discord.before_restart")
            state.request_restart("low hp relay message")
            return

        # --- valuable drop broadcast -> arm the pick-up routine -----------
        keyword = self._drop_keyword()
        if keyword and keyword.lower() in content.lower():
            state.valuable_drop = True
            LOG.warning("valuable drop broadcast detected: %r", content)
            await message.channel.send("@everyone VALUEABLE DROP !!!")
            await message.channel.send(f"{message.author.name} said: '{content}'")
            try:
                if self._user is not None:
                    await self._user.create_dm()
                    await self._user.dm_channel.send("VALUEABLE DROP !!!")
            except Exception as exc:
                LOG.warning("could not DM the operator: %s", exc)

        # --- somebody/something asked about hitpoints --------------------
        if msgs["brew_query_substring"].lower() in content.lower():
            await self._wait("discord.before_brew_reply")
            await message.channel.send("brew_counter: " + str(state.brew_counter))

        # --- no brews left ------------------------------------------------
        # Legacy quirk kept on purpose: `no_orange` is raised by the automation
        # thread but only *acted on* when the next line arrives in the channel.
        if state.no_orange:
            await message.channel.send("@everyone program finished.")
            await message.channel.send("total runtime: " + self.ctx.timer.formatted())
            LOG.warning("Program finished (out of brews).")
            await self._wait("discord.before_restart")
            state.request_restart("out of brews")
            return

        if state.program_finished:
            await message.channel.send("@everyone program finished.")
            await message.channel.send("total runtime: " + self.ctx.timer.formatted())
            self._hard_exit("program finished")

    async def _wait(self, delay_name: str) -> None:
        """Sleep in a worker thread; a pending kill/restart just skips the wait."""
        try:
            await asyncio.to_thread(self.ctx.clock.wait, delay_name)
        except core.ControlSignal:
            pass

    def _drop_keyword(self) -> str:
        route = self.ctx.route
        return route.drop_keyword if route is not None else ""

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def _register_commands(self) -> None:
        bot, cfg = self.bot, self.cfg
        state, ctx = self.state, self.ctx

        @bot.check
        async def _authorised(command_ctx) -> bool:
            allowed = cfg["authorized_user_ids"]
            if not allowed or command_ctx.author.id in allowed:
                return True
            LOG.warning("ignoring command from unauthorised user %s",
                        command_ctx.author)
            await command_ctx.send("you are not on the authorised user list.")
            return False

        # -- process control ------------------------------------------------
        @bot.command(name="kill", aliases=["stop", "quit"],
                     help="Stop the script immediately.")
        async def kill_cmd(command_ctx):
            await command_ctx.send("killing program. total runtime: "
                                   + ctx.timer.formatted())
            self._hard_exit("!kill")

        @bot.command(name="restart", aliases=["reboot"],
                     help="Restart the flow from the top, same configuration.")
        async def restart_cmd(command_ctx):
            await command_ctx.send(
                f"restarting `{ctx.route_name}` from the top "
                f"(runtime keeps counting: {ctx.timer.formatted()})")
            state.request_restart("!restart")

        @bot.command(name="screenshot", aliases=["screen", "shot"],
                     help="Press the in-game screenshot hotkey (insert).")
        async def screenshot_cmd(command_ctx):
            await asyncio.to_thread(self._tap_screenshot_key)
            await command_ctx.send("screenshot key sent.")

        # -- counters --------------------------------------------------------
        @bot.command(name="count", help="Show the brew counter.")
        async def count_cmd(command_ctx):
            await command_ctx.send("current brew_counter: " + str(state.brew_counter))

        @bot.command(name="plus", help="Register an added brew (counter -= n).")
        async def plus_cmd(command_ctx, amount: int = 1):
            # Legacy arithmetic on purpose: brew_counter counts brews *used*, so
            # adding brews to the inventory lowers it.
            value = state.bump_brews(-amount)
            await command_ctx.send(f"added brew, brew_counter: {value}")

        @bot.command(name="minus", help="Register a removed brew (counter += n).")
        async def minus_cmd(command_ctx, amount: int = 1):
            value = state.bump_brews(amount)
            await command_ctx.send(f"subtracted brew, brew_counter: {value}")

        @bot.command(name="reset", help="Zero the brew counter.")
        async def reset_cmd(command_ctx):
            value = state.set_brews(0)
            await command_ctx.send(f"reset brews count, brew_counter: {value}")

        # -- introspection ---------------------------------------------------
        @bot.command(name="runtime", aliases=["uptime"],
                     help="Total accumulated runtime (survives restarts).")
        async def runtime_cmd(command_ctx):
            await command_ctx.send(
                f"total runtime: {ctx.timer.formatted()} "
                f"(this process: {ctx.timer.formatted_session()})")

        @bot.command(name="status", help="Flags, phase and route of the run.")
        async def status_cmd(command_ctx):
            snap = state.snapshot()
            lines = [f"**{ctx.route_name}** | `{ctx.args_line}`",
                     f"runtime: {ctx.timer.formatted()}"]
            lines += [f"{key}: {value}" for key, value in snap.items()]
            await command_ctx.send("```\n" + "\n".join(lines) + "\n```")

        @bot.command(name="routes", help="List the configured routes.")
        async def routes_cmd(command_ctx):
            lines = [f"{name}: {profile.description} "
                     f"[{len(profile.legs)} leg(s)]"
                     for name, profile in config.ROUTES.items()]
            await command_ctx.send("```\n" + "\n".join(lines) + "\n```")

        # -- arbitrary shell -------------------------------------------------
        @bot.command(name="run", help="Run a shell command on the bot machine.")
        async def run_cmd(command_ctx, *, command_line: str):
            if not cfg["allow_run_command"]:
                await command_ctx.send("!run is disabled in config.DISCORD.")
                return
            LOG.warning("running shell command from Discord: %s", command_line)
            output = await asyncio.to_thread(self._run_shell, command_line)
            limit = cfg["run_command_output_limit"]
            body = output[:limit] + ("\n...(truncated)" if len(output) > limit else "")
            await command_ctx.send(f"```\n{body or '(no output)'}\n```")

        @bot.event
        async def on_command_error(command_ctx, error):
            if commands is not None and isinstance(error, commands.CommandNotFound):
                await command_ctx.send("unknown command - try `!help`")
                return
            if commands is not None and isinstance(error, commands.CheckFailure):
                return                                   # already answered
            LOG.warning("command error: %s", error)
            await command_ctx.send(f"command failed: `{error}`")

    # ------------------------------------------------------------------
    # helpers used by the commands
    # ------------------------------------------------------------------
    def _tap_screenshot_key(self) -> None:
        """'insert' is RuneLite's screenshot hotkey (it uploads to Discord).

        Works even before the first session exists (no game window needed), so
        the operator can grab a screenshot during the replay phase too.
        """
        controller = self.ctx.input
        if controller is not None:
            controller.tap("insert", hold="discord.screenshot_hold",
                           note="Discord !screenshot")
            return
        if core.keyboard_lib is None:
            LOG.warning("keyboard module missing - cannot send the screenshot key")
            return
        core.keyboard_lib.press("insert")
        self.ctx.clock.sleep(core.sample_delay(
            config.DELAYS["discord.screenshot_hold"]))
        core.keyboard_lib.release("insert")

    @staticmethod
    def _run_shell(command_line: str) -> str:
        try:
            done = subprocess.run(command_line, shell=True, capture_output=True,
                                  text=True,
                                  timeout=config.DISCORD["run_command_timeout"])
            return ((done.stdout or "") + (done.stderr or "")).strip() or \
                   f"(exit code {done.returncode}, no output)"
        except subprocess.TimeoutExpired:
            return "(timed out)"
        except Exception as exc:
            return f"(failed: {exc})"

    def _hard_exit(self, why: str) -> None:
        """Flush the runtime stopwatch, then take the process down now."""
        LOG.warning("hard exit: %s", why)
        try:
            self.ctx.timer.stop()
            LOG.warning("total runtime: %s", self.ctx.timer.formatted())
        finally:
            os._exit(0)
