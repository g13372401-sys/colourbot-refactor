"""
main.py -- entry point + the two automation phases.
===================================================

    python main.py --route route1                 # normal run (replay + loop)
    python main.py --route route1 --start common   # skip the replay (old redclick.py)
    python main.py --route route1 --debug-drop     # test the valuable-drop pick-up
    python main.py --debug                         # overlay ONLY (no automation)
    python main.py --route route1 --debug          # normal run + live overlay
    python main.py --record routes/route1_leg1.json

Layout of this file
-------------------
    StepRunner   - runs the declarative key/click sequences from config.SEQUENCES
    Automation   - one "session": route replay phase, then the common case
                   (red-target clicking loop), plus the valuable-drop routine
    main()       - CLI, the persistent runtime stopwatch, the Discord service and
                   the supervisor loop that restarts a session after any error.
                   --debug also attaches the transparent click-through overlay
                   (overlay.py) that shows the live cursor position and input
                   action feed without changing the flow.  With no route/start
                   given, `--debug` alone runs ONLY the overlay and idles.

Mapping from the old scripts
----------------------------
    tester.py   -> `main.py --route route1`         (no more subprocess dance)
    replay.py   -> Automation.run_route_phase()
    redclick.py -> Automation.run_common_case()     (`--start common` to enter here)

Everything the old scripts did in a fresh process (restart after a valuable drop,
after "5 hitpoints!", after running out of brews) is now an in-process restart of
the session, which is what lets the Discord bot and the runtime stopwatch survive
those restarts.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import threading
import time
import traceback
from typing import List, Optional, Sequence

import config
import core
import discord_bot
import inputbackends
import overlay as overlay_mod
import vision as vision_mod
from ai_agent import AIAgent, StuckDetector

LOG = logging.getLogger("colourbot.main")

HERE = os.path.dirname(os.path.abspath(__file__))


# ===========================================================================
# Small helpers
# ===========================================================================

def resolve_path(path: str) -> str:
    """Route files are given relative to this script, not to the shell's cwd."""
    return path if os.path.isabs(path) else os.path.join(HERE, path)


def load_route_file(path: str) -> List[dict]:
    full = resolve_path(path)
    try:
        with open(full, "r", encoding="utf-8") as fh:
            events = json.load(fh)
    except FileNotFoundError:
        raise core.SessionError(f"route file not found: {full}") from None
    except json.JSONDecodeError as exc:
        raise core.SessionError(f"route file {full} is not valid json: {exc}") from None
    if not events:
        raise core.SessionError(f"route file {full} contains no events")
    return events


def parse_region(text: str) -> core.Rect:
    """--game-region 969,227,947,650"""
    try:
        x, y, w, h = (int(part) for part in text.replace(" ", "").split(","))
        return core.Rect(x, y, w, h)
    except Exception:
        raise argparse.ArgumentTypeError(
            "--game-region wants four integers: X,Y,WIDTH,HEIGHT") from None


def run_in_thread(target, name: str) -> threading.Thread:
    """Start a worker that dies quietly on a kill/restart request."""
    def wrapper():
        try:
            target()
        except core.ControlSignal:
            LOG.debug("%s stopping (kill/restart requested)", name)
        except Exception:
            LOG.error("%s crashed:\n%s", name, traceback.format_exc())

    thread = threading.Thread(target=wrapper, name=name, daemon=True)
    thread.start()
    return thread


# ===========================================================================
# Route recorder (unchanged feature, kept from replay.py --record)
# ===========================================================================

def record_route(path: str, window: Optional[core.GameWindow]) -> None:
    """Record mouse/keyboard into a route .json.  ESC stops the recording.

    Improvement over the old recorder: the samples are stored in the *reference*
    coordinate frame (the canvas position the existing routes were recorded at),
    so a route recorded today still replays correctly when the client window
    sits somewhere else tomorrow.
    """
    if core.pynput_mouse is None:                     # pragma: no cover
        raise core.SessionError("pynput is required for --record")
    from pynput import keyboard as pk, mouse as pm      # noqa: WPS433

    events: List[dict] = []
    stop = threading.Event()

    def point(x, y):
        return window.to_recorded(x, y) if window else (int(x), int(y))

    def on_move(x, y):
        rx, ry = point(x, y)
        events.append({"type": "mouse_move", "x": rx, "y": ry,
                       "timestamp": time.time()})

    def on_click(x, y, button, pressed):
        rx, ry = point(x, y)
        events.append({"type": "mouse_click", "x": rx, "y": ry,
                       "button": button.name, "pressed": pressed,
                       "timestamp": time.time()})

    def on_scroll(x, y, dx, dy):
        rx, ry = point(x, y)
        events.append({"type": "mouse_scroll", "x": rx, "y": ry, "dx": dx,
                       "dy": dy, "timestamp": time.time()})

    def key_name(key):
        return getattr(key, "char", None) or getattr(key, "name", str(key))

    def on_press(key):
        if key == pk.Key.esc:
            stop.set()
            return
        events.append({"type": "key_press", "key": key_name(key),
                       "timestamp": time.time()})

    def on_release(key):
        if key == pk.Key.esc:
            return
        events.append({"type": "key_release", "key": key_name(key),
                       "timestamp": time.time()})

    with pm.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll), \
            pk.Listener(on_press=on_press, on_release=on_release):
        LOG.warning("Recording... Press Esc to stop.")
        stop.wait()

    full = resolve_path(path)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        json.dump(events, fh)
    LOG.warning("Recorded %d events to %s", len(events), full)


# ===========================================================================
# Declarative step runner
# ===========================================================================

