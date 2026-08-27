"""
_emu_client -- the one piece of shared plumbing every shim uses.
===============================================================

Lives on the injected PYTHONPATH next to the fake `mouse`, `keyboard`,
`pynput` and `discord` modules.  It exists so those shims stay tiny and
readable: they translate a library call into one `request(op, ...)` and get out
of the way.

The leading underscore is deliberate - the bot never imports it, and it will not
collide with anything on PyPI if this directory somehow ends up on a real
sys.path.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Tuple

# The shims live in <repo>/emulator/shims; `emulator.protocol` is one level up.
# sys.path[0] is whatever the running script's directory is (the repo root for
# main.py, emulator/bin for the fake tesseract), so be explicit about it.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from emulator import protocol                                    # noqa: E402


def active() -> bool:
    """True when this process was launched inside the emulator harness."""
    return bool(os.environ.get(protocol.SOCKET_ENV))


def request(op: str, **fields) -> dict:
    """One request/response round trip.  Returns the reply header."""
    header, _payload = protocol.thread_client().request(dict(fields, op=op))
    return header


def request_payload(op: str, **fields) -> Tuple[dict, bytes]:
    """Same, but the reply carries bytes (screen grabs)."""
    return protocol.thread_client().request(dict(fields, op=op))


def upload(op: str, payload: bytes, **fields) -> dict:
    """Request that *sends* bytes (the OCR helper)."""
    header, _ = protocol.thread_client().request(dict(fields, op=op), payload)
    return header


def note(text: str, kind: str = "shim") -> None:
    """Tell the emulator something happened, for the on-screen event log."""
    try:
        protocol.thread_client().send({"op": "note", "kind": kind, "text": text})
    except Exception:                       # telemetry must never break a run
        pass


def missing_reason() -> Optional[str]:
    """Why the shims cannot work, or None when everything is in place."""
    if not active():
        return f"{protocol.SOCKET_ENV} is not set"
    path = os.environ[protocol.SOCKET_ENV]
    if not os.path.exists(path):
        return f"emulator socket {path!r} does not exist"
    return None
