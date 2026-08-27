"""
scenario.py -- the scripted run, and what it asserts.
=====================================================

This is the test itself.  It drives the emulated world and the emulated Discord
channel through one full, realistic session of the flow the README describes,
and it checks - through the emulator's own observations, never by reaching into
the script - that the bot reacted the way it is supposed to:

    1.  the Discord bot logs in and answers the operator
    2.  route1 is replayed: leg 1, the teleport hop, leg 2, arrive at the spot
    3.  the common case starts clicking the red target
    4.  "smited!"                      -> drink a brew, click the prayer orb
    5.  "no space for your loot!"      -> empty the pouch, shift-drop junk
    6.  "dodgy necklace crumbled"      -> wear a new one
    7.  "Shadow Veil has faded!"       -> spellbook tab, recast, inventory tab
    8.  the target wanders off         -> chase it and click it again
    9.  !screenshot from Discord       -> insert, which pops the chat open
    10. the chat watchdog              -> notices and closes it with '`'
    11. a valuable drop broadcast      -> announce, DM, then the pick-up routine
    12. the session restarts itself, replays the route again
    13. !kill                          -> the script says goodbye and exits 0

Every wait here is a real wait.  The script's own delays are untouched, so the
whole thing takes about as long as the real flow does - which is the point: a
timing bug in the automation shows up as a failed expectation, not as a
different number in a mock.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable, List, Optional, Tuple

from . import discord_server as DS
from .checks import Ledger

# The item the route1 profile is waiting for (config.ROUTES["route1"]).
DROP_ITEM = "Enhanced crystal teleport seed"


class Scenario:
    """Drives the emulated world; records what the script did about it."""

    PLAN = [
        "discord gateway login",
        "operator talks to the bot",
        "route replay: leg 1",
        "route replay: teleport hop",
        "route replay: leg 2 + arrival",
        "common case: clicking the target",
        "event: smited",
        "event: inventory full",
        "event: dodgy necklace crumbled",
        "event: shadow veil faded",
        "event: target wanders off",
        "discord: !screenshot",
        "chat watchdog closes the chat",
        "valuable drop broadcast",
        "valuable drop pick-up",
        "session restart + replay #2",
        "discord: !kill",
    ]

    def __init__(self, server, ledger: Ledger, viewer=None,
                 bot_alive: Callable[[], bool] = lambda: True,
                 stop: Optional[threading.Event] = None):
        self.server = server
        self.game = server.game
        self.discord = server.discord
        self.desktop = server.desktop
        self.ledger = ledger
        self.viewer = viewer
        self.bot_alive = bot_alive
        self.stop = stop or threading.Event()

        self.lock = threading.RLock()
        self.status = {label: "pending" for label in self.PLAN}
        self.active: Optional[str] = None
        self.started = time.monotonic()
        self.finished = False

    # ------------------------------------------------------------------
    # step bookkeeping
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def step(self, label: str):
        with self.lock:
            self.status[label] = "active"
            self.active = label
        self.ledger.note(f"[{time.monotonic() - self.started:6.1f}s] {label}")
        failed_before = self.ledger.failed
        try:
            yield
        finally:
            with self.lock:
                self.status[label] = ("failed" if self.ledger.failed > failed_before
                                      else "done")
        if self.viewer is not None:
            self.viewer.snapshot(label.replace(":", "").replace(" ", "-"))

    def hud_state(self) -> dict:
        with self.lock:
            order = list(self.PLAN)
            index = order.index(self.active) if self.active in order else 0
            window = order[max(0, index - 4):index + 2]
            steps = [(label, self.status[label]) for label in window]
            done = sum(1 for label in order if self.status[label] in
                       ("done", "failed"))
        return {"steps": steps, "progress": done / len(self.PLAN),
                "checks": (self.ledger.passed, self.ledger.failed)}

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------
    def sleep(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def operator(self, text: str, think: float = 1.1) -> None:
        """The human types for a moment, then posts."""
        self.discord.set_typing(self.discord.operator, think + 0.3)
        self.sleep(think)
        self.discord.operator_says(text)
        self.desktop.log("discord", f"operator: {text}", DS.OPERATOR_COLOR)

    def bystander(self, text: str) -> None:
        self.discord.bystander_says(text)
        self.desktop.log("discord", f"clanmate: {text}", DS.BYSTANDER_COLOR)

    # -- observation queries ---------------------------------------------
    def count(self, kind: str) -> int:
        return self.game.count(kind)

    def since(self, kind: str, when: float) -> int:
        return self.game.since(kind, when)

    def key_presses(self, key: str, when: float = 0.0) -> int:
        with self.game.lock:
            return sum(1 for obs in self.game.observations
                       if obs.kind == "key" and obs.detail == key
                       and obs.t >= when)

    def messages(self, when: float = 0.0) -> List[Tuple[str, str]]:
        with self.discord.lock:
            everything = (list(self.discord.channel.messages)
                          + list(self.discord.dm_channel.messages))
        return [(m.author.name, m.content) for m in everything if m.at >= when]

    def bot_said(self, needle: str, when: float = 0.0) -> bool:
        needle = needle.lower()
        return any(author == self.discord.bot_user.name and needle in content.lower()
                   for author, content in self.messages(when))

    def wait_bot_says(self, name: str, needle: str, when: float,
                      timeout: float = 30.0) -> bool:
        return self.ledger.wait_for(
            name, lambda: self.bot_said(needle, when), timeout=timeout,
            detail=lambda: f"last lines: {self.messages(when)[-3:]}",
            stop=self.stop)

    def wait_obs(self, name: str, kind: str, when: float, count: int = 1,
                 timeout: float = 60.0, detail: str = "") -> bool:
        return self.ledger.wait_for(
            name, lambda: self.since(kind, when) >= count, timeout=timeout,
            detail=lambda: (detail or f"{kind}: saw {self.since(kind, when)} of "
                                      f"{count} since the trigger"),
            stop=self.stop)

    # ==================================================================
    # the run
    # ==================================================================
    def run(self) -> None:
        try:
            self._run()
        finally:
            self.finished = True

    def _run(self) -> None:
        ledger = self.ledger

        # -- the emulated client must be a valid stand-in first -----------
        problems = self.game.audit()
        ledger.assert_true(
            "the emulated client renders exactly the colours config.COLORS hunts for",
            not problems, "; ".join(problems))

        with self.step("discord gateway login"):
            ledger.wait_for("the script logs into the Discord gateway",
                            lambda: self.discord.online, timeout=90,
                            stop=self.stop)
            ledger.assert_true(
                "it asked for the message_content intent",
                "message_content" in self.discord.intents,
                f"intents: {self.discord.intents}")
            ledger.wait_for(
                "the game window was found through the window manager",
                lambda: self.server.window_queries >= 1
                and self.server.grabbed_inside_game_window(),
                timeout=90, stop=self.stop,
                detail=lambda: f"wmctrl runs: {self.server.window_queries}, "
                               f"distinct grab rectangles: "
                               f"{len(self.server.grab_boxes)}")

        with self.step("operator talks to the bot"):
            mark = time.monotonic()
            self.operator("morning - is the bot up?")
            self.sleep(1.5)
            self.operator("!status")
            ledger.wait_for(
                "!status answers with the live run state",
                lambda: self.bot_said("route1", mark)
                and self.bot_said("phase", mark),
                timeout=30, stop=self.stop,
                detail=lambda: f"channel: {self.messages(mark)[-2:]}")
            mark = time.monotonic()
            self.operator("!nonsense")
            self.wait_bot_says("an unknown command is answered politely",
                               "unknown command", mark, timeout=20)

        with self.step("route replay: leg 1"):
            # Anchored at the start of the run, not of this step: the preamble
            # fires ~2s after the script starts (route.start_pause +
            # leg.initial_pause), which is while the operator above is still
            # typing.  Nothing else in the flow presses '2' or clicks the pouch
            # before this point, so counting from zero is exact, not lenient.
            start = 0.0
            ledger.wait_for(
                "the replay presses the inventory tab key and clicks the pouch",
                lambda: self.key_presses("2", start) >= 1
                and self.since("pouch.emptied", start) >= 1,
                timeout=60, stop=self.stop)
            ledger.wait_for(
                "the recorded timeline is played back into the client",
                lambda: self.since("click", start) >= 20, timeout=90,
                stop=self.stop,
                detail=lambda: f"{self.since('click', start)} clicks so far")
            mark = time.monotonic()
            self.operator("!count")
            self.wait_bot_says("!count reports the brew counter",
                               "brew_counter: 0", mark, timeout=20)
            mark = time.monotonic()
            self.operator("!plus 3")
            self.wait_bot_says("!plus 3 converts its argument and answers",
                               "brew_counter: -3", mark, timeout=20)

        with self.step("route replay: teleport hop"):
            start = 0.0                       # first hop of the process
            ledger.wait_for(
                "the hop takes a screenshot and clicks the black teleport tile",
                lambda: self.since("teleport", start) >= 1, timeout=150,
                stop=self.stop)
            ledger.assert_true(
                "the screenshot hotkey was pressed before the teleport click",
                self.key_presses("insert", start) >= 1,
                f"insert presses since the hop started: "
                f"{self.key_presses('insert', start)}")
            mark = time.monotonic()
            self.bystander("nice, it's moving")
            self.operator("!run echo emulator-smoke-test")
            self.wait_bot_says("!run executes a shell command and posts its output",
                               "emulator-smoke-test", mark, timeout=40)

        with self.step("route replay: leg 2 + arrival"):
            start = 0.0                       # 'j' is only pressed on arrival
            ledger.wait_for(
                "leg 2 finishes and the arrival sequence taps 'j' and 'insert'",
                lambda: self.key_presses("j", start) >= 1
                and self.key_presses("insert", start) >= 1,
                timeout=180, stop=self.stop)

        with self.step("common case: clicking the target"):
            start = 0.0                       # the first attack of the process
            ledger.wait_for(
                "the bot finds the red target and starts attacking it",
                lambda: self.since("attack", start) >= 5, timeout=90,
                stop=self.stop,
                detail=lambda: f"{self.since('attack', start)} attacks")
            strays = self._strays()
            ledger.assert_true(
                "every click so far landed inside the game canvas",
                not strays,
                f"{len(strays)} press(es) missed the canvas "
                f"{self.game.canvas_box}: {strays[:5]}")

        with self.step("event: smited"):
            mark = time.monotonic()
            self.game.event_smite()
            self.wait_obs("a brew is drunk after the smite", "brew.drunk", mark,
                          timeout=90)
            self.wait_obs("the prayer orb is clicked back on", "prayer.on", mark,
                          timeout=60)
            reply = self._brew_counter_reply()
            ledger.assert_true(
                "the brew counter went up by one",
                "brew_counter: -2" in reply,
                f"!count answered {reply!r}; expected -2 (0 at the start of the "
                f"run, -3 after !plus 3, +1 for the brew just drunk)")

        with self.step("event: inventory full"):
            mark = time.monotonic()
            self.game.event_loot_full()
            self.wait_obs("the coin pouch is emptied", "pouch.emptied", mark,
                          timeout=90)
            self.wait_obs("one junk item is shift-dropped", "item.dropped", mark,
                          timeout=60)

        with self.step("event: dodgy necklace crumbled"):
            mark = time.monotonic()
            self.game.event_dodgy_gone()
            self.wait_obs("a fresh dodgy necklace is worn", "necklace.worn", mark,
                          timeout=90)

        with self.step("event: shadow veil faded"):
            mark = time.monotonic()
            self.game.event_veil_gone()
            self.wait_obs("Shadow Veil is recast from the spellbook", "veil.cast",
                          mark, timeout=90)
            self.ledger.wait_for(
                "the spellbook tab is opened and the inventory tab restored",
                lambda: self.key_presses("4", mark) >= 1
                and self.key_presses("2", mark) >= 1,
                timeout=30, stop=self.stop)

        with self.step("event: target wanders off"):
            mark = time.monotonic()
            before = self._npc_point()
            self.game.event_target_move()
            self.wait_obs("the bot chases the target and attacks it again",
                          "attack", mark, count=3, timeout=120)
            # The click that lands *on* the target is proven by the attacks
            # above; what is worth asserting separately is that the bot settled
            # on the new position - the idle-click loop hammers wherever the
            # cursor was left, so the newest click is the one that tells you it
            # re-acquired instead of grinding the empty tile it started on.
            after = self._npc_point()
            last = self._last_click(mark)
            ledger.assert_true(
                "it is now clicking the target's new position",
                last is not None and abs(last[0] - after[0]) <= 60
                and abs(last[1] - after[1]) <= 60,
                f"the target moved {before} -> {after}, but the last click "
                f"was at {last}")

        with self.step("discord: !screenshot"):
            mark = time.monotonic()
            self.operator("!screenshot")
            self.wait_obs("the in-game screenshot hotkey is pressed", "screenshot",
                          mark, timeout=40)
            self.wait_bot_says("the bot confirms the screenshot", "screenshot key sent",
                               mark, timeout=30)

        with self.step("chat watchdog closes the chat"):
            mark = time.monotonic()
            ledger.wait_for(
                "the chat box pops open after the screenshot",
                lambda: self.game.chat_open or self.since("chat.autoopen", mark) >= 1,
                timeout=30, stop=self.stop)
            ledger.wait_for(
                "the watchdog OCRs the prompt and closes the chat again",
                lambda: self.since("chat.toggle", mark) >= 1
                and not self.game.chat_open,
                timeout=90, stop=self.stop,
                detail=lambda: f"chat_open={self.game.chat_open}")

        with self.step("valuable drop broadcast"):
            # The same mark is reused by the pick-up step below: the routine
            # starts a couple of seconds after the broadcast, which is while
            # this step is still checking the DM.
            drop_mark = mark = time.monotonic()
            self.game.event_valuable_drop(DROP_ITEM, count=2)
            self.wait_bot_says("the drop is announced to the channel",
                               "VALUEABLE DROP", mark, timeout=60)
            ledger.wait_for(
                "the operator is also DMed about it",
                lambda: any("VALUEABLE DROP" in message.content
                            for message in self.discord.dm_channel.messages),
                timeout=30, stop=self.stop,
                detail=lambda: f"DMs: {[m.content for m in self.discord.dm_channel.messages]}")

        with self.step("valuable drop pick-up"):
            mark = drop_mark
            self.wait_obs("two junk slots are shift-dropped to make room",
                          "item.dropped", mark, count=2, timeout=90)
            self.wait_obs("the ground label is found and the pile clicked",
                          "drop.clicked", mark, count=1, timeout=90)
            self.wait_obs("both piles are picked up", "drop.taken", mark, count=2,
                          timeout=120)
            # The closing screenshot is not sent when the last pile is clicked
            # but when the routine gives up looking for more labels, a few
            # seconds later - so this waits rather than asserts.
            ledger.wait_for(
                "the drop routine bracketed the pick-up with screenshots",
                lambda: self.key_presses("insert", mark) >= 2,
                timeout=60, stop=self.stop,
                detail=lambda: f"insert presses during the routine: "
                               f"{self.key_presses('insert', mark)}")

        with self.step("session restart + replay #2"):
            # Marked before the wait: the supervisor loop starts run #2 within a
            # couple of seconds of the notify, and '2' is not pressed by anything
            # else between here and that preamble.
            mark = start = time.monotonic()
            self.wait_bot_says("the script announces the restart in Discord",
                               "session finished", mark, timeout=60)
            self.game.reset_to_bank()
            ledger.wait_for(
                "run #2 replays the route from the top",
                lambda: self.key_presses("2", start) >= 1
                and self.since("click", start) >= 10,
                timeout=120, stop=self.stop)
            mark = time.monotonic()
            self.operator("!status")
            self.wait_bot_says("!status now reports run 2", "run: 2", mark,
                               timeout=30)

        with self.step("discord: !kill"):
            self.sleep(6)
            mark = time.monotonic()
            self.operator("!kill")
            self.wait_bot_says("the bot says goodbye with the total runtime",
                               "killing program", mark, timeout=30)
            ledger.wait_for("the script's process exits",
                            lambda: not self.bot_alive(), timeout=45,
                            stop=self.stop)
            strays = self._strays()
            ledger.assert_true(
                "no click in the whole session landed outside the canvas",
                not strays,
                f"{len(strays)} of {len(self.desktop.click_points)} press(es) "
                f"missed the canvas: {strays[:5]}")

    # ------------------------------------------------------------------
    # assertions that need to look at the observation log
    # ------------------------------------------------------------------
    def _npc_point(self) -> Tuple[int, int]:
        with self.game.lock:
            return self.game.world_to_canvas(*self.game.npc)

    def _strays(self) -> List[Tuple[int, int, str]]:
        """Presses that landed outside the game canvas.

        Deliberately taken from the *desktop*, not from the game's observation
        log: the game only ever hears about clicks that hit its canvas, so a
        click on the window chrome - or on the wallpaper, which is what a
        coordinate-translation bug looks like - would be invisible there.
        """
        canvas = self.game.canvas_box
        with self.desktop.lock:
            points = list(self.desktop.click_points)
        return [point for point in points if not canvas.contains(point[0], point[1])]

    def _last_click(self, when: float) -> Optional[Tuple[int, int]]:
        with self.game.lock:
            for obs in reversed(self.game.observations):
                if obs.t < when:
                    break
                if obs.kind == "click":
                    return obs.data.get("x"), obs.data.get("y")
        return None

    def _brew_counter_reply(self) -> str:
        """Read the counter back out of Discord, not out of the script's memory."""
        mark = time.monotonic()
        self.discord.operator_says("!count")
        deadline = mark + 25
        while time.monotonic() < deadline:
            for author, content in self.messages(mark):
                if (author == self.discord.bot_user.name
                        and "brew_counter:" in content):
                    return content
            self.sleep(0.3)
        return "(no reply within 25s)"