class StepRunner:
    """Executes one of the named sequences in config.SEQUENCES.

    Keeping these as data means the *order* of key presses and clicks in the
    replay phase can be changed without touching any logic - which is the whole
    point, because that order is what makes the route work in game.
    """

    def __init__(self, session: "Automation"):
        self.session = session

    def run(self, sequence_name: Optional[str]) -> None:
        if not sequence_name:
            return
        try:
            steps = config.SEQUENCES[sequence_name]
        except KeyError:
            raise core.SessionError(
                f"unknown sequence '{sequence_name}' - check config.SEQUENCES") from None
        LOG.debug("running sequence '%s' (%d steps)", sequence_name, len(steps))
        for step in steps:
            self.run_step(step)

    def run_step(self, step) -> None:
        session = self.session
        if isinstance(step, config.Wait):
            session.clock.wait(step.delay)
        elif isinstance(step, config.TapKey):
            session.input.tap(step.key, hold=step.hold, after=step.after,
                              note=step.note)
        elif isinstance(step, config.ClickLargestSolid):
            session.click_largest_solid(step.color, step.what, step.optional)
        elif isinstance(step, config.ClickTemplateMatch):
            session.click_template_match(step)
        elif isinstance(step, config.Log):
            LOG.info("%s", step.message)
        else:
            raise core.SessionError(f"cannot run step {step!r} - teach StepRunner "
                                    "about it in main.py")


# ===========================================================================
# One automation session
# ===========================================================================

