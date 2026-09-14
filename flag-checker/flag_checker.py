#!/usr/bin/env python3
"""
flag_checker.py -- crash-safe low-level input flag checker (Windows)
=====================================================================

Watches EVERY low-level mouse and keyboard event on the machine in real time
and prints an alert whenever an event carries one of the injected flags
(`LLMHF_INJECTED`, `LLMHF_LOWER_IL_INJECTED`, `LLKHF_INJECTED`).

Run it in its own terminal BEFORE you start a bot / automation script:

    python flag_checker.py

It stays up (Ctrl+C to quit).  It installs exactly the same hooks a game
client's anti-cheat does, so whatever it sees the game client could see too.

CRASH-SAFETY / WHY THIS CANNOT FREEZE YOUR INPUT
------------------------------------------------
This monitor does NOT touch user32 at all.  All hooking happens inside a
separate subprocess (`hook_worker.py`):

  * If that worker ever crashes (a bad pointer, an access violation, any
    bug), the OS automatically removes its low-level hooks when the process
    dies.  Input can never be left captured, so you never have to reboot.
  * This monitor detects the worker's exit, prints a note, and starts a fresh
    one immediately, so coverage is continuous.
  * Inside the worker, callbacks ALWAYS call CallNextHookEx via `finally`, so
    even a recoverable error never blocks a single input event.

How to use
----------
1. Open a terminal and run `python flag_checker.py`.
2. Open a SECOND terminal and run the automation script under test.
3. Any event the bot sends through a *user-mode injection API*
   (`SendInput`, `mouse_event`, `keybd_event`, `SetCursorPos`) shows up as

       [INJECTED ] keyboard key-down insert  LLKHF_INJECTED

   and is therefore detectable by a game client.

   The "clean" paths (Interception kernel driver, real USB HID board) produce
   the same events but with flags == 0, so with `--verbose` they print:

       .          keyboard key-down insert (clean)

4. Ctrl+C in the checker terminal to stop.

Note on Windows-integrity: low-level hooks receive injected events only if the
hooking process runs at the SAME or HIGHER integrity level than the injector.
Run this elevated (as Administrator) if you expect the bot to run at a lower
integrity level.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "hook_worker.py")

# Constants used only for reporting flag bit names.
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002
LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002


def _flag_names(kind: str, flags: int) -> str:
    if kind == "keyboard":
        names = []
        if flags & LLKHF_INJECTED:
            names.append("LLKHF_INJECTED")
        if flags & LLKHF_LOWER_IL_INJECTED:
            names.append("LLKHF_LOWER_IL_INJECTED")
        return " | ".join(names)
    names = []
    if flags & LLMHF_INJECTED:
        names.append("LLMHF_INJECTED")
    if flags & LLMHF_LOWER_IL_INJECTED:
        names.append("LLMHF_LOWER_IL_INJECTED")
    return " | ".join(names)


def banner() -> None:
    print("=" * 78)
    print("CRASH-SAFE INPUT FLAG CHECKER  (WH_MOUSE_LL + WH_KEYBOARD_LL)")
    print("=" * 78)
    print("Watching every low-level mouse/keyboard event for injected flags.")
    print("  LLMHF_INJECTED         mouse  (bit 0)")
    print("  LLMHF_LOWER_IL_INJECTED mouse (bit 1)")
    print("  LLKHF_INJECTED         keyboard (bit 4)")
    print("  LLKHF_LOWER_IL_INJECTED keyboard (bit 1)")
    print("-" * 78)
    print("Clean events print nothing by default (add --verbose for dots).")
    print("A line starting with [INJECTED] means the bot (or any other")
    print("process) used an injection API that a game client can detect.")
    print("The hooking worker can crash harmlessly and auto-restarts;")
    print("your input can never be captured.  Ctrl+C to stop.")
    print("=" * 78)


class FlagChecker:
    def __init__(self, verbose_dots: bool = False):
        self.verbose = verbose_dots
        self.counts = {"mouse": 0, "keyboard": 0}
        self.flagged = {"mouse": 0, "keyboard": 0}
        self.restarts = 0
        self._proc = None          # subprocess.Popen for the worker
        self._stop = False

    # ---------------- worker lifecycle -------------------------------------
    def _start_worker(self) -> None:
        """Launch a fresh hook_worker.py process with an unbuffered stdout."""
        self._proc = subprocess.Popen(
            [sys.executable, "-u", WORKER],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(os.environ, FLAG_CHECKER_PARENT_PID=str(os.getpid())),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._worker_pid = self._proc.pid
        if self.restarts == 0:
            print(f"[setup] hook worker started (pid {self._proc.pid})")
        else:
            print(f"[setup] hook worker restarted (pid {self._proc.pid}, "
                  f"count {self.restarts})")

    # ---------------- main loop --------------------------------------------
    def run(self) -> int:
        self._start_worker()

        try:
            while not self._stop:
                # Drain a line of worker output (blocks until available).
                line = self._proc.stdout.readline()
                if line == "":
                    # worker died / closed stdout
                    self._handle_worker_exit()
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._on_event(obj)
        finally:
            self._shutdown_worker()

        return 0 if self._any_flagged() == 0 else 0

    def _handle_worker_exit(self) -> None:
        """Worker ended (crash or clean).  OS removed its hooks automatically;
        restart it so coverage continues."""
        if self._stop:
            return
        code = self._proc.poll()
        try:
            err = self._proc.stderr.read() if self._proc.stderr else ""
        except Exception:
            err = ""
        err = (err or "").strip()
        self._proc.wait(timeout=5)
        self.restarts += 1
        print(f"[notice] hook worker exited (rc={code})"
              + (f": {err.splitlines()[0]}" if err else "")
              + " -- input was released automatically, restarting worker ...")
        self._start_worker()

    def _shutdown_worker(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    # ---------------- reporting --------------------------------------------
    def _on_event(self, obj: dict) -> None:
        kind = obj.get("kind")
        if kind == "ready":
            return
        if kind not in ("mouse", "keyboard"):
            return
        flags = int(obj.get("flags", 0))
        detail = obj.get("detail", "")
        self.counts[kind] += 1
        names = _flag_names(kind, flags)
        if names:
            self.flagged[kind] += 1
            print(f"[INJECTED ] {kind:9s} {detail}   {names}")
        elif self.verbose:
            print(f".          {kind:9s} {detail} (clean)")

    def _any_flagged(self) -> int:
        return self.flagged["mouse"] + self.flagged["keyboard"]

    def summary(self) -> None:
        print("Summary:")
        print(f"  mouse events checked    : {self.counts['mouse']}")
        print(f"  keyboard events checked : {self.counts['keyboard']}")
        print(f"  flagged mouse events    : {self.flagged['mouse']}")
        print(f"  flagged keyboard events : {self.flagged['keyboard']}")
        print(f"  worker restarts         : {self.restarts}")


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    if len(sys.argv) > 1 and not verbose:
        print("usage: python flag_checker.py [--verbose]")
        return 2

    banner()
    checker = FlagChecker(verbose_dots=verbose)

    def _on_ctrl_c(signum, frame):
        checker._stop = True

    try:
        signal.signal(signal.SIGINT, _on_ctrl_c)
    except Exception:
        pass

    try:
        checker.run()
    finally:
        checker.summary()

    if checker._any_flagged():
        print("RESULT: DETECTABLE INPUT SEEN (injected flags were present)")
        return 1
    print("RESULT: no injected flags seen (clean)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
