"""
checks.py -- the expectation ledger.
====================================

The emulator is only a test if somebody writes down what the script was
supposed to do.  This is that notebook: every expectation is named, waited for
with a deadline, and recorded as passed or failed with how long it took.

Two flavours, both used by scenario.py:

    ledger.wait_for("prayer turned back on", lambda: ..., timeout=40)
        polls the emulator's own observations until the bot has done the thing,
        which is the honest way to assert on an asynchronous, human-timed flow.

    ledger.assert_true("run finished cleanly", code == 0)
        a straight assertion for things that are already known.

Nothing here throws: a failed expectation is recorded and the scenario keeps
going, because a run that continues after one broken step tells you far more
than a run that stops at the first one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

PENDING, PASSED, FAILED, SKIPPED = "pending", "passed", "failed", "skipped"


@dataclass
class Expectation:
    name: str
    status: str = PENDING
    detail: str = ""
    seconds: float = 0.0
    at: float = field(default_factory=time.monotonic)

    @property
    def ok(self) -> bool:
        return self.status in (PASSED, SKIPPED)

    def line(self) -> str:
        mark = {PASSED: "PASS", FAILED: "FAIL", SKIPPED: "SKIP",
                PENDING: "...."}[self.status]
        text = f"  [{mark}] {self.name}"
        if self.status == PASSED and self.seconds:
            text += f"  ({self.seconds:.1f}s)"
        if self.detail:
            text += f"\n         {self.detail}"
        return text


class Ledger:
    """Ordered list of expectations, safe to use from the scenario thread."""

    def __init__(self, log: Optional[Callable[[str], None]] = None):
        self.lock = threading.RLock()
        self.items: List[Expectation] = []
        self.log = log or (lambda _message: None)

    # -- recording ---------------------------------------------------------
    def _add(self, item: Expectation) -> Expectation:
        with self.lock:
            self.items.append(item)
        self.log(item.line())
        return item

    def assert_true(self, name: str, value: bool, detail: str = "") -> bool:
        """`detail` explains the failure, so it is only kept when it happens."""
        self._add(Expectation(name, PASSED if value else FAILED,
                              "" if value else detail))
        return bool(value)

    def skip(self, name: str, why: str) -> None:
        self._add(Expectation(name, SKIPPED, why))

    def note(self, message: str) -> None:
        """Not an expectation, just something worth having in the transcript."""
        self.log(f"  ---- {message}")

    # -- waiting -----------------------------------------------------------
    def wait_for(self, name: str, predicate: Callable[[], bool],
                 timeout: float = 30.0, poll: float = 0.2,
                 detail: Callable[[], str] = None,
                 stop: Optional[threading.Event] = None) -> bool:
        """Poll `predicate` until it is true or `timeout` runs out."""
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    self._add(Expectation(name, PASSED,
                                          detail() if detail else "",
                                          time.monotonic() - started))
                    return True
            except Exception as exc:                       # a broken predicate
                self._add(Expectation(name, FAILED, f"predicate raised: {exc}",
                                      time.monotonic() - started))
                return False
            if stop is not None and stop.is_set():
                self._add(Expectation(name, FAILED, "run aborted while waiting",
                                      time.monotonic() - started))
                return False
            time.sleep(poll)
        self._add(Expectation(name, FAILED,
                              (detail() if detail else "") or
                              f"still not true after {timeout:.0f}s",
                              time.monotonic() - started))
        return False

    # -- reporting ---------------------------------------------------------
    @property
    def passed(self) -> int:
        with self.lock:
            return sum(1 for item in self.items if item.status == PASSED)

    @property
    def failed(self) -> int:
        with self.lock:
            return sum(1 for item in self.items if item.status == FAILED)

    @property
    def failures(self) -> List[Expectation]:
        with self.lock:
            return [item for item in self.items if item.status == FAILED]

    def report(self) -> str:
        with self.lock:
            items = list(self.items)
        lines = [item.line() for item in items]
        lines.append("")
        lines.append(f"  {self.passed} passed, {self.failed} failed, "
                     f"{len(items)} expectations in total")
        return "\n".join(lines)