class Automation:
    """A single run of the flow: route replay, then the common case.

    Created fresh by the supervisor for every (re)start, so all game state
    (brew counter, flags, detected regions) starts clean - exactly what the old
    "spawn a new process" restart achieved.
    """

    def __init__(self, args, state: core.BotState, clock: core.Clock,
                 timer: core.RuntimeTimer, ctx: discord_bot.BotContext,
                 service: Optional[discord_bot.DiscordService]):
        self.args = args
        self.state = state
        self.clock = clock
        self.timer = timer
        self.ctx = ctx
        self.service = service
        self.route_name = args.route
        self.route = config.ROUTES[args.route]

        self.window: Optional[core.GameWindow] = None
        self.vision: Optional[vision_mod.Vision] = None
        self.input: Optional[core.InputController] = None
        self.chat: Optional[vision_mod.ChatWatcher] = None
        self.ai_agent: Optional[AIAgent] = None
        self.stuck_detector = StuckDetector()

        # regions detected once when the common case starts
        self.target = None            # solid red blob that gets clicked
        self.target_anchor = (0, 0)    # where it was when we last looked
        self.prayer = None             # boxed yellow
        self.inventory_anchor = None   # boxed blue
        self.player_tile = None        # boxed purple (legacy drop marker)
        self.pouch = None              # template-matched coin pouch
        self.shadow_veil = None
        self.junk_slots: Sequence[core.Region] = ()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def prepare(self) -> None:
        """Find the game window and build the vision/input/chat helpers."""
        self.window = core.GameWindow.locate(self.args.game_region)
        self.vision = vision_mod.Vision(self.window)
        self.input = core.InputController(self.window, self.clock)
        self.chat = vision_mod.ChatWatcher(self.vision, self.input, self.clock)

        # let the Discord commands act on this session
        self.ctx.input = self.input
        self.ctx.route = self.route
        self.ctx.route_name = self.route_name
        self.state.route_name = self.route_name

        # AI agent for stuck-state recovery
        self.ai_agent = AIAgent(
            self.state, self.clock, self.vision, self.input,
            self.service, self.stuck_detector)

    # ------------------------------------------------------------------
    # phase 1: route replay  (was replay.py)
    # ------------------------------------------------------------------
    def run_route_phase(self) -> None:
        steps = StepRunner(self)
        self.state.phase = "route replay"
        if config.CHAT["run_during_replay"]:
            self.chat.start_guard()
        self.clock.wait("route.start_pause")
        steps.run(self.route.preamble)
        legs = list(self.route.legs)
        for index, leg in enumerate(legs, start=1):
            LOG.warning("PLAYBACK STARTED - %s leg %d/%d (%s)",
                        self.route_name, index, len(legs), leg)
            #steps.run(self.route.preamble)
            played = self.input.play_timeline(load_route_file(leg))
            LOG.info("replayed %d recorded events from %s", played, leg)
            steps.run("leg_outro")
            if index < len(legs):
                steps.run(self.route.between_legs)

        steps.run(self.route.after_last_leg)
        self.chat.stop_guard()

    def click_largest_solid(self, color: str, what: str,
                            optional: bool = True) -> None:
        """Find the biggest blob of `color` in the game window and click it.

        This is the old clear_pouch()/wear_dodgy()/prif_click() trio, which were
        three copies of the same six lines - and which used to search the whole
        desktop, so the "largest black area" could be the terminal window.
        """
        region = self.vision.largest_solid(color)
        if region is None:
            message = f"no {color} region on screen, skipping the {what} click"
            if optional:
                LOG.warning(message)
                return
            raise core.SessionError(message)
        LOG.info("found %s -> clicking %s", color, what)
        self.input.click_region(region)

    def click_template_match(self, step: config.ClickTemplateMatch) -> None:
        """Find every match of a template image and click one at random."""
        hits = self.vision.match_templates_any([resolve_path(step.template)],
                                               threshold=step.threshold)
        if not hits:
            message = f"no {step.what} template match on screen"
            if step.optional:
                LOG.warning(message + ", skipping")
                return
            raise core.SessionError(message)
        LOG.info("found %d %s template match(es)", len(hits), step.what)
        self.click_random_region(hits, step.what)

    # ------------------------------------------------------------------
    # phase 2: the common case  (was redclick.py)
    # ------------------------------------------------------------------
    def detect_static_regions(self) -> None:
        """One capture, all the anchors the loop needs (was done inline)."""
        img = self.vision.capture()

        self.target = self.vision.largest_solid("red", img)
        if self.target is None:
            if self.ai_agent and self.ai_agent.enabled:
                result = self._try_ai_recovery()
                if result == "restarted":
                    LOG.warning("AI agent restarted")
                    raise core.RestartRequested()
                elif result == "escalated":
                    LOG.warning("AI agent escalated to human - waiting")
                    self.clock.sleep("common.after_target_click")
                # Re-scan after AI agent attempt
                img = self.vision.capture()
                self.target = self.vision.largest_solid("red", img)
            if self.target is None:
                raise core.SessionError(
                    "no solid red target region inside the game window - is the "
                    "client in the right place and the highlight plugin on?")
        self.target_anchor = self.target.center

        self.inventory_anchor = self.vision.largest_boxed("blue", img)
        self.prayer = self.vision.largest_boxed("yellow", img)
        self.player_tile = self.vision.largest_boxed("purple", img)   # legacy only
        pouch_hits = self.vision.match_templates_any(
            [resolve_path(config.COIN_POUCH_TEMPLATE)], img)
        self.pouch = pouch_hits[0] if pouch_hits else None
        brews = self.vision.match_templates_any(
            config.BREW_TEMPLATES, img,
            threshold=config.VISION["brew_match_threshold"])
        LOG.info("startup scan: %d brew dose(s), prayer=%s, inventory=%s",
                 len(brews), self.prayer is not None,
                 self.inventory_anchor is not None)

        if self.inventory_anchor is None:
            raise core.SessionError(
                "the blue inventory anchor box was not found - the junk slots, "
                "the drop slots and the Shadow Veil icon are derived from it")

        derived = config.DERIVED_REGIONS
        self.shadow_veil = self.inventory_anchor.offset_by(derived["shadow_veil"])
        self.junk_slots = (
            self.inventory_anchor.offset_by(derived["junk_slot_1"]),
            self.inventory_anchor.offset_by(derived["junk_slot_2"]),
        )

        if self.args.save_debug_image:
            regions = [r for r in (self.target, self.inventory_anchor, self.prayer,
                                   self.player_tile, self.pouch, self.shadow_veil,
                                   *self.junk_slots) if r is not None]
            self.vision.save_annotated(img, regions, self.args.save_debug_image)

    def run_common_case(self) -> str:
        """The clicking loop.  Returns the reason the session wants a restart."""
        state, clock = self.state, self.clock
        state.phase = "common case"
        self.detect_static_regions()
        LOG.info("brew_counter: %d", state.brew_counter)
        self.chat.start_guard()
        self.stuck_detector.heartbeat()

        while True:
            # 1) one click on the target, then hand over to the worker threads
            self.click_target(self.target)
            self.stuck_detector.heartbeat()

            workers = [run_in_thread(self.watch_target_movement, "target-watch")]
            if self.target is not None:
                workers.append(run_in_thread(self.idle_click_loop, "idle-click"))
            workers.append(run_in_thread(self.watch_game_events, "event-watch"))
            for worker in workers:
                worker.join()

            # Prioritise valuable drop collection over any pending restart /
            # kill.  A restart may have been requested by a worker (e.g. red
            # target not visible) while the Discord bot simultaneously set the
            # valuable_drop flag — the drop must be picked up first.
            if state.valuable_drop:
                saved_interrupt = state.pending_actions
                state.clear_interrupt()
                try:
                    self.collect_valuable_drop()
                finally:
                    if saved_interrupt == "kill":
                        state.request_kill("deferred kill after drop")
                    elif saved_interrupt == "restart":
                        state.request_restart("deferred restart after drop")
                clock.wait("drop.before_restart")
                return "valuable drop collected"

            state.raise_if_interrupted()      # !kill / !restart while we waited

            # 2) whichever flag stopped the workers now gets handled
            handled_something = False

            if not state.prayer_active:                 # got smited -> re-pray, re-brew
                self.turn_on_prayer()
                state.prayer_active = True
                handled_something = True

            if state.target_move:                  # target wandered off
                self.follow_target()
                state.target_move = False
                handled_something = True
            elif state.full_invent:
                self.handle_full_inventory()
                handled_something = True
            elif state.no_dodgy:
                self.wear_new_dodgy()
                handled_something = True
            elif not state.shadow_veil_active:
                self.recast_shadow_veil()
                handled_something = True

            if handled_something:
                self.stuck_detector.heartbeat()

            # 3) AI agent stuck-state recovery
            if self.ai_agent and self.ai_agent.enabled:
                if self.stuck_detector.is_stuck():
                    result = self._try_ai_recovery()
                    if result == "restarted":
                        return "AI agent restarted session"
                    elif result == "escalated":
                        # Human was notified; wait for them to act via Discord
                        LOG.warning("AI agent escalated to human - waiting")
                        clock.sleep("common.after_target_click")
                    else:
                        # resolved or gave_up -- reset detector and continue
                        self.stuck_detector.reset()

    # -- the three worker loops -----------------------------------------
    def _try_ai_recovery(self) -> str:
        """Run the AI agent to try to unstick the bot.

        Pauses the chat guard while the AI works (it needs a clean screen)
        and resumes it afterward.  Returns "restarted", "escalated", or
        "resolved" / "gave_up".
        """
        LOG.warning("AI agent: activating stuck-state recovery")
        if self.chat is not None:
            self.chat.stop_guard()
        try:
            phase = self.state.phase
            result = self.ai_agent.try_unstick(
                f"Stuck during '{phase}' phase. "
                f"prayer_active={self.state.prayer_active}, "
                f"target_move={self.state.target_move}, "
                f"full_invent={self.state.full_invent}.")
            LOG.warning("AI agent: result = %s", result)
            return result
        except Exception as exc:
            LOG.error("AI agent crashed: %s", exc)
            return "gave_up"
        finally:
            if self.chat is not None:
                self.chat.start_guard()

    def _keep_going(self) -> bool:
        """The compound condition the legacy worker loops all shared."""
        state = self.state
        return (state.prayer_active and not state.empty_pouch and not state.no_dodgy
                and state.shadow_veil_active and not state.full_invent
                and not state.valuable_drop and not state.target_move
                and not state.five_hp)

    def click_target(self, target: Optional[core.Region]) -> None:
        """Move to a random point inside the red blob and click it once."""
        if target is None or not self.state.prayer_active:
            return
        self.input.click_region(target,
                               jitter_px=config.MOUSE["target_jitter_px"])
        self.clock.wait("common.after_target_click")

    def idle_click_loop(self) -> None:
        """Keep clicking where the cursor already is (the actual grinding).

        Includes the original 5% chance of a longer "human" hitch.
        """
        chance = config.COMMON["idle_click_pause_chance"]
        while self._keep_going():
            if random.random() <= chance:
                self.clock.wait("common.idle_click.pause")
            self.input.click_here()
            self.stuck_detector.heartbeat()
            self.clock.wait("common.idle_click.interval")

    def watch_target_movement(self) -> None:
        """Re-scan the red blob and raise target_move when it jumped."""
        threshold = config.VISION["target_move_threshold_px"]
        interval = config.VISION["scan_interval_seconds"]
        while self._keep_going():
            region = self.vision.largest_solid("red")
            if region is None:
                # The legacy code crashed this thread; a missing target for one
                # frame is usually the chat box or an animation, so just retry.
                LOG.warning("red target not visible this frame")
                self.clock.sleep(interval)
                if self.state.restart_count >= self.state.MAX_RESTARTS:
                    LOG.error(f"max restart limit reached: {self.state.MAX_RESTARTS}")
                    self.state.request_kill("restart limit reached")
                else:
                    self.state.restart_count += 1
                    self.service.notify_dm(f"red target not found: {self.state.restart_count} times")
                    if self.ai_agent and self.ai_agent.enabled:
                        result = self._try_ai_recovery()
                        if result == "restarted":
                            LOG.warning("AI agent restarted")
                            return "AI agent restarted session"
                        elif result == "escalated":
                            # Human was notified; wait for them to act via Discord
                            LOG.warning("AI agent escalated to human - waiting")
                            self.clock.sleep("common.after_target_click")
                        else:
                            # resolved or gave_up -- reset detector and continue
                            #self.stuck_detector.reset()
                            LOG.warning("Fallback restarted")
                            self.state.request_restart("red target not visible")
                return
                #continue
            dx = abs(self.target_anchor[0] - region.center[0])
            dy = abs(self.target_anchor[1] - region.center[1])
            if dx > threshold or dy > threshold:
                LOG.info("target moved by (%d,%d) px", dx, dy)
                self.state.target_move = True
                return
            self.clock.sleep(interval)

    def target_has_moved(self) -> bool:
        """Single-shot version of the above (used while chasing the target)."""
        threshold = config.VISION["target_move_threshold_px"]
        region = self.vision.largest_solid("red")
        if region is None:
            return False
        dx = abs(self.target_anchor[0] - region.center[0])
        dy = abs(self.target_anchor[1] - region.center[1])
        return dx > threshold or dy > threshold

    def watch_game_events(self) -> None:
        """Turn relayed Discord chat lines into automation flags."""
        state, clock = self.state, self.clock
        msgs = config.DISCORD["messages"]
        poll = config.VISION["event_poll_seconds"]

        while state.prayer_active:
            if state.valuable_drop or state.target_move:
                return

            if state.has_message(msgs["smited"]):
                LOG.warning("No prayer")
                state.prayer_active = False
                state.drop_message(msgs["smited"])
                clock.wait("common.after_smite")
                state.drop_message(msgs["smited"], drain=True)
                return

            if state.has_message(msgs["invent_full"]):
                LOG.warning("Full invent")
                state.full_invent = True
                state.drop_message(msgs["invent_full"])
                clock.wait("common.after_invent_full")
                state.drop_message(msgs["invent_full"], drain=True)
                return

            if state.has_message(msgs["dodgy_gone"]):
                LOG.warning("No dodgy")
                state.no_dodgy = True
                state.drop_message(msgs["dodgy_gone"])
                clock.wait("common.after_dodgy_gone")
                return

            if state.has_message(msgs["veil_gone"]):
                LOG.warning("Shadow veil")
                state.shadow_veil_active = False
                state.drop_message(msgs["veil_gone"])
                clock.wait("common.after_veil_gone")
                return

            clock.sleep(poll)

    # -- reactions -------------------------------------------------------
    def turn_on_prayer(self) -> None:
        """Drink a brew, then click the prayer orb back on."""
        LOG.warning("turning on prayer")
        self.state.bump_brews(1)
        brews = self.vision.match_templates_any(
            config.BREW_TEMPLATES,
            threshold=config.VISION["brew_match_threshold"])
        LOG.info("brew counter: %d", self.state.brew_counter)
        self.clock.wait("prayer.before_brew_click")

        if not brews:
            self.state.no_orange = True
            LOG.warning("Program finished (out of brews).")
            if self.service:
                self.service.notify("@everyone program finished.")
                self.service.notify("total runtime: " + self.timer.formatted())
            try:
                self.clock.wait("discord.before_restart")
            except core.ControlSignal:
                pass
            self.state.request_restart("out of brews")
            return

        self.click_random_region(brews, "brew dose")
        self.clock.wait("prayer.after_brew_click")
        if self.prayer is not None:
            self.input.click_region(self.prayer)
        self.clock.wait("prayer.after_prayer_click")

    def click_random_region(self, regions: Sequence[core.Region],
                            what: str = "region") -> None:
        """Click the middle of a randomly chosen blob (brews, necklaces)."""
        if not regions:
            LOG.warning("no %s available to click", what)
            return
        chosen = random.choice(list(regions))
        LOG.info("clicking random %s at canvas %s", what, chosen.center)
        self.input.move_and_click(*chosen.center)

    def follow_target(self) -> None:
        """Chase the red blob until it stops moving between two checks."""
        LOG.info("following the target")
        region = self.vision.largest_solid("red")
        if region is None:
            raise core.SessionError("lost the red target region while following it")
        self.click_target(region)
        self.target = region
        self.target_anchor = region.center
        self.clock.wait("target.settle")

        while self.target_has_moved():
            region = self.vision.largest_solid("red")
            if region is None:
                raise core.SessionError("lost the red target region while following it")
            self.click_target(region)
            self.target = region
            self.target_anchor = region.center
            if not self.state.prayer_active:
                self.turn_on_prayer()
                self.state.prayer_active = True
            self.clock.wait("target.settle")

    def handle_full_inventory(self) -> None:
        """Empty the coin pouch, then shift-drop one junk item."""
        LOG.info("inventory full - emptying the pouch and dropping junk")
        img = self.vision.capture()
        self.clock.wait("invent.before_pouch_click")
        hits = self.vision.match_templates_any([resolve_path(
            config.COIN_POUCH_TEMPLATE)], img)
        if hits:
            self.input.click_region(hits[0])
        self.clock.wait("invent.after_pouch_click")

        with self.input.held_key("shift"):
            self.clock.wait("invent.shift_settle")
            self.input.click_region(self.inventory_anchor)
            self.clock.wait("invent.after_drop_click")
        self.clock.wait("invent.after_shift_release")
        self.state.full_invent = False

    def wear_new_dodgy(self) -> None:
        """Put a fresh dodgy necklace on (any of the matching icons)."""
        img = self.vision.capture()
        template = os.path.join(HERE, "images", "Dodgy_necklace.webp")
        necklaces = self.vision.match_templates_any([template], img)
        self.clock.wait("dodgy.before_click")
        if necklaces:
            self.click_random_region(necklaces, "dodgy necklace")
        self.state.no_dodgy = False
        self.clock.wait("dodgy.after_click")

    def recast_shadow_veil(self) -> None:
        """Spellbook tab -> cast Shadow Veil -> back to the inventory tab."""
        LOG.info("shadow veil")
        self.clock.wait("veil.before_key")
        self.input.tap("4", hold="veil.spell_key_hold", after="veil.after_spell_key",
                       note="spellbook tab")
        self.input.click_region(self.shadow_veil)
        self.state.shadow_veil_active = True
        self.clock.wait("veil.after_spell_click")
        self.input.tap("2", hold="veil.tab_key_hold", after="veil.after_tab_key",
                       note="inventory tab")

    # ------------------------------------------------------------------
    # valuable drop
    # ------------------------------------------------------------------
    def collect_valuable_drop(self, clear_inventory: bool = True) -> int:
        """Free two inventory slots, then take the loot off the floor.

        Timing and key/click order are the ones from the old script; only the
        "where is the loot" part changed:

            old: click the magenta box on the player's own tile twice, and hope
                 the player never walked off that tile.
            new: find the ground-item label, OCR-confirm the item name, click the
                 pile, wait for the player *and* the trailing camera to settle,
                 re-scan and repeat until the label is gone.
        """
        state, clock = self.state, self.clock
        state.phase = "valuable drop"
        LOG.warning("valuable drop routine started")

        clock.wait("drop.before_screenshot")
        self.input.tap("insert", hold="drop.screenshot_hold",
                       note="screenshot before the pick-up")
        clock.wait("drop.between_screenshots")
        self.input.tap("home", hold="drop.screenshot_hold",
                       note="screenshot before the pick-up")
        clock.wait("drop.after_screenshot")

        if clear_inventory:
            # Two free slots: the valuable drop can be a stack of two.
            if not self.junk_slots:
                raise core.SessionError("junk inventory slots unknown - run "
                                        "detect_static_regions() first")
            with self.input.held_key("shift"):
                clock.wait("drop.shift_settle")
                self.input.click_region(self.junk_slots[0])
                clock.wait("drop.between_drops")
                self.input.click_region(self.junk_slots[1])
                clock.wait("drop.after_drops")

        picked = self.take_ground_drop()

        clock.wait("drop.before_final_screenshot")
        self.input.tap("insert", hold="drop.final_screenshot_hold",
                       after="drop.after_final_screenshot",
                       note="screenshot after the pick-up")
        clock.wait("drop.between_screenshots")
        self.input.tap("home", hold="drop.final_screenshot_hold",
                       after="drop.after_final_screenshot",
                       note="screenshot after the pick-up")
        LOG.warning("collected %d item click(s). starting replay soon", picked)
        state.valuable_drop = False
        return picked

    def take_ground_drop(self) -> int:
        """Pick up the drop until BOTH signals agree it is gone.  Click count.

        Where to click comes from the hollow pink *outline box* RuneLite draws
        around the drop's tile (`DropFinder.find_drop_by_outline`) - the text
        label can be walked over by the red monster blob, the outline cannot.
        The OCR'd text label (`DropFinder.find_drop`) is the *second* removal
        signal: the drop only counts as collected when both the outline and the
        item label are gone.

        Neither signal is trusted from a single frame: the monster's red blob is
        smaller than the tile, so a capture taken while it walks onto the tile
        can transiently cover part of the outline, but a settled frame never
        will.  "Gone" therefore only counts after `confirm_scans` consecutive
        clean scans.  If the outline shows back up, the drop is still on the
        floor and we click it again instead of giving up.
        """
        cfg = config.DROP
        finder = vision_mod.DropFinder(self.vision, self.route.drop_item_name)
        budget = max(1, self.route.expected_drops) + cfg["extra_attempts"]
        picked = 0
        attempt_count = 0

        # First acquisition gets several tries: the loot beam animation and the
        # chat box popping up both like to hide the tile for a moment.
        self.chat.ensure_closed("valuable drop scan")
        drop_box = finder.find_drop_by_outline()
        for scan in range(cfg["initial_scan_retries"]):
            if drop_box is not None:
                break
            if scan < cfg["initial_scan_retries"] - 1:
                LOG.info("no drop tile outline yet, re-scanning (%d/%d)",
                         scan + 1, cfg["initial_scan_retries"])
                self.clock.wait("drop.rescan_pause")
                self.chat.ensure_closed("valuable drop scan")
                drop_box = finder.find_drop_by_outline()

        if drop_box is None:
            LOG.error("could not find the %r drop on the floor",
                      self.route.drop_item_name)
            return 0

        confirmed = False
        confirm_clean = 0              # consecutive scans with both signals gone
        confirm_scans = 0              # total scans where only the label lingered
        reason = ""

        while not confirmed:
            if drop_box is not None:
                # The drop is still on the floor -> click it, then re-scan.
                if attempt_count >= budget:
                    reason = (f"attempt budget ({budget}) exhausted after "
                              f"{picked} pick-up click(s)")
                    break
                confirm_clean = 0
                confirm_scans = 0
                x, y = drop_box.center
                y += cfg["tile_click_offset_y"]   # box centre sits below tile centre
                LOG.warning("taking the drop at canvas (%d,%d) [click %d/%d]",
                            x, y, attempt_count + 1, budget)
                with self.input.held_key("shift"):
                    self.clock.wait("drop.shift_settle")
                    self.input.move_and_click(x, y, jitter_px=cfg["click_jitter_px"])
                    self.clock.wait("drop.after_drops")
                picked += 1
                attempt_count += 1
                # player walks there, camera catches up
                self.clock.wait("drop.pickup_settle")
                if attempt_count >= cfg["extra_attempts_warn"]:
                    LOG.warning("taking excessive attempts to pick up the drop!")
                    if self.service:
                        self.service.notify("@everyone taking excessive attempts to pick up the drop!")
                        self.service.notify_dm(f"{self.route_name}: taking excessive attempts to pick up the drop")
            else:
                label = finder.find_drop()
                if label is not None:
                    # The item label survives but no outline this frame - the
                    # monster is probably mid-walk on the tile.  Don't trust it.
                    confirm_clean = 0
                    confirm_scans += 1
                    if confirm_scans > cfg["confirm_max_scans"]:
                        reason = (f"OCR label kept matching for {confirm_scans} "
                                  "scans without an outline")
                        break
                    LOG.warning("drop OCR label still visible but no outline - "
                                "re-scanning (%d/%d)", confirm_scans,
                                cfg["confirm_max_scans"])
                    self.clock.wait("drop.rescan_pause")
                else:
                    # BOTH signals gone - accumulate a clean run.
                    confirm_clean += 1
                    LOG.info("drop confirm scan %d/%d: tile outline and OCR "
                             "label both gone", confirm_clean,
                             cfg["confirm_scans"])
                    if confirm_clean >= cfg["confirm_scans"]:
                        confirmed = True
                        break
                    self.clock.wait("drop.rescan_pause")

            # fresh look for the next round
            self.chat.ensure_closed("valuable drop scan")
            drop_box = finder.find_drop_by_outline()

        if confirmed:
            LOG.warning("drop confirmed gone by BOTH tile outline and OCR "
                        "label after %d click(s)", picked)
        else:
            LOG.error("drop NOT confirmed collected (%s) - check the "
                      "inventory, it may not have fit", reason)
            if self.service:
                self.service.notify("@everyone drop NOT confirmed collected!")
                self.service.notify_dm(f"{self.route_name}: drop NOT confirmed "
                                       "collected")
        return picked

    def run_drop_debug(self) -> int:
        """--debug-drop: exercise the pick-up without waiting for a real drop.

        Assumes the loot is already lying on the floor somewhere on screen (no
        Discord broadcast needed).  Everything else - the chat watchdog, the two
        junk drops, the OCR scan, the 3 s settle between clicks - runs exactly
        like it does in production.
        """
        LOG.warning("=== valuable drop DEBUG mode (route %s, item %r) ===",
                    self.route_name, self.route.drop_item_name)
        self.state.phase = "drop debug"
        if not vision_mod.ocr_available():
            LOG.warning("Tesseract is not available - the label will be matched "
                        "by colour only (see the README for the install step)")

        clear = not self.args.skip_inventory_clear
        if clear:
            self.detect_static_regions()          # needs the blue anchor box
        else:
            LOG.info("--skip-inventory-clear: not dropping any junk items")

        self.chat.start_guard()
        self.chat.ensure_closed("debug start")
        picked = self.collect_valuable_drop(clear_inventory=clear)
        self.chat.stop_guard()
        LOG.warning("=== debug run finished: %d take-click(s) issued ===", picked)
        return picked

    def click_home_tab(self) -> None:
        """--debug-tab-click: template-match house_tab.png and click it."""
        template = os.path.join(HERE, "images", "house_tab.png")
        #LOG.warning("=== tab-click DEBUG mode: looking for %s ===", template)
        self.state.phase = "clicking home tab"
        hits = self.vision.match_templates_any([template])
        if not hits:
            LOG.warning("house_tab.png not found in the game window")
            return
        self.input.move_and_click(*hits[0].center)
        LOG.warning("clicked house_tab.png at canvas %s", hits[0].center)

    # ------------------------------------------------------------------
    # the whole flow
    # ------------------------------------------------------------------
    def run(self) -> str:
        """Route replay (unless we were told to skip it) + the common case."""
        if self.args.start == "common" and self.state.session_index == 1:
            LOG.warning("--start common: skipping the route replay for this run "
                        "(restarts will replay %s from the top)", self.route_name)
        else:
            self.run_route_phase()
        return self.run_common_case()


