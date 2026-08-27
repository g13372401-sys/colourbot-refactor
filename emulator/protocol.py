"""
protocol.py -- the wire between the script's process and the emulator.
======================================================================

The bot runs in its own process (it is started exactly as an operator would
start it: `python main.py --route route1`), so the shims that replace
`mouse`, `keyboard`, `pynput`, `PIL.ImageGrab`, `wmctrl` and `discord.py` need a
way to reach the emulator.  That way is a unix domain socket - no TCP, no
network, no ports; it works while the machine is fully offline.

Framing (deliberately boring):

    4 bytes   big endian length of the json header
    N bytes   utf-8 json header, e.g. {"op": "grab", "bbox": [...]}
    M bytes   optional raw payload; M comes from the header key "nbytes"

Everything is request/response except the Discord gateway connection, which the
server also uses to *push* message events at the client (same framing, the
client just keeps reading).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from typing import Optional, Tuple

_HEADER_LEN = struct.Struct(">I")

# Environment variable the runner sets so every shim can find the socket.
SOCKET_ENV = "COLOURBOT_EMULATOR_SOCKET"


# ---------------------------------------------------------------------------
# low level frame io
# ---------------------------------------------------------------------------

def send_frame(sock: socket.socket, header: dict, payload: bytes = b"") -> None:
    """Write one header (+ optional payload) to `sock`."""
    if payload:
        header = dict(header, nbytes=len(payload))
    blob = json.dumps(header).encode("utf-8")
    sock.sendall(_HEADER_LEN.pack(len(blob)) + blob)
    if payload:
        sock.sendall(payload)


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("emulator connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket) -> Tuple[dict, bytes]:
    """Read one header (+ payload) from `sock`.  Raises ConnectionError on EOF."""
    raw_len = _recv_exactly(sock, _HEADER_LEN.size)
    (length,) = _HEADER_LEN.unpack(raw_len)
    header = json.loads(_recv_exactly(sock, length).decode("utf-8"))
    payload = b""
    nbytes = header.get("nbytes")
    if nbytes:
        payload = _recv_exactly(sock, int(nbytes))
    return header, payload


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class EmulatorClient:
    """One connection to the emulator, guarded by a lock.

    The shims are called from many threads at once (the vision threads capture
    while the input threads click), so each thread gets its own client through
    `thread_client()` below - that keeps request/response pairs from
    interleaving without serialising the whole bot behind one socket.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get(SOCKET_ENV)
        if not self.path:
            raise RuntimeError(
                f"{SOCKET_ENV} is not set - the emulator shims were loaded "
                "outside of the emulator test harness")
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None

    # -- connection -------------------------------------------------------
    def connect(self) -> socket.socket:
        if self._sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.path)
            # Screen grabs are ~2 MB, so give the socket a roomy send buffer.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            self._sock = sock
        return self._sock

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    # -- requests ---------------------------------------------------------
    def request(self, header: dict, payload: bytes = b"") -> Tuple[dict, bytes]:
        """Send a request, wait for the reply."""
        with self._lock:
            sock = self.connect()
            try:
                send_frame(sock, header, payload)
                return recv_frame(sock)
            except (ConnectionError, OSError):
                # The emulator went away (test over, or it crashed).  Drop the
                # socket so a later call reconnects instead of looping on a
                # dead file descriptor.
                self._sock = None
                raise

    def send(self, header: dict, payload: bytes = b"") -> None:
        """Fire and forget (used for the notes the shims post for the HUD)."""
        try:
            self.request(dict(header, ack=False))
        except Exception:                       # never let telemetry break a run
            pass


_LOCAL = threading.local()


def thread_client() -> EmulatorClient:
    """The calling thread's private connection to the emulator."""
    client = getattr(_LOCAL, "client", None)
    if client is None:
        client = EmulatorClient()
        _LOCAL.client = client
    return client
