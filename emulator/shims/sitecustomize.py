"""
sitecustomize -- how the emulator gets inside the script's process.
===================================================================

Python imports `sitecustomize` automatically at interpreter start-up, before it
runs any script.  The harness puts this directory first on PYTHONPATH, so this
file runs before `main.py` does - which is exactly the hook we need to replace
`PIL.ImageGrab.grab` with "read the emulator's framebuffer".

The other four interfaces (mouse, keyboard, pynput, discord) are replaced by
plain modules sitting next to this one: because the shim directory comes first
on sys.path, `import mouse` finds ours instead of the real package.  ImageGrab
cannot be done that way (PIL is a package the bot also needs for real work), so
it is monkeypatched here instead.

Nothing about this is visible to the script: it imports the same module names,
calls the same functions, and gets pixels/positions back.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _chain_original_sitecustomize() -> None:
    """Run the distro's sitecustomize.py, which we are shadowing.

    Debian's version installs the apport hook; harmless either way, but a shim
    that silently disables part of the platform is a bad shim.
    """
    for entry in sys.path:
        if not entry or os.path.abspath(entry) == _HERE:
            continue
        candidate = os.path.join(entry, "sitecustomize.py")
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    code = compile(handle.read(), candidate, "exec")
                exec(code, {"__name__": "sitecustomize", "__file__": candidate})
            except Exception:
                pass
            return


def _patch_image_grab() -> None:
    """`PIL.ImageGrab.grab()` -> a crop of the emulator's virtual desktop."""
    from PIL import Image, ImageGrab

    import _emu_client

    def grab(bbox=None, include_layered_windows=False, all_screens=False,
             xdisplay=None):
        header, payload = _emu_client.request_payload(
            "grab", bbox=list(bbox) if bbox else None)
        width, height = int(header["w"]), int(header["h"])
        return Image.frombytes("RGB", (width, height), payload)

    grab.__doc__ = ImageGrab.grab.__doc__
    ImageGrab.grab = grab
    # Pillow re-exports it here as well on some versions.
    if hasattr(ImageGrab, "grabclipboard"):
        ImageGrab.grabclipboard = lambda: None


def _install() -> None:
    import _emu_client

    reason = _emu_client.missing_reason()
    if reason:
        raise RuntimeError(reason)
    _patch_image_grab()
    sys.stderr.write("[emulator] shims active for %s (pid %d)\n"
                     % (os.path.basename(sys.argv[0] or "?"), os.getpid()))
    sys.stderr.flush()


_chain_original_sitecustomize()

# Only the bot process gets patched.  Helper processes started *by* the bot
# (pytesseract shells out, `!run` runs whatever the operator typed) inherit
# PYTHONPATH and would otherwise open a pointless socket each time.
if os.path.basename(sys.argv[0] or "") == "main.py" and os.environ.get(
        "COLOURBOT_EMULATOR_SOCKET"):
    try:
        _install()
    except Exception as exc:                                # pragma: no cover
        sys.stderr.write(f"[emulator] shim installation failed: {exc}\n")
        raise
