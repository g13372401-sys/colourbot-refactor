"""
main.py -- entry point + the two automation phases.
===================================================

    python main.py --route route1                 # normal run (replay + loop)
    python main.py --route route1 --start common   # skip the replay (old redclick.py)
    python main.py --route route1 --debug-drop     # test the valuable-drop pick-up
    python main.py --record routes/route1_leg1.json

Layout of this file
-------------------
    StepRunner   - runs the declarative key/click sequences from config.SEQUENCES
    Automation   - one "session": route replay phase, then the common case
                   (red-target clicking loop), plus the valuable-drop routine
    main()       - CLI, the persistent runtime stopwatch, the Discord service and
                   the supervisor loop that restarts a session after any error

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
import vision as vision_mod

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

    def __init__(self, args, state: core.AutomationState, clock: core.Clock,
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

        # regions detected once when the common case starts
        self.target = None            # solid red blob that gets clicked
        self.target_anchor = (0, 0)    # where it was when we last looked
        self.prayer = None             # boxed yellow
        self.inventory_anchor = None   # boxed blue
        self.player_tile = None        # boxed purple (legacy drop marker)
        self.pouch = None              # boxed cyan
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

    # ------------------------------------------------------------------
    # phase 1: route replay  (was replay.py)
    # ------------------------------------------------------------------
    def run_route_phase(self) -> None:
        steps = StepRunner(self)
        self.state.phase = "route replay"
        if config.CHAT["run_during_replay"]:
            self.chat.start_guard()

        self.clock.wait("route.start_pause")
        legs = list(self.route.legs)
        for index, leg in enumerate(legs, start=1):
            LOG.warning("PLAYBACK STARTED - %s leg %d/%d (%s)",
                        self.route_name, index, len(legs), leg)
            steps.run(self.route.preamble)
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

    # ------------------------------------------------------------------
    # phase 2: the common case  (was redclick.py)
    # ------------------------------------------------------------------
    def detect_static_regions(self) -> None:
        """One capture, all the anchors the loop needs (was done inline)."""
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
        self.pouch = self.vision.largest_boxed("cyan", img)
        brews = self.vision.equal_largest_solids("orange", img)
        LOG.info("startup scan: %d brew blob(s), prayer=%s, inventory=%s",
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

        while True:
            # 1) one click on the target, then hand over to the worker threads
            self.click_target(self.target)

            workers = [run_in_thread(self.watch_target_movement, "target-watch")]
            if self.target is not None:
                workers.append(run_in_thread(self.idle_click_loop, "idle-click"))
            workers.append(run_in_thread(self.watch_game_events, "event-watch"))
            for worker in workers:
                worker.join()
            state.raise_if_interrupted()      # !kill / !restart while we waited

            # 2) whichever flag stopped the workers now gets handled
            if not state.clicking:                 # got smited -> re-pray, re-brew
                self.turn_on_prayer()
                state.clicking = True

            if state.target_move:                  # target wandered off
                self.follow_target()
                state.target_move = False

            if state.valuable_drop:
                self.collect_valuable_drop()
                clock.wait("drop.before_restart")
                return "valuable drop collected"
            elif state.full_invent:
                self.handle_full_inventory()
            elif state.no_dodgy:
                self.wear_new_dodgy()
            elif not state.shadow_veil_active:
                self.recast_shadow_veil()

    # -- the three worker loops -----------------------------------------
    def _keep_going(self) -> bool:
        """The compound condition the legacy worker loops all shared."""
        state = self.state
        return (state.clicking and not state.empty_pouch and not state.no_dodgy
                and state.shadow_veil_active and not state.full_invent
                and not state.valuable_drop and not state.target_move
                and not state.five_hp)

    def click_target(self, target: Optional[core.Region]) -> None:
        """Move to a random point inside the red blob and click it once."""
        if target is None or not self.state.clicking:
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
                continue
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

        while state.clicking:
            if state.valuable_drop or state.target_move:
                return

            if state.has_message(msgs["smited"]):
                LOG.warning("No prayer")
                state.clicking = False
                state.drop_message(msgs["smited"])
                clock.wait("common.after_smite")
                state.drop_message(msgs["smited"], drain=True)
                return

            if state.has_message(msgs["loot_full"]):
                LOG.warning("Full invent")
                state.full_invent = True
                state.drop_message(msgs["loot_full"])
                clock.wait("common.after_loot_full")
                state.drop_message(msgs["loot_full"], drain=True)
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
        brews = self.vision.equal_largest_solids("orange")
        LOG.info("brew counter: %d", self.state.brew_counter)
        self.clock.wait("prayer.before_brew_click")

        if not brews:
            self.state.no_orange = True
            LOG.error("no orange! (out of brews)")
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
            if not self.state.clicking:
                self.turn_on_prayer()
                self.state.clicking = True
            self.clock.wait("target.settle")

    def handle_full_inventory(self) -> None:
        """Empty the coin pouch, then shift-drop one junk item."""
        LOG.info("inventory full - emptying the pouch and dropping junk")
        img = self.vision.capture()
        self.clock.wait("invent.before_pouch_click")
        pouch = self.vision.largest_solid("cyan", img)
        if pouch is not None:
            self.input.click_region(pouch)
        self.clock.wait("invent.after_pouch_click")

        with self.input.held_key("shift"):
            self.clock.wait("invent.shift_settle")
            self.input.click_region(self.inventory_anchor)
            self.clock.wait("invent.after_drop_click")
        self.clock.wait("invent.after_shift_release")
        self.state.full_invent = False

    def wear_new_dodgy(self) -> None:
        """Put a fresh dodgy necklace on (any of the equal white icons)."""
        img = self.vision.capture()
        necklaces = self.vision.equal_largest_solids("white", img)
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
        LOG.warning("collected %d item click(s). starting replay soon", picked)
        state.valuable_drop = False
        return picked

    def take_ground_drop(self) -> int:
        """Click the drop until its ground label is gone.  Returns click count.

        Each click walks the player onto the loot tile; the camera follows the
        player and lags behind, so the label moves *after* the click.  That is
        why we always re-scan after `drop.pickup_settle` (3 s) instead of
        clicking the same pixel twice like the old code did.
        """
        cfg = config.DROP
        finder = vision_mod.DropFinder(self.vision, self.route.drop_item_name)
        budget = max(1, self.route.expected_drops) + cfg["extra_attempts"]
        picked = 0

        for attempt in range(1, budget + 1):
            label = None
            # The very first scan gets a few retries: the loot beam animation and
            # the chat box popping up both like to hide the label for a moment.
            scans = cfg["initial_scan_retries"] if picked == 0 else 1
            for scan in range(scans):
                self.chat.ensure_closed("valuable drop scan")
                label = finder.find_drop()
                if label is not None:
                    break
                if scan < scans - 1:
                    LOG.info("no ground label yet, re-scanning (%d/%d)",
                             scan + 1, scans)
                    self.clock.wait("drop.rescan_pause")

            if label is None:
                if picked:
                    LOG.warning("no ground label left - %d item(s) taken", picked)
                else:
                    LOG.error("could not find the %r label on the floor",
                              self.route.drop_item_name)
                break

            x, y = label.click_point
            LOG.warning("taking the drop at canvas (%d,%d) [click %d/%d]",
                        x, y, attempt, budget)
            self.input.move_and_click(x, y, jitter_px=cfg["click_jitter_px"])
            picked += 1
            # player walks there, camera catches up
            self.clock.wait("drop.pickup_settle")

        if finder.find_drop() is not None:
            LOG.error("the drop label is STILL on screen - check the inventory, "
                      "it may not have fit")
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
    parser.add_argument("--skip-inventory-clear", action="store_true",
                        help="with --debug-drop: do not shift-drop the two junk "
                             "items first (use when the inventory is not full)")
    parser.add_argument("--record", metavar="FILE",
                        help="record a new route leg to FILE (ESC stops)")
    parser.add_argument("--game-region", type=parse_region, default=None,
                        metavar="X,Y,W,H",
                        help="skip the window search and use this canvas rect")
    parser.add_argument("--no-discord", action="store_true",
                        help="run without the Discord control channel")
    parser.add_argument("--reset-runtime", action="store_true",
                        help="zero the persistent runtime stopwatch first")
    parser.add_argument("--save-debug-image", metavar="FILE", default=None,
                        help="write an annotated capture of the detected regions")
    parser.add_argument("--list-routes", action="store_true",
                        help="print the configured routes and exit")
    parser.add_argument("--calibrate", action="store_true",
                        help="print the detected window/canvas geometry and exit")
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

    # Starting at the common case still needs to know which route to replay when
    # the loop restarts itself, so we insist on being told explicitly.
    if args.route is None:
        if args.start == "common":
            parser.error("--route is required when starting at the common case "
                         "(the loop replays it on every restart), e.g. "
                         "--start common --route route1")
        args.route = config.DEFAULT_ROUTE

    state = core.AutomationState()
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

    # --- long lived services -------------------------------------------
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
                if args.debug_drop:
                    session.run_drop_debug()
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
        timer.stop()
        LOG.warning("total runtime: %s", timer.formatted())

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