# ===========================================================================
# CLI + supervisor
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Colour-bot automation harness for anti-cheat benchmarking.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--route", default=None, choices=sorted(config.ROUTES),
                        help="which route profile from config.ROUTES to run "
                             f"(default: {config.DEFAULT_ROUTE}; required with "
                             "--start common)")
    parser.add_argument("--start", default="full", choices=("full", "common"),
                        help="'full' = replay the route then loop; "
                             "'common' = jump straight into the clicking loop "
                             "(the old redclick.py entry point)")
    parser.add_argument("--debug-drop", action="store_true",
                        help="test the valuable-drop pick-up on loot that is "
                             "already on the floor, then exit")
    parser.add_argument("--debug-tab-click", action="store_true",
                        help="find house_tab.png in the game window via template "
                             "matching and click it, then exit")
    parser.add_argument("--skip-inventory-clear", action="store_true",
                        help="with --debug-drop: do not shift-drop the two junk "
                             "items first (use when the inventory is not full)")
    parser.add_argument("--debug", action="store_true",
                        help="show a transparent click-through overlay over the "
                             "game window with the live cursor position (canvas "
                             "coords) and a feed of the bot's input actions. "
                             "Game stays visible and clickable.  With no route "
                             "or --start, runs the overlay ONLY (nothing else "
                             "runs); with --route X it runs the normal flow "
                             "with the overlay attached.")
    parser.add_argument("--record", metavar="FILE",
                        help="record a new route leg to FILE (ESC stops)")
    parser.add_argument("--game-region", type=parse_region, default=None,
                        metavar="X,Y,W,H",
                        help="skip the window search and use this canvas rect")
    parser.add_argument("--no-discord", action="store_true",
                        help="run without the Discord control channel")
    parser.add_argument("--ai-agent", action="store_true",
                        help="enable AI agent for stuck-state recovery "
                             "(overrides config.AI_AGENT['enabled'])")
    parser.add_argument("--reset-runtime", action="store_true",
                        help="zero the persistent runtime stopwatch first")
    parser.add_argument("--save-debug-image", metavar="FILE", default=None,
                        help="write an annotated capture of the detected regions")
    parser.add_argument("--list-routes", action="store_true",
                        help="print the configured routes and exit")
    parser.add_argument("--calibrate", action="store_true",
                        help="print the detected window/canvas geometry and exit")
    parser.add_argument("--input-backend", default=None,
                        choices=sorted(inputbackends.BACKENDS),
                        help="how input reaches Windows: 'interception' "
                             "(kernel filter driver - no LLMHF_INJECTED), "
                             "'arduino' (real USB HID board - no flags at all), "
                             "'sendinput' (legacy mouse/keyboard/pynput - SETS "
                             "LLMHF_INJECTED).  Default: config.INPUT['backend']")
    parser.add_argument("--list-input-backends", action="store_true",
                        help="print the available input transports and exit")
    parser.add_argument("--log-level", default=None,
                        help="DEBUG / INFO / WARNING")
    parser.add_argument("--log-file", default="", help="log file path ('' = config)")
    return parser


