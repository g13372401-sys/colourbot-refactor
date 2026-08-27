#!/usr/bin/env python3
"""
test_emulator_flow.py -- the emulator test.  Run this.
======================================================

    python test_emulator_flow.py

It builds a virtual 1920x1080 desktop, draws a game client window on it whose
title is exactly the one `config.GAME_WINDOW["title_contains"]` looks for, draws
a Discord client next to it, and then starts the script the way an operator
would:

    python main.py --route route1

The script is *not* modified and is not aware of any of this.  It finds its
window through `wmctrl`, screenshots it through `PIL.ImageGrab`, moves the mouse
with `mouse`/`pynput`, types with `keyboard`, OCRs with `tesseract` and talks to
Discord with `discord.py` - and every one of those six interfaces is answered by
the emulator over a unix socket.  Nothing goes near a real screen, a real
keyboard or the network.

Every delay in `config.DELAYS` is left alone, so this takes as long as a real
session does (roughly six to eight minutes for two full runs of route1).  That
is the point: `emulator/scenario.py` asserts on what the script *did* and how
long it took to do it, so a timing or configuration regression fails a named
expectation instead of quietly passing.

While it runs you can watch it: a window called "colour-bot emulator" if there
is a display, and always an mp4 plus one PNG per step of the flow in the output
directory printed at start-up.

Exit code 0 means every expectation passed and the script exited cleanly.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

import config                                                    # noqa: E402
from emulator import discord_server as DS                        # noqa: E402
from emulator.checks import Ledger                               # noqa: E402
from emulator.scenario import Scenario                           # noqa: E402
from emulator.server import EmulatorServer                       # noqa: E402
from emulator.viewer import Hud, Viewer                          # noqa: E402

SHIMS = os.path.join(REPO_ROOT, "emulator", "shims")
FAKE_BIN = os.path.join(REPO_ROOT, "emulator", "bin")
DEFAULT_OUT = os.path.join(tempfile.gettempdir(), "colourbot-emulator")

# Two full runs of route1 plus the whole event script; the watchdog is a
# backstop for a hung run, not a schedule.
DEFAULT_TIMEOUT = 20 * 60


# ---------------------------------------------------------------------------
# repo hygiene
# ---------------------------------------------------------------------------

class SideEffects:
    """Puts back the files the script writes into the repository.

    `RuntimeTimer` keeps a cumulative stopwatch in `runtime_total.json` and the
    logger writes `colourbot.log`; an emulator run must not leave either of them
    changed, so both are snapshotted here and restored at the end (after being
    copied into the run's output directory, where they are actually useful).
    """

    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        self.paths = [os.path.join(REPO_ROOT, config.GENERAL["runtime_file"]),
                      os.path.join(REPO_ROOT, config.GENERAL["log_file"])]
        self.saved = {}

    def __enter__(self) -> "SideEffects":
        for path in self.paths:
            self.saved[path] = None
            if os.path.exists(path):
                with open(path, "rb") as handle:
                    self.saved[path] = handle.read()
        return self

    def __exit__(self, *_exc) -> None:
        for path, original in self.saved.items():
            if os.path.exists(path):
                try:
                    shutil.copy2(path, os.path.join(self.out_dir,
                                                    os.path.basename(path)))
                except OSError:
                    pass
            if original is None:
                if os.path.exists(path):
                    os.unlink(path)
            else:
                with open(path, "wb") as handle:
                    handle.write(original)


# ---------------------------------------------------------------------------
# the script under test
# ---------------------------------------------------------------------------

def bot_environment(socket_path: str) -> dict:
    """The environment that makes a normal `python main.py` land in here."""
    env = dict(os.environ)
    # sitecustomize.py (and the fake mouse/keyboard/pynput/discord packages)
    # must come first; the repo root is there so the shims can import
    # `emulator.protocol`.
    env["PYTHONPATH"] = os.pathsep.join(
        [SHIMS, REPO_ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    # ...and the fake `wmctrl` / `tesseract` before any real ones.
    env["PATH"] = os.pathsep.join([FAKE_BIN, env.get("PATH", "")])
    env["COLOURBOT_EMULATOR_SOCKET"] = socket_path
    # config.DISCORD ships a placeholder token; these are the env overrides
    # discord_bot.py already supports.
    env["COLOURBOT_DISCORD_TOKEN"] = DS.BOT_TOKEN
    env["COLOURBOT_DISCORD_USER_ID"] = str(DS.OPERATOR_ID)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def start_bot(env: dict, log_path: str) -> subprocess.Popen:
    """Start the script exactly as the README tells an operator to."""
    handle = open(log_path, "wb")
    process = subprocess.Popen(
        [sys.executable, "main.py", "--route", "route1"],
        cwd=REPO_ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True)
    process._log_handle = handle              # keep it open for the run
    return process


def tail(path: str, lines: int = 25) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError as exc:
        return f"(could not read {path}: {exc})"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the whole automation flow against the emulated game "
                    "client and Discord channel.")
    parser.add_argument("--out", default=os.environ.get("COLOURBOT_EMULATOR_OUT",
                                                        DEFAULT_OUT),
                        help="where the mp4, the snapshots and the logs go")
    parser.add_argument("--fps", type=int, default=15,
                        help="recording/live frame rate of the viewer")
    parser.add_argument("--no-window", action="store_true",
                        help="never open the live window, even with a display")
    parser.add_argument("--no-video", action="store_true",
                        help="skip the mp4 recording (snapshots are still saved)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="hard limit for the whole run, in seconds")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    socket_path = os.path.join(tempfile.gettempdir(),
                               f"colourbot-emulator-{os.getpid()}.sock")
    bot_log = os.path.join(out_dir, "bot-stdout.log")

    print("=" * 78)
    print("colour-bot emulator - full flow test")
    print("=" * 78)
    print(f"  repository   {REPO_ROOT}")
    print(f"  artifacts    {out_dir}")
    print(f"  socket       {socket_path}")
    print(f"  window title {config.GAME_WINDOW['title_contains'][0]!r} "
          f"(what the script hunts for)")
    display = os.environ.get("DISPLAY") or "(none - headless, watch run.mp4)"
    print(f"  display      {display}")
    print("=" * 78, flush=True)

    server = EmulatorServer(socket_path)
    ledger = Ledger(log=lambda line: print(line, flush=True))
    process: Optional[subprocess.Popen] = None
    stop = threading.Event()

    viewer = Viewer(server.desktop, out_dir, fps=args.fps,
                    live=False if args.no_window else None,
                    record=not args.no_video)
    scenario = Scenario(server, ledger, viewer,
                        bot_alive=lambda: process is not None
                        and process.poll() is None,
                        stop=stop)
    server.desktop.hud_renderer = Hud(server, scenario).render

    def on_signal(_signum, _frame):
        print("\n[runner] interrupted - stopping the run", flush=True)
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    started = time.monotonic()
    with SideEffects(out_dir):
        server.start()
        viewer.start()
        try:
            env = bot_environment(socket_path)
            process = start_bot(env, bot_log)
            server.bot_pid = process.pid
            server.desktop.log("run", f"started: python main.py --route route1 "
                                      f"(pid {process.pid})")
            print(f"[runner] python main.py --route route1  -> pid "
                  f"{process.pid}\n", flush=True)

            watchdog = threading.Timer(args.timeout, stop.set)
            watchdog.daemon = True
            watchdog.start()

            scenario.run()
            watchdog.cancel()

            # The scenario ends with !kill; give the process a moment either way.
            deadline = time.monotonic() + 20
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if process.poll() is None:
                ledger.assert_true("the script exited on its own", False,
                                   "still running after !kill - terminating it")
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            code = process.poll()
            ledger.assert_true("the script exited cleanly (exit code 0)",
                               code == 0, f"exit code {code}")
        finally:
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                try:
                    process._log_handle.close()
                except Exception:
                    pass
            viewer.snapshot("final")
            viewer.stop()
            server.stop()

    elapsed = time.monotonic() - started
    desktop, discord = server.desktop, server.discord
    print()
    print("=" * 78)
    print("EXPECTATIONS")
    print("=" * 78)
    print(ledger.report())
    print()
    print("=" * 78)
    print("WHAT THE SCRIPT DID")
    print("=" * 78)
    print(f"  wall clock           {int(elapsed) // 60}m {int(elapsed) % 60:02d}s")
    print(f"  mouse moves          {desktop.moves}")
    print(f"  mouse clicks         {desktop.clicks}")
    print(f"  key presses          {desktop.key_presses}")
    print(f"  screen grabs         {server.grabs}")
    print(f"  discord in / out     {discord.injected} / {discord.sent_by_bot}")
    print(f"  game observations    {len(server.game.observations)}")
    for label, value in server.game.summary():
        print(f"  {label:<20} {value}")
    print()
    print("=" * 78)
    print("ARTIFACTS")
    print("=" * 78)
    print(f"  {out_dir}")
    if viewer.record and os.path.exists(viewer.video_path):
        size = os.path.getsize(viewer.video_path) / 1e6
        print(f"    run.mp4            {viewer.frames} frames, {size:.1f} MB")
    print(f"    {len(viewer.snapshots)} step snapshots (NN-<step>.png)")
    print(f"    bot-stdout.log     {os.path.getsize(bot_log) / 1e3:.0f} kB")
    print()
    if ledger.failures:
        print("last lines of the script's own log:")
        print(tail(bot_log, 25))

    failed = ledger.failed
    print("=" * 78)
    print("RESULT: " + ("PASSED - the automation flow behaved as specified"
                        if not failed else
                        f"FAILED - {failed} expectation(s) did not hold"))
    print("=" * 78, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
