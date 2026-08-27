"""
game_client.py -- the fake RuneLite client.
===========================================

This is the thing `main.py` believes it is playing.  It renders a 947x650
canvas inside a 955x680 window whose title contains "RuneLite", exactly the
geometry `config.GAME_WINDOW` describes:

    canvas_size   = (947, 650)
    window_insets = (4, 26, 4, 4)      # chrome around the rendered area
    chrome_color  = (30, 30, 30)       # used by GameWindow._refine_canvas

and it paints all eight highlight colours `config.COLORS` looks for, in the
geometric relationships `config.DERIVED_REGIONS` assumes:

    * the blue inventory anchor box sits on one inventory slot,
    * "junk slot 1/2" (+1,+39) and (+43,+39) land on the next row of slots,
      which means the inventory grid pitch has to be exactly (43, 39),
    * the Shadow Veil icon (-3,+101) lands on the spellbook page.

Get those wrong and the bot clicks empty space - which is precisely the class of
configuration bug this emulator exists to catch.

The client is a real (small) game, not a picture: it has an inventory with
items, prayer, a Shadow Veil timer, a target NPC that can wander, a chat box
that pops open a couple of seconds after every `insert` screenshot (the
behaviour `vision.ChatWatcher` was written for), ground items with the pink
Ground-Items label the drop finder OCRs, and two scenes with a teleport tile in
between.  Everything the bot does to it has a visible, checkable effect, and
every effect is recorded in `observations` so the test can assert on it.

A word on the interface layout
------------------------------
The recorded routes in routes/*.json are replayed verbatim and contain 39 real
mouse clicks at fixed canvas coordinates.  The panel below is placed so that
those clicks land on harmless things (an item, the panel background, the
minimap, empty ground) and never on the tab strip - one stray tab click during
leg 2 would leave the spellbook open, and the common case would then find no
inventory at all.  `emulator/checks.py` re-verifies that before every run, so
the layout cannot silently drift.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from . import render as R
from .render import Box

# ---------------------------------------------------------------------------
# geometry - keep in sync with config.GAME_WINDOW
# ---------------------------------------------------------------------------

CANVAS_W, CANVAS_H = 947, 650
INSET_L, INSET_T, INSET_R, INSET_B = 4, 26, 4, 4
WINDOW_W = CANVAS_W + INSET_L + INSET_R
WINDOW_H = CANVAS_H + INSET_T + INSET_B
WINDOW_TITLE = "RuneLite - EmuAcct"          # config.GAME_WINDOW["title_contains"]

# -- inventory grid ---------------------------------------------------------
# config.DERIVED_REGIONS says junk slot 1 is (+1, +39) from the blue anchor box
# and junk slot 2 is (+43, +39): one row down, and one row down + one column
# right.  So the grid pitch *must* be (43, 39).
SLOT_PITCH_X, SLOT_PITCH_Y = 43, 39
SLOT_W, SLOT_H = 42, 38
GRID_ORIGIN = (769, 352)                     # centre of slot (col 0, row 0)
GRID_COLS, GRID_ROWS = 4, 7
ANCHOR_SLOT = (0, 1)                         # the slot the blue box is drawn on

TAB_ROW = Box(741, 296, 200, 28)
INVENTORY_PANEL = Box(741, 326, 200, 288)
MINIMAP = Box(749, 8, 190, 172)
PRAYER_ORB = Box(703, 43, 34, 34)            # hollow yellow box
COMPASS_ORB = Box(703, 4, 34, 34)
HP_ORB = Box(703, 85, 34, 34)

# -- chat box (canvas coordinates, from config.CHAT) ------------------------
CHAT_BOX = Box(0, 470, 512, 180)
CHAT_PROMPT_RECT = Box(2, 600, 230, 26)      # config.CHAT["prompt_rect"]
CHAT_PROMPT_TEXT = "Press Enter to Chat"     # config.CHAT["prompt_text"]
CHAT_ALL_BUTTON = Box(6, 626, 56, 22)        # config.CHAT["all_button_rect"]
CHAT_TABS = ("All", "Game", "Public", "Private")

# -- spellbook --------------------------------------------------------------
# The blue anchor box is Box(752, 374, 35, 35), so DERIVED_REGIONS["shadow_veil"]
# resolves to centre (766, 492) and the bot clicks x in [760,772], y in
# [485,499] (0.2 shrink + 4 px of move jitter).  Cell (0, 4) below is centred on
# exactly that point.
SPELL_ORIGIN = (766, 340)
SPELL_PITCH = 38
SPELL_COLS, SPELL_ROWS = 5, 5
SHADOW_VEIL_CELL = (0, 4)

# -- world ------------------------------------------------------------------
PLAYER_HOME = (352, 322)
NPC_HOME = (300, 232)
TELEPORT_TILE = Box(414, 386, 46, 30)        # solid black marked tile
DROP_SPOTS = ((250, 512), (330, 556))        # under the chat box, on purpose
WALKABLE = Box(24, 96, 640, 500)             # the player never walks under the UI
WALK_SPEED = 220.0                           # canvas px per second

# -- game palette (never a reserved colour, see render.RESERVED) ------------
GROUND_A = (74, 92, 56)
GROUND_B = (66, 84, 50)
GROUND_C = (88, 78, 54)
CAVE_A = (58, 54, 66)
CAVE_B = (50, 46, 58)
CAVE_C = (68, 60, 72)
WALL = (96, 88, 74)
PROP_TREE = (44, 82, 52)
PROP_TRUNK = (78, 60, 40)
PROP_ROCK = (104, 104, 112)
PANEL_WOOD = (58, 50, 42)
PANEL_EDGE = (94, 80, 62)
SLOT_BG = (48, 42, 36)
GAME_TEXT = (238, 240, 244)                  # NOT pure white (white is a colour
                                             # the bot hunts for)
GAME_TEXT_DIM = (188, 186, 176)
HP_GREEN = (96, 186, 82)
HP_RED = (204, 42, 38)                       # NOT (255,0,0)
CHAT_BG = (24, 20, 16)                       # NOT (0,0,0) and NOT (30,30,30)
XP_GOLD = (226, 196, 96)
VEIL_PURPLE = (168, 138, 226)
# Far enough from the ground-item label pink (255,102,178) to survive its
# tolerance of 40 - otherwise every chat line about the drop would look like
# another ground label to DropFinder.
DROP_TEXT = (204, 106, 186)
LOOT_ICON = (198, 128, 206)


@dataclass
class Item:
    """One inventory item.  `color`/`size` decide what the bot's vision sees."""
    name: str
    kind: str                       # necklace | pouch | brew | junk | loot | misc
    color: Tuple[int, int, int]
    size: Tuple[int, int] = (26, 26)
    doses: int = 0

    @property
    def droppable(self) -> bool:
        return self.kind in ("junk", "loot")