def print_routes() -> None:
    print("configured routes (config.ROUTES):")
    for name, profile in config.ROUTES.items():
        print(f"  {name:10s} {profile.description}")
        for index, leg in enumerate(profile.legs, start=1):
            print(f"             leg {index}: {leg}")
        print(f"             valuable drop: {profile.drop_item_name!r} "
              f"(broadcast keyword {profile.drop_keyword!r})")


def run_overlay_only(game_region: Optional[core.Rect]) -> int:
    """`--debug` with no route/start/record: run the overlay and nothing else.

    Locates the game window (so the overlay knows where to draw), starts the
    transparent click-through overlay, and then idles until Ctrl-C.  No Discord
    service, no route replay, no automation loop, no input of any kind - it only
    reads the cursor position to show it over the game.
    """
    window = core.GameWindow.locate(game_region)
    overlay = overlay_mod.DebugOverlay(window)
    overlay.start()
    LOG.warning("overlay-only mode: showing cursor/action debug over %s | "
                "Ctrl-C or ESC to exit", window.canvas)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        LOG.warning("ctrl-c - stopping the debug overlay")
    finally:
        overlay.stop()
        LOG.info("debug overlay stopped")
    return 0


def calibrate(args) -> int:
    """Show what the bot thinks the geometry is - run this after moving windows."""
    window = core.GameWindow.locate(args.game_region)
    reference = config.GAME_WINDOW["reference_canvas_origin"]
    print(f"window rect          : {window.window}")
    print(f"game canvas          : {window.canvas}")
    print(f"reference canvas     : {reference} (routes were recorded here)")
    print(f"recorded offset      : {window.recorded_offset}")
    print(f"screen               : {window.screen}")
    print(f"tesseract available  : {vision_mod.ocr_available()}")
    if args.save_debug_image:
        vis = vision_mod.Vision(window)
        img = vis.capture()
        regions = []
        for color in ("red", "blue", "yellow", "purple", "cyan"):
            region = vis.largest_boxed(color, img) or vis.largest_solid(color, img)
            if region is not None:
                regions.append(region)
        vis.save_annotated(img, regions, args.save_debug_image)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    core.setup_logging(args.log_level, args.log_file)

    if args.list_routes:
        print_routes()
        return 0

    if args.list_input_backends:
        print(inputbackends.describe_all())
        return 0

    # `--debug` with no explicit route/start/record means "standalone debug
    # overlay only" - nothing else runs.  `--debug --route X` keeps the normal
    # flow with the overlay attached.
    overlay_only = (args.debug and args.route is None
                    and args.start != "common" and not args.record)

    # Starting at the common case still needs to know which route to replay when
    # the loop restarts itself, so we insist on being told explicitly.
    if not overlay_only and args.route is None:
        if args.start == "common":
            parser.error("--route is required when starting at the common case "
                         "(the loop replays it on every restart), e.g. "
                         "--start common --route route1")
        args.route = config.DEFAULT_ROUTE

    state = core.BotState()
    clock = core.Clock(state)
    timer = core.RuntimeTimer()
    if args.reset_runtime:
        timer.load()
        timer.reset()

    if args.calibrate:
        return calibrate(args)

    if args.record:
        window = None
        try:
            window = core.GameWindow.locate(args.game_region)
        except core.SessionError as exc:
            LOG.warning("%s - recording raw screen coordinates instead", exc)
        record_route(args.record, window)
        return 0

    if overlay_only:
        return run_overlay_only(args.game_region)

    # --- input transport ------------------------------------------------
    # Opened here, before anything moves, so a missing driver or an unplugged
    # HID board is a clear error at start-up rather than a surprise mid-run.
    try:
        inputbackends.set_backend(inputbackends.create(args.input_backend))
    except inputbackends.BackendUnavailable as exc:
        LOG.error("no usable input backend: %s", exc)
        return 2

    # --- long lived services -------------------------------------------
    if args.ai_agent:
        config.AI_AGENT["enabled"] = True
        LOG.warning("--ai-agent: AI stuck-state recovery ENABLED (provider: %s)",
                     config.AI_AGENT["provider"])
    timer.start()
    ctx = discord_bot.BotContext(state, clock, timer)
    ctx.route_name = args.route
    ctx.route = config.ROUTES[args.route]
    ctx.args_line = " ".join(argv if argv is not None else sys.argv[1:])

    service = None
    if not args.no_discord:
        # Started *before* the first route replay so that !kill / !restart /
        # !screenshot already work while the bot is still walking to the spot.
        service = discord_bot.DiscordService(ctx)
        if not service.start():
            service = None

    core.start_panic_key_listener(state, timer)

    exit_code = 0
    overlay = None          # debug overlay persists across session restarts
    try:
        while True:
            state.session_index += 1
            state.clear_interrupt()
            state.reset_for_new_session()
            LOG.warning("=== run #%d | route %s | total runtime %s ===",
                        state.session_index, args.route, timer.formatted())

            session = Automation(args, state, clock, timer, ctx, service)
            try:
                session.prepare()
                # --debug: build the overlay once (the game window rarely moves,
                # and rebuilding it on every restart would make it flicker), then
                # point it at this session's fresh InputController so the action
                # feed updates.  Restarts recreate both, but the overlay persists.
                if overlay is None and args.debug:
                    overlay = overlay_mod.DebugOverlay(session.window)
                    overlay.start()
                if overlay is not None:
                    session.input.set_action_sink(overlay.push_action)
                if args.debug_drop:
                    session.run_drop_debug()
                    break
                if args.debug_tab_click:
                    session.click_home_tab()
                    break
                reason = session.run()
                LOG.warning("session finished (%s) - restarting the flow", reason)
                if service:
                    service.notify(f"session finished ({reason}) - restarting "
                                   f"`{args.route}`. runtime: {timer.formatted()}")
            except core.KillRequested:
                LOG.warning("kill requested - shutting down")
                break
            except core.RestartRequested:
                LOG.warning("restart requested - starting the flow from the top")
                continue
            except core.ControlSignal:                 # future-proofing
                break
            except Exception as exc:
                # "restart from the top in the same configuration as the user
                # started the run in" - including after a crash.
                LOG.error("session crashed: %s\n%s", exc, traceback.format_exc())
                if service:
                    service.notify(f"@here session crashed: `{exc}` - restarting "
                                   f"`{args.route}` in "
                                   f"{config.GENERAL['restart_backoff_seconds']:.0f}s "
                                   f"(runtime {timer.formatted()})")
                try:
                    state.raise_if_interrupted()
                except core.KillRequested:
                    break
                except core.RestartRequested:
                    pass
                state.clear_interrupt()
                time.sleep(config.GENERAL["restart_backoff_seconds"])
            finally:
                if session.chat is not None:
                    session.chat.stop_guard()
    except KeyboardInterrupt:
        LOG.warning("ctrl-c - shutting down")
    finally:
        if overlay is not None:
            overlay.stop()
            LOG.info("debug overlay stopped")
        timer.stop()
        LOG.warning("total runtime: %s", timer.formatted())

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
