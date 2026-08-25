"""
config.py -- every knob of the colour-bot lives here.
=====================================================

This is the ONLY file you should need to touch for day to day tweaking:

    * Discord token / channel / user id            -> DISCORD
    * which routes exist and which .json they use  -> ROUTES
    * key press order / mouse click order          -> SEQUENCES
    * every single sleep in the script             -> DELAYS
    * colours the vision code looks for            -> COLORS
    * where the game window is / how it is found   -> GAME_WINDOW
    * valuable drop pick-up behaviour              -> DROP
    * chat-window watchdog                         -> CHAT

Nothing in here imports the rest of the project, so it can never create an
import cycle and you can `python -c "import config"` to syntax check it.

A note on the timing regime (read this before you change a sleep!)
-----------------------------------------------------------------
The old scripts had two different timing systems living side by side:

    1. `time.sleep(random.uniform(a, b))` statements sprinkled through the code.
    2. Absolute timestamps stored inside the recorded route .json files.

Both are now expressed with the *same* vocabulary: a "delay spec" (`Fixed`,
`Uniform`, `Gauss`).  Every wait in the program is looked up by name from the
DELAYS table below and sampled by `core.Clock`, and the recorded routes are
replayed by `core.InputController.play_timeline()` which schedules the recorded
deltas against the same clock.  So: one clock, one table, no magic numbers
buried in the logic.

The numbers below are a 1:1 copy of what the original scripts did - every
unique sleep statement kept its own entry (and therefore its own random
distribution).  Renaming/retiming a step is now a one line change.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# 1. Tiny data types used by the tables further down
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fixed:
    """Always waits exactly `seconds` (old `time.sleep(1)`)."""
    seconds: float


@dataclass(frozen=True)
class Uniform:
    """Uniform random wait (old `time.sleep(random.uniform(lo, hi))`)."""
    lo: float
    hi: float


@dataclass(frozen=True)
class Gauss:
    """Normal-distribution wait (old `random.gauss(mean, stddev)`)."""
    mean: float
    stddev: float


# -- declarative steps, used by SEQUENCES ------------------------------------
# The route-replay phase is a fixed list of "do this, then that" steps, so it
# is expressed as data instead of code.  main.py:StepRunner knows how to run
# each of these four step types; add a new type there if you need one.

@dataclass(frozen=True)
class Wait:
    """Sleep using DELAYS[delay]."""
    delay: str


@dataclass(frozen=True)
class TapKey:
    """Press a key, hold it for DELAYS[hold], release it, then wait DELAYS[after]."""
    key: str
    hold: str
    after: Optional[str] = None
    note: str = ""


@dataclass(frozen=True)
class ClickLargestSolid:
    """Find the largest solid blob of `color` inside the game window and click it once.

    `what` is only used for logging so the console tells you *why* it clicked.
    `optional=True` mirrors the old behaviour of silently skipping the click
    when the colour is not on screen (e.g. an already empty coin pouch).
    """
    color: str
    what: str
    optional: bool = True


@dataclass(frozen=True)
class Log:
    """Print a line (kept so the console output matches the old scripts)."""
    message: str


@dataclass(frozen=True)
class RouteProfile:
    """Everything that makes one route different from another.

    legs            : recorded .json files, replayed in this order.
    preamble        : sequence run *before every* leg (see SEQUENCES).
    between_legs    : sequence run between two legs (None for single-leg routes).
    after_last_leg  : sequence run once the last leg finished, right before the
                      common case (red-clicking loop) starts.
    drop_keyword    : substring the Discord relay prints when the valuable drop
                      is rolled -> arms the pick-up routine.
    drop_item_name  : the *on-ground* item label, used by the OCR to find the
                      pile on the floor.  Must match the in-game name.
    expected_drops  : how many of them normally hit the floor (used as the
                      pick-up attempt budget).
    """
    description: str
    legs: Sequence[str]
    preamble: Optional[str] = "leg_preamble"
    between_legs: Optional[str] = None
    after_last_leg: Optional[str] = "arrive_at_spot"
    drop_keyword: str = "teleport"
    drop_item_name: str = "Enhanced crystal teleport seed"
    expected_drops: int = 2


# ---------------------------------------------------------------------------
# 2. General / process wide
# ---------------------------------------------------------------------------

GENERAL = {
    # Persistent "how long did this account survive" stopwatch.  It is *not*
    # reset by a crash, a !restart, an ESC kill or a reboot - the file keeps
    # accumulating until you pass --reset-runtime.
    "runtime_file": "runtime_total.json",
    "runtime_flush_seconds": 10,      # how often the stopwatch is written to disk

    # Console + file logging.  Set "log_file": None to only log to the console.
    "log_file": "colourbot.log",
    "log_level": "INFO",              # DEBUG for very chatty vision logs

    # After an unhandled error the supervisor restarts the whole flow from the
    # top with the same CLI arguments; this is how long it waits first.
    "restart_backoff_seconds": 5.0,

    # Panic key.  Held down anywhere on the desktop -> process dies (legacy ESC).
    "panic_key": "esc",

    # Full path to the Tesseract binary (used by the chat watchdog and the
    # valuable-drop OCR).  Leave None when tesseract is on PATH; on Windows it
    # usually is not, so point it at e.g.
    #   r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    "tesseract_cmd": None,
}


# ---------------------------------------------------------------------------
# 3. Game window / coordinate system
# ---------------------------------------------------------------------------
# The old scripts grabbed the whole desktop, so the "largest black blob" could
# easily be a terminal or an IDE and the bot happily clicked Windows UI.
# Everything is now clipped to the game *canvas* (the rendered client area,
# excluding RuneLite's own title bar), and all vision coordinates are canvas
# relative.  core.GameWindow does the translation back to screen coordinates.
#
# The recorded routes hold absolute screen coordinates captured while the
# canvas sat at REFERENCE_CANVAS_ORIGIN (that is the layout in screenshot.png).
# At startup we work out where the canvas is *now* and shift every recorded
# coordinate by the difference, so the .json files never have to be re-recorded.

GAME_WINDOW = {
    # Window title match (case insensitive substring).  First match wins.
    "title_contains": ["RuneLite"],

    # Canvas geometry of the reference layout (measured from screenshot.png).
    "reference_canvas_origin": (969, 227),
    "canvas_size": (947, 650),

    # RuneLite draws its own title bar *inside* the window, so the canvas is
    # inset from the window rectangle by this much: (left, top, right, bottom).
    "window_insets": (4, 26, 4, 4),

    # Colour of RuneLite's window chrome; used to auto-refine the canvas origin
    # (handy because Windows sometimes reports invisible resize borders).
    "chrome_color": (30, 30, 30),
    "chrome_tolerance": 6,
    "auto_refine_canvas": True,
    "auto_refine_max_shift": 20,      # px; refuse "refinements" larger than this

    # Safety net: never let a click land outside the canvas.
    "clamp_clicks_to_canvas": True,
    # Recorded routes start with the cursor parked over the terminal, i.e.
    # outside the game.  Those are pure moves (no clicks) so by default we
    # replay them untouched; flip this if you want the pointer to stay glued
    # to the game window at all times.
    "clamp_replayed_moves_to_canvas": False,
}


# ---------------------------------------------------------------------------
# 4. Colours the vision layer looks for
# ---------------------------------------------------------------------------
# These are RuneLite plugin highlight colours, i.e. flat RGB, which is why an
# exact match is used (tolerance 0 = identical behaviour to the old scripts).

COLORS = {
    "red": (255, 0, 0),        # the target NPC/area that gets clicked
    "yellow": (255, 250, 0),   # prayer orb (boxed)
    "blue": (0, 67, 255),      # inventory anchor box (boxed)
    "purple": (231, 0, 255),   # player tile (boxed) - only used by legacy drop code
    "orange": (255, 154, 0),   # brew doses in the inventory
    "cyan": (0, 255, 241),     # coin pouch
    "white": (255, 255, 255),  # dodgy necklaces
    "black": (0, 0, 0),        # teleport tile clicked between the two legs
}

COLOR_TOLERANCE = 0            # 0 = exact RGB match (legacy). 1-10 = fuzzy.


# ---------------------------------------------------------------------------
# 5. Regions derived from the blue anchor box
# ---------------------------------------------------------------------------
# The old code did arithmetic like `boxed_blue_avg_y + 101` inline.  Same
# numbers, but now they have names and live in one place.  All values are pixel
# offsets from the detected blue box (centre offset + bounds offsets).

@dataclass(frozen=True)
class RegionOffset:
    what: str
    dcx: int
    dcy: int
    dx0: int
    dx1: int
    dy0: int
    dy1: int


DERIVED_REGIONS = {
    # Shadow Veil spell icon, one row below the blue box.
    "shadow_veil": RegionOffset("shadow veil spell", -3, 101, -2, -14, 98, 100),
    # The two junk inventory slots that get shift-dropped to free space.
    "junk_slot_1": RegionOffset("junk inventory slot 1", 1, 39, 4, -2, 42, 36),
    "junk_slot_2": RegionOffset("junk inventory slot 2", 43, 39, 46, 40, 42, 36),
}


# ---------------------------------------------------------------------------
# 6. Vision / detection loop behaviour
# ---------------------------------------------------------------------------

VISION = {
    # How far (px) the red blob must move before we treat it as "target moved".
    "target_move_threshold_px": 50,

    # Pause between two frames of the "did the target move?" watcher.
    #
    # Legacy note: that loop had no sleep at all - it simply ran as fast as a
    # pure-python flood fill over a 1920x1080 grab allowed, i.e. roughly one
    # frame per second on the reference machine.  The detector is now vectorised
    # (numpy/OpenCV) and only scans the game canvas, so it would spin ~30x
    # faster and react to target movement much sooner than the original.  The
    # explicit interval below keeps the original cadence; set it to 0.0 if you
    # *want* the faster reactions.
    "scan_interval_seconds": 0.8,

    # Poll interval of the Discord-message watcher thread.  The legacy loop was
    # a busy spin (100% of a core); 10 ms costs nothing and is invisible in game.
    "event_poll_seconds": 0.01,

    # Tolerance used when grouping "all roughly equally large blobs" (brews,
    # necklaces).  0.9 = keep everything at least 90% as big as the biggest.
    "equal_region_tolerance": 0.9,
}


# ---------------------------------------------------------------------------
# 7. Mouse behaviour (human-like movement)
# ---------------------------------------------------------------------------
# Straight port of the old bezier mover; the random draws happen in the same
# order and with the same ranges, so the movement "fingerprint" is unchanged.

MOUSE = {
    "steps": 51,                          # samples along the curve: i/50 for i in 0..50
    "control_spread_px": 300,             # random offset applied to both control points
    "bezier_intensity": 0.5,              # how far the control points may pull the curve
    "step_duration": Uniform(0.005, 0.02),
    "final_duration": Uniform(0.02, 0.04),
    "jitter_px": 4,                       # generic "click near the middle" jitter
    "target_jitter_px": 10,               # the red-target clicks used a bigger jitter
    "region_shrink": 0.2,                 # click box = central 20% of a region
    "extra_click_interval": Gauss(0.2, 0.003),   # gap between multi-clicks
}


# ---------------------------------------------------------------------------
# 8. DELAYS -- the single timing table
# ---------------------------------------------------------------------------
# Naming scheme: "<phase>.<what it waits for>".
# Every entry below existed as a literal sleep in replay.py / redclick.py.

DELAYS: Dict[str, object] = {
    # -- route replay phase (was replay.py) --------------------------------
    "route.start_pause":          Fixed(1.0),        # before anything happens
    "leg.initial_pause":          Fixed(1.0),        # start of every leg
    "leg.tab_key_hold":           Uniform(0.1, 0.5), # '2' (inventory tab) hold time
    "leg.after_tab_key":          Fixed(1.0),
    "leg.after_pouch_click":      Fixed(1.0),
    "leg.after_necklace_click":   Fixed(1.0),
    "leg.after_timeline":         Fixed(1.0),        # after the recorded events ran

    # -- teleport hop between leg 1 and leg 2 ------------------------------
    "hop.before_screenshot":      Uniform(4.5, 5.5),
    "hop.screenshot_hold":        Uniform(0.1, 0.2), # 'insert' = RuneLite screenshot
    "hop.after_screenshot":       Uniform(0.1, 0.2),
    "hop.after_teleport_click":   Uniform(4.5, 5.5),

    # -- arriving at the spot, right before the common case ----------------
    "arrive.key_hold":            Uniform(0.1, 0.2), # 'j'
    "arrive.after_key":           Uniform(0.1, 0.2),
    "arrive.screenshot_hold":     Uniform(0.1, 0.2), # 'insert'

    # -- common case: clicking the red target (was redclick.py) ------------
    "common.after_target_click":  Uniform(0.1, 0.3),
    "common.idle_click.interval": Uniform(0.2, 0.4),
    "common.idle_click.pause":    Uniform(1.1, 1.4), # the occasional "afk" hitch
    "common.after_smite":         Uniform(0.0, 0.5),
    "common.after_loot_full":     Uniform(0.5, 0.8),
    "common.after_dodgy_gone":    Uniform(0.0, 0.5),
    "common.after_veil_gone":     Uniform(0.0, 0.5),

    # -- drinking a brew + turning prayer back on --------------------------
    "prayer.before_brew_click":   Uniform(1.0, 2.0),
    "prayer.after_brew_click":    Uniform(1.0, 2.0),
    "prayer.after_prayer_click":  Uniform(0.5, 1.0),

    # -- following the target when it wandered off -------------------------
    "target.settle":              Fixed(2.0),

    # -- inventory full: empty the pouch and shift-drop a junk item --------
    "invent.before_pouch_click":  Uniform(1.0, 2.0),
    "invent.after_pouch_click":   Uniform(1.0, 2.0),
    "invent.shift_settle":        Uniform(0.1, 0.5),
    "invent.after_drop_click":    Uniform(0.3, 0.8),
    "invent.after_shift_release": Uniform(1.0, 2.0),

    # -- dodgy necklace crumbled: wear a new one ---------------------------
    "dodgy.before_click":         Uniform(1.0, 2.0),
    "dodgy.after_click":          Uniform(1.0, 2.0),

    # -- Shadow Veil faded: recast it --------------------------------------
    "veil.before_key":            Uniform(0.7, 1.2),
    "veil.spell_key_hold":        Uniform(0.05, 0.1),   # '4' = spellbook tab
    "veil.after_spell_key":       Uniform(0.0, 0.3),
    "veil.after_spell_click":     Uniform(1.0, 1.5),
    "veil.tab_key_hold":          Uniform(0.05, 0.1),   # '2' = back to inventory
    "veil.after_tab_key":         Uniform(0.4, 0.8),

    # -- valuable drop ------------------------------------------------------
    "drop.before_screenshot":     Uniform(0.1, 0.5),
    "drop.screenshot_hold":       Uniform(0.1, 0.5),
    "drop.after_screenshot":      Uniform(1.0, 2.0),
    "drop.shift_settle":          Uniform(0.1, 0.5),
    "drop.between_drops":         Uniform(0.1, 0.5),
    "drop.after_drops":           Uniform(0.3, 0.8),
    # NEW: the player (and the camera, which lags behind it) needs ~3 s to come
    # to a full stop after a walk-there click before the next OCR scan is sane.
    "drop.pickup_settle":         Fixed(3.0),
    "drop.rescan_pause":          Uniform(0.2, 0.4),    # NEW: breath before re-scanning
    "drop.before_final_screenshot": Uniform(1.0, 2.0),
    "drop.final_screenshot_hold": Uniform(0.1, 0.5),
    "drop.after_final_screenshot": Uniform(0.1, 0.5),
    "drop.before_restart":        Uniform(5.0, 10.0),

    # -- chat watchdog (NEW) ------------------------------------------------
    "chat.key_hold":              Uniform(0.05, 0.12),  # '`' hold time
    "chat.after_toggle":          Uniform(0.8, 1.2),    # let the chat animate away
    "chat.after_all_click":       Uniform(0.8, 1.2),

    # -- Discord driven actions --------------------------------------------
    "discord.before_restart":     Fixed(1.0),   # legacy sleep(1) before respawning
    "discord.before_brew_reply":  Fixed(1.0),   # legacy sleep(1) before answering
    "discord.screenshot_hold":    Uniform(0.1, 0.5),  # !screenshot -> 'insert'
}


# ---------------------------------------------------------------------------
# 9. SEQUENCES -- fixed key/mouse orders of the replay phase
# ---------------------------------------------------------------------------
# Read top to bottom: this *is* the order the bot does things in.  Reorder the
# lines to reorder the actions; swap a delay name to retime a step.

SEQUENCES: Dict[str, List[object]] = {
    # Runs before every recorded leg: open the inventory tab, empty the coin
    # pouch, put a dodgy necklace on.
    "leg_preamble": [
        Wait("leg.initial_pause"),
        TapKey("2", hold="leg.tab_key_hold", after="leg.after_tab_key",
               note="inventory tab"),
        ClickLargestSolid("cyan", "coin pouch"),
        Wait("leg.after_pouch_click"),
        ClickLargestSolid("white", "dodgy necklace"),
        Wait("leg.after_necklace_click"),
    ],

    # Runs after the recorded events of a leg finished.
    "leg_outro": [
        Wait("leg.after_timeline"),
    ],

    # route1 only: screenshot, then click the black-marked teleport tile.
    "teleport_hop": [
        Log("clicking prif in 5"),
        Wait("hop.before_screenshot"),
        TapKey("insert", hold="hop.screenshot_hold", after="hop.after_screenshot",
               note="RuneLite screenshot"),
        ClickLargestSolid("black", "teleport tile"),
        Log("clicked prif"),
        Wait("hop.after_teleport_click"),
    ],

    # Last thing before the common case starts.
    "arrive_at_spot": [
        TapKey("j", hold="arrive.key_hold", after="arrive.after_key",
               note="toggle run/spec, route specific"),
        TapKey("insert", hold="arrive.screenshot_hold", note="RuneLite screenshot"),
    ],
}


# ---------------------------------------------------------------------------
# 10. Routes
# ---------------------------------------------------------------------------
# `--route <key>` on the command line picks one of these.  Add a new route by
# dropping the recorded .json into routes/ and adding an entry here.
#
# File naming: <route>_leg<N>.json, replayed in list order.  (The old names
# were route1_2.json for the *first* leg and route1.json for the second one,
# which is why nobody could remember which ran first.)

ROUTES: Dict[str, RouteProfile] = {
    "route1": RouteProfile(
        description="Two-leg route: bank/gear prep, teleport hop, then travel to the spot.",
        legs=["routes/route1_leg1.json", "routes/route1_leg2.json"],
        preamble="leg_preamble",
        between_legs="teleport_hop",
        after_last_leg="arrive_at_spot",
        drop_keyword="teleport",
        drop_item_name="Enhanced crystal teleport seed",
        expected_drops=2,
    ),
    "route2": RouteProfile(
        description="Single-leg standalone route (no teleport hop in the middle).",
        legs=["routes/route2_leg1.json"],
        preamble="leg_preamble",
        between_legs=None,               # nothing to do between legs, there is only one
        after_last_leg="arrive_at_spot",
        # TODO(route2): set these to whatever this route's valuable drop is.
        #   drop_keyword   -> substring of the Discord broadcast line
        #   drop_item_name -> the ground label the OCR should look for
        drop_keyword="teleport",
        drop_item_name="Enhanced crystal teleport seed",
        expected_drops=2,
    ),
}

DEFAULT_ROUTE = "route1"


# ---------------------------------------------------------------------------
# 11. Discord integration
# ---------------------------------------------------------------------------

DISCORD = {
    # === PUT YOUR CREDENTIALS HERE ===================================
    "token": "BOT_TOKEN_PLACEHOLDER",     # Discord Developer Portal -> Bot -> Token
    "user_id": 000000000000000000,        # your account id, used for the DM ping
    # =================================================================
    # Both can also be supplied through the environment, which is nicer than
    # editing a tracked file:  COLOURBOT_DISCORD_TOKEN / COLOURBOT_DISCORD_USER_ID

    # Channel the bot posts to on its own initiative (crash notices, "session
    # finished", ...).  0 = just reply in whatever channel last saw traffic.
    "channel_id": 0,

    "command_prefix": "!",

    # Only these user ids may run commands.  Empty list = anybody in the
    # channel (that is what the legacy script effectively did).
    "authorized_user_ids": [],

    # !run <shell command> - handy for "tasklist", "git pull", ... from the phone.
    "allow_run_command": True,
    "run_command_timeout": 30,
    "run_command_output_limit": 1800,      # Discord hard-caps messages at 2000

    # Game chat lines the RuneLite Discord relay posts, and what they mean.
    # Change these if you re-word the relay filters.
    "messages": {
        "loot_full": "There is no space for your loot!",
        "smited": "smited!",
        "dodgy_gone": "Your dodgy necklace has crumbled to dust.",
        "veil_gone": "Shadow Veil has faded!",
        "low_hp": "5 hitpoints!",
        "death_substring": "died",
        "brew_query_substring": "hitpoints",
    },

    # Legacy quirk, preserved on purpose: the script watches the last N relayed
    # lines and panics when "loot_full" shows up `loot_spam_threshold` times.
    # With window 6 and threshold 8 it can never actually fire - the original
    # had the same off-by-two.  Set the window to 8 to make it live.
    "recent_window": 6,
    "loot_spam_threshold": 8,
}


# ---------------------------------------------------------------------------
# 11b. Common-case behaviour
# ---------------------------------------------------------------------------

COMMON = {
    # Chance that the idle-clicking loop takes a longer "human" break before a
    # click (legacy: `if random.random() <= 0.05`).
    "idle_click_pause_chance": 0.05,
}


# ---------------------------------------------------------------------------
# 12. Chat-window watchdog (NEW)
# ---------------------------------------------------------------------------
# Pressing 'insert' (RuneLite screenshot) also pops the game chat open a few
# seconds later.  The chat's translucent black gradient + text sits on top of
# the game world and is the number one reason colour detection or the drop OCR
# fails, so we watch for it and close it again.
#
# Coordinates are CANVAS RELATIVE (0,0 = top-left of the rendered game area),
# taken from the reference layout.

CHAT = {
    "enabled": True,
    "check_interval_seconds": 15.0,   # watchdog cadence during the common case
    "run_during_replay": False,       # the replay phase is short; leave it alone

    "toggle_key": "`",                # closes/opens the chat
    "max_toggle_attempts": 2,         # press it at most twice (a 3rd re-opens it)

    # "Press Enter to Chat" strip, drawn right above the All/Game/Private row
    # while the chat is open.  (x, y, w, h) in canvas coordinates.
    "prompt_rect": (2, 600, 230, 26),
    "prompt_text": "press enter to chat",
    "prompt_match_ratio": 0.55,       # fuzzy OCR match threshold
    "prompt_white_threshold": 170,    # chat text is near-white on dark

    # Fallback: clicking the 'All' button does the same as the '`' key.
    "all_button_rect": (6, 626, 56, 22),
}


# ---------------------------------------------------------------------------
# 13. Valuable drop pick-up (NEW)
# ---------------------------------------------------------------------------
# Old behaviour: drop two junk items, then blindly click the magenta box that
# sits on the player's own tile twice.  That fails whenever the player walked
# off the tile the loot landed on.
#
# New behaviour: find the ground-item label that RuneLite's Ground Items plugin
# paints over the pile (highlight colour below), confirm the item name with
# local OCR (Tesseract - no cloud services), click the pile, wait for the
# player *and* the trailing camera to settle, then re-scan and click again
# until the label is gone.

DROP = {
    # Highlight colour RuneLite paints the loot label in.  Measured from
    # valuable_drop.png: (255, 102, 178) with a bit of alpha blending, hence
    # the tolerance.
    "label_color": (255, 102, 178),
    "label_tolerance": 40,

    # Glyphs are grouped into one label with this dilation kernel (w, h).
    "line_kernel": (15, 3),
    "min_label_pixels": 40,           # ignore specks
    "min_label_width": 25,
    "max_label_height": 26,

    # The label is drawn centred on the pile; the sprite itself sits a few
    # pixels lower.  (Measured: label centre y 697, sprite centre y 702.)
    "click_offset_y": 5,
    "click_jitter_px": 3,

    # OCR verification.
    "ocr_enabled": True,
    "ocr_upscale": 3,                 # tesseract likes big glyphs
    "ocr_dilate": True,               # thicken the thin RS font
    "ocr_match_ratio": 0.62,          # difflib ratio against drop_item_name
    # If OCR cannot confirm the name but exactly one label is on screen, still
    # click it (better a wasted click than a lost 6M gp drop).
    "click_unverified_single_label": True,

    # Attempt budget: one click per item + one spare scan.
    "extra_attempts": 1,
    # Number of scans before giving up on the very first label (the loot beam
    # animation and the camera drift can hide it for a moment).
    "initial_scan_retries": 3,
}