@dataclass
class GroundItem:
    """A pile on the floor, with the label RuneLite paints above it."""
    name: str
    label: str
    world: Tuple[int, int]
    claimed: bool = False           # clicked; the player is walking over
    taken: bool = False             # picked up; the label is gone

    @property
    def visible(self) -> bool:
        return not self.taken


@dataclass
class Observation:
    """Something the bot did to the client, for the test's expectation ledger."""
    t: float
    kind: str
    detail: str = ""
    data: dict = field(default_factory=dict)


class GameClient:
    """The emulated client: state, input handling and rendering."""

    def __init__(self, origin: Tuple[int, int], rng: Optional[random.Random] = None):
        self.origin = origin                       # window top-left on the desktop
        self.rng = rng or random.Random(1337)
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self._last_update = time.monotonic()

        # -- player / world ------------------------------------------------
        self.scene = "bank"                        # "bank" -> "spot" after the hop
        self.player = [float(PLAYER_HOME[0]), float(PLAYER_HOME[1])]
        self.player_target: Optional[Tuple[float, float]] = None
        self.camera = [0.0, 0.0]
        self.camera_target = [0.0, 0.0]
        self.npc = [float(NPC_HOME[0]), float(NPC_HOME[1])]
        self.npc_name = "Chaos Fanatic"
        self.npc_hp = 1.0
        self.hp, self.max_hp = 99, 99
        self.prayer_points, self.max_prayer = 99.0, 99
        self.prayer_on = True
        self.veil_active = True
        self.dodgy_worn = True
        self.xp = 4_213_337
        self.attacks = 0
        self.kills = 0

        # -- interface -----------------------------------------------------
        self.tab = "inventory"                     # inventory | spellbook | prayer
        self.chat_open = False
        self.chat_lines: List[Tuple[str, Tuple[int, int, int]]] = []
        self._chat_open_at: Optional[float] = None  # 'insert' pops it open later
        self.shift_down = False
        self.screenshots = 0
        self._flash_until = 0.0
        self._toast: Optional[Tuple[str, float]] = None
        self.hitsplats: List[Tuple[int, int, int, float]] = []

        # -- inventory -----------------------------------------------------
        self.slots: Dict[Tuple[int, int], Optional[Item]] = {}
        self._build_inventory()

        # -- ground items / drops -------------------------------------------
        self.ground: List[GroundItem] = []

        # -- bookkeeping for the test ----------------------------------------
        self.observations: List[Observation] = []
        self.relay_queue: List[str] = []           # game chat -> Discord relay
        self.counters: Dict[str, int] = {}

        self.chat_say("Welcome to Old School RuneScape.", GAME_TEXT_DIM)
        self.chat_say("Shadow Veil is active.", VEIL_PURPLE)

    # ==================================================================
    # geometry
    # ==================================================================
    @property
    def window_box(self) -> Box:
        return Box(self.origin[0], self.origin[1], WINDOW_W, WINDOW_H)

    @property
    def canvas_box(self) -> Box:
        return Box(self.origin[0] + INSET_L, self.origin[1] + INSET_T,
                   CANVAS_W, CANVAS_H)

    @staticmethod
    def slot_center(col: int, row: int) -> Tuple[int, int]:
        return (GRID_ORIGIN[0] + SLOT_PITCH_X * col,
                GRID_ORIGIN[1] + SLOT_PITCH_Y * row)

    @staticmethod
    def slot_box(col: int, row: int) -> Box:
        cx, cy = GameClient.slot_center(col, row)
        return Box(cx - SLOT_W // 2, cy - SLOT_H // 2, SLOT_W, SLOT_H)

    @staticmethod
    def slot_at(cx: int, cy: int) -> Optional[Tuple[int, int]]:
        for col in range(GRID_COLS):
            for row in range(GRID_ROWS):
                if GameClient.slot_box(col, row).contains(cx, cy):
                    return (col, row)
        return None

    @property
    def anchor_box(self) -> Box:
        """The blue highlight box the whole inventory geometry hangs off."""
        cx, cy = self.slot_center(*ANCHOR_SLOT)
        return Box(cx - 17, cy - 17, 35, 35)

    @staticmethod
    def spell_center(col: int, row: int) -> Tuple[int, int]:
        return (SPELL_ORIGIN[0] + SPELL_PITCH * col,
                SPELL_ORIGIN[1] + SPELL_PITCH * row)

    def world_to_canvas(self, x: float, y: float) -> Tuple[int, int]:
        return int(x + self.camera[0]), int(y + self.camera[1])

    # ==================================================================
    # inventory helpers
    # ==================================================================
    def _build_inventory(self) -> None:
        for col in range(GRID_COLS):
            for row in range(GRID_ROWS):
                self.slots[(col, row)] = None

        def put(col, row, item):
            self.slots[(col, row)] = item

        white = R.RESERVED["white"]
        cyan = R.RESERVED["cyan"]
        orange = R.RESERVED["orange"]

        def necklace():
            return Item("Dodgy necklace", "necklace", white, (24, 24))

        # Row 0: two spare dodgy necklaces (white) + the coin pouch (cyan).
        # `leg_preamble` clicks the largest white blob before *every* leg, so a
        # two-leg route wears two per run; row 3 holds two more, which covers the
        # "necklace crumbled" event of run 1 and the first leg of run 2.  After
        # that the click is simply skipped (ClickLargestSolid is optional), which
        # is itself worth seeing in a long run.
        put(0, 0, necklace())
        put(1, 0, necklace())
        put(2, 0, Item("Coin pouch", "pouch", cyan, (26, 22)))
        put(3, 0, Item("Rune pouch", "misc", (108, 84, 152), (24, 26)))

        # Row 1: the slot the blue anchor box is drawn around.
        put(0, 1, Item("Bones", "junk", (214, 206, 178), (24, 24)))

        # Row 2: the two junk items the drop routine shift-drops (junk_slot_1
        # resolves to (0,2) and junk_slot_2 to (1,2)).
        put(0, 2, Item("Big bones", "junk", (206, 198, 170), (26, 26)))
        put(1, 2, Item("Ashes", "junk", (150, 146, 140), (24, 24)))

        # Row 3: the spare necklaces.
        put(0, 3, necklace())
        put(1, 3, necklace())

        # Row 5: super restore doses (orange) - the "brews".
        for col in range(4):
            put(col, 5, Item("Super restore(4)", "brew", orange, (22, 28), doses=4))

        # Row 6: odds and ends.
        put(0, 6, Item("Shark", "misc", (108, 148, 176), (24, 24)))
        put(1, 6, Item("Teleport tab", "misc", (176, 160, 118), (24, 24)))

    def items_of(self, kind: str) -> List[Tuple[Tuple[int, int], Item]]:
        with self.lock:
            return [(pos, item) for pos, item in sorted(self.slots.items())
                    if item is not None and item.kind == kind]

    def free_slots(self) -> List[Tuple[int, int]]:
        with self.lock:
            return [pos for pos, item in sorted(self.slots.items()) if item is None]

    # ==================================================================
    # bookkeeping
    # ==================================================================
    def observe(self, kind: str, detail: str = "", **data) -> None:
        with self.lock:
            self.observations.append(
                Observation(time.monotonic(), kind, detail, data))
            self.counters[kind] = self.counters.get(kind, 0) + 1

    def count(self, kind: str) -> int:
        with self.lock:
            return self.counters.get(kind, 0)

    def since(self, kind: str, t: float) -> int:
        """How many `kind` observations happened at or after monotonic time `t`."""
        with self.lock:
            return sum(1 for obs in self.observations
                       if obs.kind == kind and obs.t >= t)

    def last(self, kind: str) -> Optional[Observation]:
        with self.lock:
            for obs in reversed(self.observations):
                if obs.kind == kind:
                    return obs
        return None

    def chat_say(self, line: str, color=GAME_TEXT_DIM) -> None:
        with self.lock:
            self.chat_lines.append((line, color))
            del self.chat_lines[:-12]

    def relay(self, line: str) -> None:
        """Post a game chat line into the Discord relay queue."""
        self.chat_say(line, XP_GOLD)
        with self.lock:
            self.relay_queue.append(line)

    def pop_relay(self) -> List[str]:
        with self.lock:
            out, self.relay_queue = self.relay_queue, []
            return out

    def toast(self, message: str, seconds: float = 2.5) -> None:
        self._toast = (message, time.monotonic() + seconds)

    # ==================================================================
    # scripted game events (driven by emulator/scenario.py)
    # ==================================================================
    def event_smite(self) -> None:
        """The NPC smites the player: prayer drained, relay posts 'smited!'."""
        with self.lock:
            self.prayer_on = False
            self.prayer_points = 0.0
            self.hp = max(12, self.hp - 24)
        self.observe("event.smite")
        self.chat_say(f"{self.npc_name} smites you!", (226, 120, 96))
        # config.DISCORD["messages"]["smited"] is matched by *equality*, so the
        # relay posts exactly what the bot is configured to look for.
        self.relay("smited!")

    def event_loot_full(self) -> None:
        """Fill the inventory with loot, then relay the 'no space' line."""
        with self.lock:
            for pos in self.free_slots():
                self.slots[pos] = Item("Grubby key", "loot", (172, 148, 96),
                                       (24, 24))
        self.observe("event.loot_full")
        self.relay("There is no space for your loot!")

    def event_dodgy_gone(self) -> None:
        with self.lock:
            self.dodgy_worn = False
        self.observe("event.dodgy_gone")
        self.relay("Your dodgy necklace has crumbled to dust.")

    def event_veil_gone(self) -> None:
        with self.lock:
            self.veil_active = False
        self.observe("event.veil_gone")
        self.relay("Shadow Veil has faded!")

    def event_target_move(self, dx: int = 104, dy: int = 78) -> None:
        """Walk the NPC far enough to trip VISION['target_move_threshold_px']."""
        with self.lock:
            self.npc[0] += dx
            self.npc[1] += dy
        self.observe("event.target_move", f"npc -> {tuple(self.npc)}")
        self.chat_say(f"The {self.npc_name} wanders off.", GAME_TEXT_DIM)

    def event_valuable_drop(self, item_name: str, count: int = 2,
                            value: str = "6.67M") -> None:
        """Drop `count` piles on the floor and broadcast it to Discord."""
        with self.lock:
            # RuneLite's Ground Items plugin paints the plain item name above
            # each pile; DropFinder OCRs it and fuzzy-matches route.drop_item_name.
            self.ground = [GroundItem(item_name, item_name, spot)
                           for spot in DROP_SPOTS[:count]]
        self.observe("event.valuable_drop", item_name, count=count)
        self.chat_say(f"Valuable drop: {item_name} ({value} gp)", DROP_TEXT)
        # The broadcast the RuneLite loot-broadcast relay posts.  It contains the
        # route's drop_keyword ("teleport") - and nothing that looks like the
        # death or hitpoints triggers.
        self.relay(f"EmuAcct received a valuable drop: {item_name} "
                   f"x{count} ({value} gp)")

    def event_low_hp(self) -> None:
        with self.lock:
            self.hp = 5
        self.observe("event.low_hp")
        self.relay("5 hitpoints!")

    def reset_to_bank(self) -> None:
        """Put the world back the way a fresh session would find it."""
        with self.lock:
            self.scene = "bank"
            self.player = [float(PLAYER_HOME[0]), float(PLAYER_HOME[1])]
            self.player_target = None
            self.npc = [float(NPC_HOME[0]), float(NPC_HOME[1])]
            self.npc_hp = 1.0
            self.camera = [0.0, 0.0]
            self.camera_target = [0.0, 0.0]
            self.ground = []
            self.hitsplats = []
        self.observe("scene.reset", "back at the bank")

    # ==================================================================
    # input
    # ==================================================================
    def handle_key(self, key: str, action: str) -> None:
        """A key press/release arriving from `keyboard` or `pynput`."""
        key = (key or "").lower()
        with self.lock:
            if key == "shift":
                self.shift_down = (action == "press")
                return
            if action != "press":
                return

            self.observe("key", key)
            if key == "2":
                self.tab = "inventory"
                self.toast("inventory tab")
            elif key == "3":
                self.tab = "prayer"
                self.toast("prayer tab")
            elif key == "4":
                self.tab = "spellbook"
                self.toast("spellbook tab")
            elif key == "insert":
                self._take_screenshot()
            elif key == "`":
                self._toggle_chat("` key")
            elif key == "j":
                self.toast("run energy toggled")
            elif key in ("k", "l"):
                # keys the recorded route presses (camera / prayer hotkeys)
                self.toast(f"hotkey {key}")

    def handle_click(self, cx: int, cy: int, button: str = "left",
                     action: str = "click") -> None:
        """A mouse button event, already converted to canvas coordinates."""
        if action not in ("click", "press"):
            return
        with self.lock:
            self.observe("click", f"({cx},{cy})", x=cx, y=cy, button=button,
                         shift=self.shift_down)
            if button != "left":
                self.toast("right click menu")
                return
            self._dispatch_left_click(cx, cy)

    # -- click routing ----------------------------------------------------
    def _dispatch_left_click(self, cx: int, cy: int) -> None:
        # 1) the chat box (it is drawn on top of the world)
        if self.chat_open and CHAT_BOX.contains(cx, cy):
            if CHAT_ALL_BUTTON.contains(cx, cy):
                self._toggle_chat("'All' button")
            return

        # 2) the side panel: tabs, inventory slots or spellbook icons
        if INVENTORY_PANEL.contains(cx, cy) or TAB_ROW.contains(cx, cy):
            self._click_panel(cx, cy)
            return

        # 3) orbs
        if PRAYER_ORB.inset(-6).contains(cx, cy):
            self._click_prayer_orb()
            return
        if MINIMAP.contains(cx, cy) or COMPASS_ORB.contains(cx, cy):
            self.observe("minimap")
            self.toast("minimap click")
            return

        # 4) the world
        self._click_world(cx, cy)

    def _click_panel(self, cx: int, cy: int) -> None:
        if TAB_ROW.contains(cx, cy):
            index = (cx - TAB_ROW.x) * 3 // TAB_ROW.w
            self.tab = ("inventory", "spellbook", "prayer")[max(0, min(2, index))]
            self.observe("tab", self.tab)
            self.toast(f"{self.tab} tab")
            return

        if self.tab == "spellbook":
            for col in range(SPELL_COLS):
                for row in range(SPELL_ROWS):
                    scx, scy = self.spell_center(col, row)
                    if Box(scx - 18, scy - 18, 36, 36).contains(cx, cy):
                        self._cast_spell(col, row)
                        return
            return

        if self.tab != "inventory":
            return

        pos = self.slot_at(cx, cy)
        if pos is None:
            self.toast("panel background")
            return
        item = self.slots[pos]
        if item is None:
            self.observe("slot.empty", str(pos))
            self.toast("empty slot")
            return
        self._use_item(pos, item)

    def _use_item(self, pos: Tuple[int, int], item: Item) -> None:
        col, row = pos
        if self.shift_down:
            if item.droppable:
                self.slots[pos] = None
                self.observe("item.dropped", item.name, col=col, row=row)
                self.chat_say(f"You drop the {item.name}.", GAME_TEXT_DIM)
                self.toast(f"dropped {item.name}")
            else:
                self.observe("item.not_droppable", item.name, col=col, row=row)
                self.toast(f"{item.name} is not droppable")
            return

        if item.kind == "pouch":
            self.observe("pouch.emptied", item.name, col=col, row=row)
            self.chat_say("You empty your coin pouch.", GAME_TEXT_DIM)
            self.toast("coin pouch emptied")
        elif item.kind == "necklace":
            self.slots[pos] = None
            self.dodgy_worn = True
            self.observe("necklace.worn", item.name, col=col, row=row)
            self.chat_say("You put on a dodgy necklace.", GAME_TEXT_DIM)
            self.toast("dodgy necklace worn")
        elif item.kind == "brew":
            item.doses -= 1
            self.hp = min(self.max_hp, self.hp + 18)
            self.prayer_points = min(float(self.max_prayer),
                                     self.prayer_points + 26)
            if item.doses <= 0:
                self.slots[pos] = None
            self.observe("brew.drunk", item.name, col=col, row=row,
                         doses=item.doses)
            self.chat_say("You drink some of your super restore.", GAME_TEXT_DIM)
            self.toast("brew sipped")
        else:
            self.observe("item.used", item.name, col=col, row=row)
            self.toast(f"clicked {item.name}")

    def _cast_spell(self, col: int, row: int) -> None:
        if (col, row) == SHADOW_VEIL_CELL:
            self.veil_active = True
            self.observe("veil.cast")
            self.chat_say("You cast Shadow Veil.", VEIL_PURPLE)
            self.toast("Shadow Veil cast")
        else:
            self.observe("spell.other", f"({col},{row})")
            self.toast("spell clicked")

    def _click_prayer_orb(self) -> None:
        self.prayer_on = True
        self.prayer_points = max(self.prayer_points, 40.0)
        self.observe("prayer.on")
        self.chat_say("Protect from Melee activated.", (146, 196, 232))
        self.toast("prayer on")

    def _click_world(self, cx: int, cy: int) -> None:
        # ground items first: the pick-up clicks just under the label's centre
        for item in self.ground:
            if not item.visible or item.claimed:
                continue
            gx, gy = self.world_to_canvas(*item.world)
            if Box(gx - 40, gy - 40, 80, 74).contains(cx, cy):
                self._claim_ground_item(item, cx, cy)
                return

        tx, ty = self.world_to_canvas(TELEPORT_TILE.x, TELEPORT_TILE.y)
        if Box(tx - 4, ty - 4, TELEPORT_TILE.w + 8,
               TELEPORT_TILE.h + 8).contains(cx, cy):
            self._teleport()
            return

        nx, ny = self.world_to_canvas(*self.npc)
        if Box(nx - 26, ny - 26, 52, 52).contains(cx, cy):
            self._attack_npc()
            return

        # anything else: walk there (clamped to the ground the camera shows)
        self._walk_to(cx - self.camera[0], cy - self.camera[1])
        self.observe("walk", f"({cx},{cy})")

    def _walk_to(self, wx: float, wy: float) -> None:
        self.player_target = (float(min(max(wx, WALKABLE.x), WALKABLE.x1)),
                              float(min(max(wy, WALKABLE.y), WALKABLE.y1)))

    def _attack_npc(self) -> None:
        self.attacks += 1
        self.npc_hp = max(0.05, self.npc_hp - 0.06)
        damage = self.rng.randint(4, 28)
        nx, ny = self.world_to_canvas(*self.npc)
        self.hitsplats.append((nx + self.rng.randint(-8, 8), ny, damage,
                               time.monotonic()))
        del self.hitsplats[:-6]
        self.xp += damage * 4
        self.observe("attack", self.npc_name, attacks=self.attacks)
        if self.npc_hp <= 0.1:
            self.npc_hp = 1.0
            self.kills += 1
            self.observe("kill", self.npc_name, kills=self.kills)
            self.chat_say(f"You defeat the {self.npc_name}.", GAME_TEXT_DIM)

    def _claim_ground_item(self, item: GroundItem, cx: int, cy: int) -> None:
        """Start walking onto the pile; `update()` finishes the pick-up.

        The label stays on screen until the player actually gets there, and the
        camera lags behind - which is exactly why the bot waits
        `drop.pickup_settle` (3 s) and re-scans instead of clicking twice.
        """
        item.claimed = True
        self._walk_to(item.world[0], item.world[1] - 10)
        self.camera_target = [self.camera[0] + self.rng.randint(-9, 9),
                              self.camera[1] + self.rng.randint(-7, 7)]
        self.observe("drop.clicked", item.name, x=cx, y=cy)
        self.toast(f"walking to {item.name}")

    def _finish_pickup(self, item: GroundItem) -> None:
        item.taken = True
        free = self.free_slots()
        if free:
            self.slots[free[0]] = Item(item.name, "loot", LOOT_ICON, (26, 26))
            self.observe("drop.taken", item.name)
            self.chat_say(f"You take the {item.name}.", DROP_TEXT)
            self.toast(f"picked up {item.name}")
        else:
            self.observe("drop.no_space", item.name)
            self.chat_say("You do not have enough inventory space.", (226, 120, 96))

    def _teleport(self) -> None:
        previous = self.scene
        self.scene = "spot"
        self.player = [float(PLAYER_HOME[0]), float(PLAYER_HOME[1])]
        self.player_target = None
        self.camera = [0.0, 0.0]
        self.camera_target = [0.0, 0.0]
        self._flash_until = time.monotonic() + 0.5
        self.observe("teleport", f"{previous} -> {self.scene}")
        self.chat_say("You teleport to the catacombs.", VEIL_PURPLE)
        self.toast("teleported")

    # -- screenshot / chat -------------------------------------------------
    def _take_screenshot(self) -> None:
        self.screenshots += 1
        self._flash_until = time.monotonic() + 0.35
        self.observe("screenshot", f"#{self.screenshots}")
        self.toast("screenshot saved")
        self.chat_say("Screenshot saved and uploaded.", GAME_TEXT_DIM)
        # The behaviour vision.ChatWatcher exists for: RuneLite's screenshot
        # hotkey pops the chat box open again a couple of seconds later.
        self._chat_open_at = time.monotonic() + 2.5
        self.relay_queue.append("[screenshot] EmuAcct uploaded a screenshot.")

    def _toggle_chat(self, why: str) -> None:
        self.chat_open = not self.chat_open
        self._chat_open_at = None
        self.observe("chat.toggle",
                     f"{'opened' if self.chat_open else 'closed'} ({why})",
                     open=self.chat_open, why=why)
        self.toast(f"chat {'opened' if self.chat_open else 'closed'}")

    def open_chat(self, why: str = "scripted") -> None:
        with self.lock:
            if not self.chat_open:
                self._toggle_chat(why)

    # ==================================================================
    # time based updates
    # ==================================================================
    def update(self, now: Optional[float] = None) -> None:
        now = now or time.monotonic()
        with self.lock:
            dt = max(0.0, min(0.25, now - self._last_update))
            self._last_update = now

            if self._chat_open_at is not None and now >= self._chat_open_at:
                self._chat_open_at = None
                if not self.chat_open:
                    self.chat_open = True
                    self.observe("chat.autoopen", "after screenshot", open=True)
                    self.chat_say("[chat popped open after the screenshot]",
                                  GAME_TEXT_DIM)

            # the player walks towards its target
            if self.player_target is not None:
                px, py = self.player
                tx, ty = self.player_target
                dx, dy = tx - px, ty - py
                distance = math.hypot(dx, dy)
                step = WALK_SPEED * dt
                if distance <= max(3.0, step):
                    self.player = [tx, ty]
                    self.player_target = None
                else:
                    self.player = [px + dx / distance * step,
                                   py + dy / distance * step]

            # arriving on a claimed pile picks it up
            if self.player_target is None:
                for item in self.ground:
                    if item.claimed and not item.taken:
                        self._finish_pickup(item)

            # the camera drifts after the player (trailing, on purpose)
            for axis in (0, 1):
                delta = self.camera_target[axis] - self.camera[axis]
                if abs(delta) > 0.2:
                    self.camera[axis] += delta * min(1.0, 3.0 * dt)

            if self.prayer_on and self.prayer_points > 0:
                self.prayer_points = max(0.0, self.prayer_points - 0.12 * dt)

    # ==================================================================
    # rendering
    # ==================================================================
    def render_window(self) -> np.ndarray:
        """The whole window: RuneLite's chrome plus the rendered canvas."""
        surface = R.new_surface(WINDOW_W, WINDOW_H, R.WINDOW_CHROME)
        # The title bar is kept sparse so GameWindow._refine_canvas still sees
        # these rows as "mostly chrome" and finds the canvas underneath them.
        R.text(surface, WINDOW_TITLE, (10, 18), 0.44, R.TITLE_TEXT, 1)
        for index, tint in enumerate(((150, 74, 74), (92, 96, 104), (92, 96, 104))):
            R.fill(surface, Box(WINDOW_W - 20 - index * 22, 8, 12, 10), tint)
        R.blit(surface, self.render_canvas(), INSET_L, INSET_T)
        return surface

    def render_canvas(self) -> np.ndarray:
        """The 947x650 rendered game area - what `ImageGrab` will capture."""
        with self.lock:
            img = R.new_surface(CANVAS_W, CANVAS_H, GROUND_B)
            self._draw_world(img)
            self._draw_overlays(img)
            self._draw_side_panel(img)
            self._draw_orbs(img)
            if self.chat_open:
                self._draw_chat(img)
            self._draw_flash(img)
            return img

    # -- world -------------------------------------------------------------
    def _draw_world(self, img: np.ndarray) -> None:
        cave = self.scene == "spot"
        base_a, base_b, accent = ((CAVE_A, CAVE_B, CAVE_C) if cave
                                  else (GROUND_A, GROUND_B, GROUND_C))
        ox, oy = int(self.camera[0]), int(self.camera[1])

        # tiled floor
        tile = 38
        for gy in range(-1, CANVAS_H // tile + 2):
            for gx in range(-1, CANVAS_W // tile + 2):
                x = gx * tile + ox % tile
                y = gy * tile + oy % tile
                color = base_a if (gx + gy) % 2 == 0 else base_b
                cv2.rectangle(img, (x, y), (x + tile - 1, y + tile - 1), color, -1)
        # a couple of paths / rock veins so it does not look like a checkerboard
        for index in range(6):
            x = (index * 173 + 40 + ox) % (CANVAS_W + 120) - 60
            cv2.rectangle(img, (x, 0), (x + 22, CANVAS_H - 1), accent, -1)

        if cave:
            self._draw_props(img, [(120, 120), (620, 150), (200, 420), (560, 470)],
                             PROP_ROCK, "rock")
            R.text(img, "CATACOMBS OF EMULATION - multi combat", (16, 28), 0.44,
                   GAME_TEXT_DIM, 1, shadow=True)
        else:
            self._draw_props(img, [(140, 150), (520, 120), (250, 430), (600, 400)],
                             PROP_TREE, "tree")
            R.fill(img, Box(60 + ox, 60 + oy, 260, 26), WALL)
            R.text(img, "EMU BANK - deposit box", (68 + ox, 80 + oy), 0.42,
                   GAME_TEXT_DIM, 1, shadow=True)
            R.text(img, "GIELINOR BANK PLAZA", (16, 28), 0.44, GAME_TEXT_DIM, 1,
                   shadow=True)

        # The marked teleport tile: solid black, and the bot clicks the largest
        # black blob on the canvas between the two legs.
        tx, ty = self.world_to_canvas(TELEPORT_TILE.x, TELEPORT_TILE.y)
        tile_box = Box(tx, ty, TELEPORT_TILE.w, TELEPORT_TILE.h)
        R.fill(img, tile_box, R.RESERVED["black"])
        R.text(img, "teleport", (tile_box.x - 1, tile_box.y - 5), 0.36,
               GAME_TEXT_DIM, 1, shadow=True)

        # ground items + the pink label the Ground Items plugin paints
        for item in self.ground:
            if not item.visible:
                continue
            gx, gy = self.world_to_canvas(*item.world)
            R.fill(img, Box(gx - 9, gy - 7, 18, 14), LOOT_ICON)
            width = R.text_size(item.label, R.OCR_SCALE, R.OCR_THICKNESS,
                                R.OCR_FONT)[0]
            R.draw_text_colored(img, item.label, (gx - width // 2, gy - 30),
                                R.DROP_LABEL)

        # the player and its purple true-tile marker
        px, py = self.world_to_canvas(*self.player)
        R.hollow_highlight(img, Box(px - 23, py - 2, 46, 30),
                           R.RESERVED["purple"], 2)
        self._draw_character(img, px, py, (86, 118, 176), (206, 178, 140))
        R.text(img, "EmuAcct", (px - 24, py - 42), 0.36, XP_GOLD, 1, shadow=True)

        # The target NPC: a solid red blob, exactly what largest_solid('red')
        # is looking for.
        nx, ny = self.world_to_canvas(*self.npc)
        R.text(img, self.npc_name, (nx - 30, ny - 28), 0.36, GAME_TEXT, 1,
               shadow=True)
        self._draw_character(img, nx, ny + 30, (58, 46, 70), (176, 148, 120))
        R.fill(img, Box(nx - 21, ny - 21, 42, 42), R.RESERVED["red"])
        R.progress_bar(img, Box(nx - 21, ny + 46, 42, 5), self.npc_hp,
                       HP_GREEN, HP_RED)

        # Hitsplats float *above* the red box: drawn across it they would split
        # the blob and shift its centre, which the bot would read as "the target
        # moved".
        for hx, hy, damage, when in list(self.hitsplats):
            age = time.monotonic() - when
            if age > 1.4:
                continue
            y = int(hy - 38 - age * 14)
            R.fill(img, Box(hx - 11, y - 9, 22, 18), HP_RED)
            R.text(img, str(damage), (hx - 5, y + 5), 0.38, GAME_TEXT, 1)

    @staticmethod
    def _draw_props(img: np.ndarray, spots, color, kind: str) -> None:
        for x, y in spots:
            if kind == "tree":
                R.fill(img, Box(x - 5, y, 10, 34), PROP_TRUNK)
                cv2.circle(img, (x, y - 6), 26, color, -1, cv2.LINE_AA)
            else:
                cv2.circle(img, (x, y), 20, color, -1, cv2.LINE_AA)
                cv2.circle(img, (x, y), 20, (72, 72, 80), 2, cv2.LINE_AA)

    @staticmethod
    def _draw_character(img: np.ndarray, x: int, y: int, body, skin) -> None:
        R.fill(img, Box(x - 8, y - 20, 16, 22), body)
        cv2.circle(img, (x, y - 26), 7, skin, -1, cv2.LINE_AA)
        R.fill(img, Box(x - 8, y + 2, 6, 12), (54, 48, 44))
        R.fill(img, Box(x + 2, y + 2, 6, 12), (54, 48, 44))

    # -- HUD overlays ------------------------------------------------------
    def _draw_overlays(self, img: np.ndarray) -> None:
        R.text(img, f"XP: {self.xp:,}", (16, CANVAS_H - 16), 0.42, XP_GOLD, 1,
               shadow=True)
        status = [f"kills {self.kills}", f"attacks {self.attacks}",
                  f"veil {'ON' if self.veil_active else 'FADED'}",
                  f"dodgy {'worn' if self.dodgy_worn else 'GONE'}",
                  f"scene {self.scene}"]
        R.text(img, "   |   ".join(status), (16, 48), 0.4, GAME_TEXT_DIM, 1,
               shadow=True)
        if self._toast and time.monotonic() < self._toast[1]:
            message = self._toast[0]
            width = R.text_size(message, 0.44, 1)[0] + 20
            box = Box(CANVAS_W // 2 - width // 2 - 90, 60, width, 26)
            R.blend_rect(img, box, (18, 22, 28), 0.72)
            R.outline(img, box, (120, 132, 150), 1)
            R.text(img, message, (box.x + 10, box.y1 - 8), 0.44, GAME_TEXT, 1)

    def _draw_flash(self, img: np.ndarray) -> None:
        if time.monotonic() >= self._flash_until:
            return
        # A screenshot flash: a frame, never a full white fill - that would hand
        # the bot a gigantic "white" blob to click.
        R.outline(img, Box(0, 0, CANVAS_W, CANVAS_H), (232, 226, 180), 6)

    # -- side panel --------------------------------------------------------
    def _draw_side_panel(self, img: np.ndarray) -> None:
        R.fill(img, TAB_ROW, PANEL_WOOD)
        R.outline(img, TAB_ROW, PANEL_EDGE, 1)
        for index, name in enumerate(("Inventory", "Spellbook", "Prayer")):
            box = Box(TAB_ROW.x + index * (TAB_ROW.w // 3), TAB_ROW.y,
                      TAB_ROW.w // 3, TAB_ROW.h)
            active = self.tab == name.lower()
            R.fill(img, box.inset(2), (86, 72, 56) if active else (52, 45, 38))
            R.text(img, name, (box.x + 6, box.y1 - 9), 0.36,
                   GAME_TEXT if active else GAME_TEXT_DIM, 1)

        R.fill(img, INVENTORY_PANEL, PANEL_WOOD)
        R.outline(img, INVENTORY_PANEL, PANEL_EDGE, 1)

        if self.tab == "inventory":
            self._draw_inventory(img)
        elif self.tab == "spellbook":
            self._draw_spellbook(img)
        else:
            self._draw_prayers(img)

    def _draw_inventory(self, img: np.ndarray) -> None:
        for (col, row), item in sorted(self.slots.items()):
            box = self.slot_box(col, row)
            R.fill(img, box.inset(1), SLOT_BG)
            if item is None:
                continue
            w, h = item.size
            icon = Box(box.cx - w // 2, box.cy - h // 2, w, h)
            # Flat, exact colour: the vision layer matches these *exactly*, and
            # the necklaces/brews have to stay the same size as each other for
            # `equal_largest_solids` to return all of them.
            R.fill(img, icon, item.color)
            if item.kind == "brew":
                R.fill(img, Box(icon.cx - 3, icon.y - 4, 6, 4), (96, 78, 44))
            if item.doses:
                R.text(img, str(item.doses), (box.x + 3, box.y1 - 3), 0.3,
                       GAME_TEXT_DIM, 1)

        # The blue "inventory anchor" highlight box; config.DERIVED_REGIONS
        # derives the junk slots and the Shadow Veil icon from it.
        R.hollow_highlight(img, self.anchor_box, R.RESERVED["blue"], 2)

    def _draw_spellbook(self, img: np.ndarray) -> None:
        for row in range(SPELL_ROWS):
            for col in range(SPELL_COLS):
                cx, cy = self.spell_center(col, row)
                box = Box(cx - 15, cy - 15, 30, 30)
                is_veil = (col, row) == SHADOW_VEIL_CELL
                R.fill(img, box, (74, 62, 96) if is_veil else (58, 52, 46))
                R.outline(img, box, (128, 108, 168) if is_veil else (86, 76, 62), 1)
                if is_veil:
                    cv2.circle(img, (cx, cy), 8, VEIL_PURPLE, -1, cv2.LINE_AA)
                    R.text(img, "veil", (box.x + 4, box.y1 + 11), 0.3,
                           VEIL_PURPLE, 1)
        R.text(img, "Arceuus spellbook",
               (INVENTORY_PANEL.x + 8, INVENTORY_PANEL.y1 - 8), 0.36,
               GAME_TEXT_DIM, 1)

    def _draw_prayers(self, img: np.ndarray) -> None:
        for row in range(SPELL_ROWS):
            for col in range(SPELL_COLS):
                cx, cy = self.spell_center(col, row)
                box = Box(cx - 15, cy - 15, 30, 30)
                R.fill(img, box, (60, 58, 48))
                R.outline(img, box, (92, 88, 70), 1)
        R.text(img, "Prayers", (INVENTORY_PANEL.x + 8, INVENTORY_PANEL.y1 - 8),
               0.36, GAME_TEXT_DIM, 1)

    # -- orbs --------------------------------------------------------------
    def _draw_orbs(self, img: np.ndarray) -> None:
        R.fill(img, MINIMAP, (44, 46, 40))
        R.outline(img, MINIMAP, PANEL_EDGE, 1)
        self._draw_minimap_dots(img)

        R.fill(img, COMPASS_ORB, (52, 50, 44))
        R.text(img, "N", (COMPASS_ORB.cx - 5, COMPASS_ORB.cy + 5), 0.42,
               GAME_TEXT_DIM, 1)

        # Prayer orb: a *boxed* yellow highlight (largest_boxed('yellow')).
        R.fill(img, PRAYER_ORB.inset(3),
               (86, 122, 158) if self.prayer_on else (58, 58, 62))
        R.hollow_highlight(img, PRAYER_ORB, R.RESERVED["yellow"], 2)
        R.text(img, str(int(self.prayer_points)),
               (PRAYER_ORB.x + 6, PRAYER_ORB.y1 - 11), 0.36, GAME_TEXT, 1)

        R.fill(img, HP_ORB.inset(3), (96, 52, 48))
        R.outline(img, HP_ORB, (128, 92, 74), 2)
        R.text(img, str(int(self.hp)), (HP_ORB.x + 6, HP_ORB.y1 - 11), 0.36,
               GAME_TEXT, 1)

    @staticmethod
    def _draw_minimap_dots(img: np.ndarray) -> None:
        """A static, deterministic minimap doodle (no per-frame randomness)."""
        for index in range(14):
            x = MINIMAP.x + 12 + (index * 37) % (MINIMAP.w - 24)
            y = MINIMAP.y + 14 + (index * 53) % (MINIMAP.h - 28)
            cv2.circle(img, (x, y), 3, (120, 132, 104), -1)
        cv2.circle(img, (MINIMAP.cx, MINIMAP.cy), 4, (226, 226, 200), -1)

    # -- chat --------------------------------------------------------------
    def _draw_chat(self, img: np.ndarray) -> None:
        """The chat box: the reason `vision.ChatWatcher` exists.

        It covers the bottom-left of the world (including anything lying on the
        floor there), so the bot has to notice it and close it before it can
        find a ground-item label.
        """
        R.blend_rect(img, CHAT_BOX, CHAT_BG, 0.9)
        R.outline(img, CHAT_BOX, (68, 60, 48), 1)

        y = CHAT_BOX.y + 18
        for line, color in self.chat_lines[-9:]:
            R.text(img, line[:74], (8, y), 0.38, color, 1)
            y += 14
            if y > CHAT_PROMPT_RECT.y - 6:
                break

        # "Press Enter to Chat" - near-white text on the dark chat background,
        # inside config.CHAT["prompt_rect"] (2, 600, 230, 26).  It is painted
        # through render.draw_text_colored so the OCR stand-in can recognise it
        # by re-rendering the same phrase.
        R.draw_text_colored(img, CHAT_PROMPT_TEXT,
                            (CHAT_PROMPT_RECT.x + 4, CHAT_PROMPT_RECT.y + 6),
                            (236, 236, 232))

        for index, name in enumerate(CHAT_TABS):
            box = Box(CHAT_ALL_BUTTON.x + index * (CHAT_ALL_BUTTON.w + 4),
                      CHAT_ALL_BUTTON.y, CHAT_ALL_BUTTON.w, CHAT_ALL_BUTTON.h)
            R.fill(img, box, (62, 54, 42) if index == 0 else (46, 40, 32))
            R.outline(img, box, (96, 84, 64), 1)
            R.text(img, name, (box.x + 6, box.y1 - 7), 0.34, GAME_TEXT_DIM, 1)

    # ==================================================================
    # self check
    # ==================================================================
    def expected_color_boxes(self) -> Dict[str, List[Box]]:
        """Where each reserved colour is allowed to appear (for the audit)."""
        nx, ny = self.world_to_canvas(*self.npc)
        px, py = self.world_to_canvas(*self.player)
        tx, ty = self.world_to_canvas(TELEPORT_TILE.x, TELEPORT_TILE.y)
        boxes: Dict[str, List[Box]] = {
            "red": [Box(nx - 24, ny - 24, 48, 48)],
            "purple": [Box(px - 26, py - 5, 52, 36)],
            "yellow": [PRAYER_ORB.inset(-3)],
            "blue": [self.anchor_box.inset(-3)],
            "black": [Box(tx - 3, ty - 3, TELEPORT_TILE.w + 6,
                          TELEPORT_TILE.h + 6)],
            "white": [], "orange": [], "cyan": [],
            "drop_label": [],
        }
        for hx, hy, _damage, _when in self.hitsplats:
            boxes["red"].append(Box(hx - 14, hy - 70, 28, 60))
        if self.tab == "inventory":
            for (col, row), item in sorted(self.slots.items()):
                if item is None:
                    continue
                box = self.slot_box(col, row)
                if item.kind == "necklace":
                    boxes["white"].append(box)
                elif item.kind == "brew":
                    boxes["orange"].append(box)
                elif item.kind == "pouch":
                    boxes["cyan"].append(box)
        for item in self.ground:
            if not item.visible:
                continue
            gx, gy = self.world_to_canvas(*item.world)
            boxes["drop_label"].append(Box(gx - 200, gy - 48, 400, 44))
        return boxes

    def audit(self) -> List[str]:
        """Is every reserved-colour pixel part of a real game element?"""
        return R.audit_canvas(self.render_canvas(), self.expected_color_boxes())

    # ==================================================================
    # a compact state dump for the test HUD
    # ==================================================================
    def summary(self) -> List[Tuple[str, str]]:
        with self.lock:
            necklaces = len(self.items_of("necklace"))
            brews = sum(item.doses for _pos, item in self.items_of("brew"))
            junk = len(self.items_of("junk"))
            loot = len(self.items_of("loot"))
            return [
                ("scene", self.scene),
                ("tab", self.tab),
                ("chat", "OPEN" if self.chat_open else "closed"),
                ("prayer", "ON" if self.prayer_on else "OFF"),
                ("veil", "active" if self.veil_active else "FADED"),
                ("dodgy", "worn" if self.dodgy_worn else "GONE"),
                ("necklaces", str(necklaces)),
                ("brew doses", str(brews)),
                ("junk/loot", f"{junk}/{loot}"),
                ("free slots", str(len(self.free_slots()))),
                ("attacks", str(self.attacks)),
                ("screenshots", str(self.screenshots)),
                ("on floor", str(sum(1 for g in self.ground if g.visible))),
            ]
