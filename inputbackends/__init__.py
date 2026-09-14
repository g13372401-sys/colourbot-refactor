"""
inputbackends -- pick the transport the bot's mouse and keyboard go through.
============================================================================

    from inputbackends import get_backend
    backend = get_backend()           # honours config.INPUT
    backend.move(x, y, duration=0.03)
    backend.click()

Backends (see each module for the detail):

    interception   kernel filter driver on the mouse/keyboard class stacks.
                   Software only, no extra hardware.  Events enter through the
                   device stack, so LLMHF_INJECTED is NOT set.        [DEFAULT]

    arduino        a real USB HID microcontroller driven over serial.  The
                   events *are* hardware events, so nothing is set - and there
                   is no driver on the machine to find either.

    sendinput      the old mouse/keyboard/pynput path.  Sets LLMHF_INJECTED
                   (and LLMHF_LOWER_IL_INJECTED from a lower integrity level).
                   Fallback and test control only.

Selection (config.INPUT):

    backend: "auto"    try each name in `auto_order` and keep the first that
                       starts.  A backend that needs missing hardware or a
                       missing driver raises BackendUnavailable, which is
                       logged and skipped.
    backend: "<name>"  use exactly that one; if it cannot start, say why and
                       stop - no silent downgrade to a detectable transport.

    allow_flagged_fallback: False stops "auto" from ever landing on
                       `sendinput`.  Set it to False on the live account: it
                       turns "my anti-cheat-safe bot quietly went back to
                       SendInput because I forgot to plug the board in" into a
                       clean, loud failure.

The chosen backend is a process-wide singleton: `core.InputController` is
rebuilt on every session restart, and re-opening the driver context (or
re-enumerating a serial port) on each restart would be both slow and needless.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from .base import (BackendUnavailable, InputBackend, LLMHF_INJECTED,
                   LLMHF_LOWER_IL_INJECTED)
from .arduino import SerialHidBackend
from .interception import InterceptionBackend
from .sendinput import SendInputBackend

LOG = logging.getLogger("colourbot.input")

BACKENDS: Dict[str, type] = {
    InterceptionBackend.name: InterceptionBackend,
    SerialHidBackend.name: SerialHidBackend,
    SendInputBackend.name: SendInputBackend,
}

DEFAULT_ORDER = ("interception", "arduino", "sendinput")

_lock = threading.Lock()
_current: Optional[InputBackend] = None


def _settings() -> dict:
    """config.INPUT, with defaults for an older config.py that lacks it."""
    import config
    return dict(getattr(config, "INPUT", {}) or {})


def build(name: str, settings: Optional[dict] = None) -> InputBackend:
    """Construct (but do not start) one backend by name."""
    try:
        factory = BACKENDS[name]
    except KeyError:
        raise BackendUnavailable(
            f"unknown input backend {name!r}; known: {', '.join(sorted(BACKENDS))}")
    settings = settings if settings is not None else _settings().get(name, {})
    return factory(settings)


def create(name: str = None, settings: Optional[dict] = None) -> InputBackend:
    """Build *and start* a backend, resolving "auto" against the config order.

    Raises BackendUnavailable with the reason of every candidate that failed,
    so the operator sees "driver not installed" and "no board on COM*" instead
    of a bare "no backend".
    """
    cfg = _settings()
    name = name or cfg.get("backend", "auto") or "auto"

    if name != "auto":
        backend = build(name, settings)
        backend.start()
        _announce(backend)
        return backend

    order: List[str] = list(cfg.get("auto_order", DEFAULT_ORDER))
    if not cfg.get("allow_flagged_fallback", True):
        order = [n for n in order if not BACKENDS[n].sets_injected_flags]

    problems = []
    for candidate in order:
        try:
            backend = build(candidate, settings)
            backend.start()
        except BackendUnavailable as exc:
            LOG.info("input backend %r unavailable: %s", candidate, exc)
            problems.append(f"  {candidate}: {exc}")
            continue
        except Exception as exc:                              # pragma: no cover
            LOG.warning("input backend %r failed to start: %s", candidate, exc)
            problems.append(f"  {candidate}: {exc}")
            continue
        _announce(backend)
        return backend

    raise BackendUnavailable("no usable input backend:\n" + "\n".join(problems))


def _announce(backend: InputBackend) -> None:
    """One log line the operator (and the log reader) cannot miss."""
    if backend.sets_injected_flags:
        LOG.warning("input backend: %s  <-- DETECTABLE (LLMHF_INJECTED)",
                    backend.describe())
    else:
        LOG.info("input backend: %s", backend.describe())


def get_backend(name: str = None) -> InputBackend:
    """The process-wide backend, created on first use."""
    global _current
    with _lock:
        if _current is None:
            _current = create(name)
        return _current


def set_backend(backend: Optional[InputBackend]) -> None:
    """Install a ready-made backend (tests, --input-backend, hot swap)."""
    global _current
    with _lock:
        if _current is not None and _current is not backend:
            _current.close()
        _current = backend


def close_backend() -> None:
    set_backend(None)


def describe_all() -> str:
    """A table for `--list-input-backends` and the flag report."""
    lines = ["available input backends:"]
    for key in DEFAULT_ORDER:
        cls = BACKENDS[key]
        flag = "FLAGGED" if cls.sets_injected_flags else "clean  "
        hardware = " (needs hardware)" if cls.needs_hardware else ""
        lines.append(f"  [{flag}] {key:<12} {cls.os_interface}{hardware}")
    return "\n".join(lines)


__all__ = ["BACKENDS", "BackendUnavailable", "InputBackend", "LLMHF_INJECTED",
           "LLMHF_LOWER_IL_INJECTED", "InterceptionBackend", "SerialHidBackend",
           "SendInputBackend", "build", "create", "get_backend", "set_backend",
           "close_backend", "describe_all"]
