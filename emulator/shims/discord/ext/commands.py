"""
discord.ext.commands -- prefix commands, checks, converters, help.
==================================================================

`discord_bot.py` registers eleven commands with the usual decorators and relies
on a surprising amount of the framework's behaviour, all of which is reproduced
here because the emulator test is supposed to exercise the *real* control flow:

    * `@bot.command(name=..., aliases=[...], help=...)`   name + alias lookup
    * `async def plus_cmd(ctx, amount: int = 1)`          annotation conversion,
                                                          default when omitted
    * `async def run_cmd(ctx, *, command_line: str)`      keyword-only consumes
                                                          the rest of the line
    * `@bot.check`                                        global check, and a
                                                          `CheckFailure` that is
                                                          swallowed by the error
                                                          handler
    * `CommandNotFound` for `!nonsense`
    * `DefaultHelpCommand` so `!help` prints the command list
    * exceptions raised inside a command surface as `CommandInvokeError`

Everything is dispatched on the bot's own event loop, exactly like the real
library, so `asyncio.to_thread(...)` inside a command still works.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, Callable, Dict, List, Optional

from .. import Client, DiscordException, Message

__all__ = ["Bot", "Context", "Command", "CommandError", "CommandNotFound",
           "CheckFailure", "BadArgument", "MissingRequiredArgument",
           "CommandInvokeError", "UserInputError", "DefaultHelpCommand",
           "HelpCommand", "Cog", "command", "check"]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class CommandError(DiscordException):
    pass


class CommandNotFound(CommandError):
    pass


class CheckFailure(CommandError):
    pass


class UserInputError(CommandError):
    pass


class BadArgument(UserInputError):
    pass


class MissingRequiredArgument(UserInputError):
    def __init__(self, param):
        self.param = param
        super().__init__(f"{getattr(param, 'name', param)} is a required "
                         "argument that is missing.")


class CommandInvokeError(CommandError):
    def __init__(self, original: Exception):
        self.original = original
        super().__init__(f"Command raised an exception: "
                         f"{type(original).__name__}: {original}")


class TooManyArguments(UserInputError):
    pass


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

class Context:
    """What every command receives as its first argument."""

    def __init__(self, bot: "Bot", message: Message, prefix: str,
                 invoked_with: str, command: Optional["Command"],
                 args_line: str):
        self.bot = bot
        self.message = message
        self.prefix = prefix
        self.invoked_with = invoked_with
        self.command = command
        self.args_line = args_line
        self.author = message.author
        self.channel = message.channel
        self.guild = message.guild
        self.me = bot.user

    async def send(self, content: str = None, **kwargs) -> Message:
        return await self.channel.send(content, **kwargs)

    async def reply(self, content: str = None, **kwargs) -> Message:
        return await self.channel.send(content, **kwargs)

    def __repr__(self) -> str:
        return f"<Context command={self.invoked_with!r} author={self.author}>"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _resolved_params(callback: Callable) -> List[inspect.Parameter]:
    """The command's parameters (minus `ctx`), with real annotation objects.

    `discord_bot.py` starts with `from __future__ import annotations`, so at
    runtime `plus_cmd`'s `amount: int` annotation is the *string* `"int"`.  The
    real library resolves that before converting (discord.utils.evaluate_
    annotation); a shim that did not would hand the command `"3"` instead of
    `3` and turn a working command into a TypeError - a bug in the harness that
    would look exactly like a bug in the script.
    """
    params = list(inspect.signature(callback).parameters.values())[1:]
    try:
        hints = typing.get_type_hints(callback)
    except Exception:                                # unresolvable annotation
        return params
    return [param.replace(annotation=hints.get(param.name, param.annotation))
            for param in params]


class Command:
    def __init__(self, callback: Callable, name: str = None,
                 aliases: List[str] = None, help: str = None, **kwargs):
        self.callback = callback
        self.name = name or callback.__name__
        self.aliases = list(aliases or ())
        self.help = help or inspect.getdoc(callback) or ""
        self.short_doc = self.help.split("\n")[0]
        self.enabled = kwargs.pop("enabled", True)
        self.hidden = kwargs.pop("hidden", False)
        self.checks: List[Callable] = []
        self.params = _resolved_params(callback)

    # -- argument parsing --------------------------------------------------
    def parse_arguments(self, args_line: str) -> List[Any]:
        """Turn "3" into [3] for `(ctx, amount: int = 1)`.

        Mirrors the real converter rules closely enough for the commands in
        this repository: positional parameters take one whitespace separated
        token each, a keyword-only parameter swallows the remaining line, and
        the annotation is applied as a callable (`int("3")`).
        """
        rest = args_line.strip()
        values: List[Any] = []

        for index, param in enumerate(self.params):
            if param.kind is inspect.Parameter.KEYWORD_ONLY:
                if not rest:
                    if param.default is inspect.Parameter.empty:
                        raise MissingRequiredArgument(param)
                    values.append(param.default)
                else:
                    values.append(self._convert(param, rest))
                rest = ""
                break
            if param.kind is inspect.Parameter.VAR_POSITIONAL:
                while rest:
                    token, _, rest = rest.partition(" ")
                    rest = rest.strip()
                    values.append(self._convert(param, token))
                break
            if not rest:
                if param.default is inspect.Parameter.empty:
                    raise MissingRequiredArgument(param)
                values.append(param.default)
                continue
            token, _, rest = rest.partition(" ")
            rest = rest.strip()
            values.append(self._convert(param, token))

        return values

    @staticmethod
    def _convert(param, raw: str) -> Any:
        annotation = param.annotation
        if annotation is inspect.Parameter.empty or annotation is str:
            return raw
        if isinstance(annotation, type):
            try:
                return annotation(raw)
            except Exception as exc:
                raise BadArgument(
                    f"Converting to \"{annotation.__name__}\" failed for "
                    f"parameter \"{param.name}\".") from exc
        return raw

    async def invoke(self, ctx: "Context") -> None:
        values = self.parse_arguments(ctx.args_line)
        keyword = {}
        positional = []
        for param, value in zip(self.params, values):
            if param.kind is inspect.Parameter.KEYWORD_ONLY:
                keyword[param.name] = value
            else:
                positional.append(value)
        try:
            await self.callback(ctx, *positional, **keyword)
        except CommandError:
            raise
        except Exception as exc:
            raise CommandInvokeError(exc) from exc

    def __repr__(self) -> str:
        return f"<Command {self.name}>"


def command(name: str = None, **attrs):
    """Bare `@commands.command()` decorator (the bot uses `@bot.command`)."""
    def decorator(func):
        return Command(func, name=name, **attrs)
    return decorator


def check(predicate):
    """`@commands.check(...)` on a single command."""
    def decorator(func):
        target = func if isinstance(func, Command) else func
        checks = getattr(target, "checks", None)
        if checks is None:
            target.checks = checks = []
        checks.append(predicate)
        return target
    return decorator


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------

class HelpCommand:
    def __init__(self, **options):
        self.no_category = options.pop("no_category", "No Category")
        self.context: Optional[Context] = None

    async def send(self, ctx: Context, text: str) -> None:
        await ctx.send(text)


class DefaultHelpCommand(HelpCommand):
    """The stock "here is everything I can do" listing."""

    async def command_callback(self, ctx: Context, command_name: str = "") -> None:
        bot: Bot = ctx.bot
        if command_name:
            cmd = bot.get_command(command_name)
            if cmd is None:
                await ctx.send(f'No command called "{command_name}" found.')
                return
            await ctx.send(f"```\n{ctx.prefix}{cmd.name} {self._signature(cmd)}\n\n"
                           f"{cmd.help}\n```")
            return

        width = max([len(cmd.name) for cmd in bot.unique_commands()] + [4])
        lines = [f"{self.no_category}:"]
        for cmd in sorted(bot.unique_commands(), key=lambda c: c.name):
            lines.append(f"  {cmd.name.ljust(width)}  {cmd.short_doc}")
        lines.append("")
        lines.append(f"Type {ctx.prefix}help command for more info on a command.")
        await ctx.send("```\n" + "\n".join(lines) + "\n```")

    @staticmethod
    def _signature(cmd: Command) -> str:
        parts = []
        for param in cmd.params:
            if param.default is inspect.Parameter.empty:
                parts.append(f"<{param.name}>")
            else:
                parts.append(f"[{param.name}={param.default}]")
        return " ".join(parts)


class Cog:                                                     # pragma: no cover
    """Only here so `isinstance`/subclass checks in third party code work."""


# ---------------------------------------------------------------------------
# bot
# ---------------------------------------------------------------------------

class Bot(Client):
    """Client + command dispatch."""

    def __init__(self, command_prefix: str = "!", intents=None,
                 help_command: HelpCommand = None, description: str = None,
                 **kwargs):
        super().__init__(intents=intents, **kwargs)
        self.command_prefix = command_prefix
        self.description = description or ""
        self.all_commands: Dict[str, Command] = {}
        self._checks: List[Callable] = []
        self.help_command = help_command
        if help_command is not None:
            self._register_help(help_command)

    # -- registration ------------------------------------------------------
    def command(self, name: str = None, **attrs):
        """`@bot.command(name="kill", aliases=["stop"], help="...")`."""
        def decorator(func):
            cmd = func if isinstance(func, Command) else Command(func, name=name,
                                                                 **attrs)
            self.add_command(cmd)
            return cmd
        return decorator

    def add_command(self, cmd: Command) -> None:
        if cmd.name in self.all_commands:
            raise CommandError(f"command {cmd.name} is already registered")
        self.all_commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self.all_commands[alias] = cmd

    def remove_command(self, name: str) -> Optional[Command]:
        cmd = self.all_commands.pop(name, None)
        if cmd is not None:
            for alias in cmd.aliases:
                self.all_commands.pop(alias, None)
        return cmd

    def get_command(self, name: str) -> Optional[Command]:
        return self.all_commands.get(name)

    def unique_commands(self) -> List[Command]:
        seen, out = set(), []
        for cmd in self.all_commands.values():
            if id(cmd) not in seen:
                seen.add(id(cmd))
                out.append(cmd)
        return out

    @property
    def commands(self) -> List[Command]:
        return self.unique_commands()

    def check(self, predicate):
        """`@bot.check` - a global check run before every command."""
        self._checks.append(predicate)
        return predicate

    def _register_help(self, help_command: HelpCommand) -> None:
        async def _help(ctx, *, command_name: str = ""):
            help_command.context = ctx
            await help_command.command_callback(ctx, command_name)

        _help.__doc__ = "Shows this message."
        self.add_command(Command(_help, name="help", help="Shows this message."))

    # -- dispatch ----------------------------------------------------------
    async def process_commands(self, message: Message) -> None:
        """Parse `message` and run the command it names."""
        if message.author == self.user:
            return
        ctx = await self.get_context(message)
        if ctx is None:
            return
        await self.invoke(ctx)

    async def get_context(self, message: Message) -> Optional[Context]:
        prefix = self.command_prefix
        content = message.content
        if not content.startswith(prefix):
            return None
        body = content[len(prefix):].strip()
        if not body:
            return None
        invoked, _, args_line = body.partition(" ")
        return Context(self, message, prefix, invoked,
                       self.get_command(invoked), args_line.strip())

    async def invoke(self, ctx: Context) -> None:
        try:
            if ctx.command is None:
                raise CommandNotFound(f'Command "{ctx.invoked_with}" is not found')
            for predicate in self._checks + ctx.command.checks:
                if not await _maybe_await(predicate(ctx)):
                    raise CheckFailure(
                        f"The check functions for command {ctx.command.name} "
                        "failed.")
            await ctx.command.invoke(ctx)
        except CommandError as error:
            await self._dispatch_error(ctx, error)
        except Exception as error:                             # pragma: no cover
            await self._dispatch_error(ctx, CommandInvokeError(error))

    async def _dispatch_error(self, ctx: Context, error: Exception) -> None:
        handler = self._listeners.get("on_command_error")
        if handler is None:                                    # pragma: no cover
            raise error
        await handler(ctx, error)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
