"""
emulator -- a self contained, graphical fake of the game client + Discord.
=========================================================================

The colour-bot in this repository only ever talks to the outside world through
six narrow interfaces:

    1. `PIL.ImageGrab`      - "what is on the screen"
    2. `mouse` / `keyboard` - vision driven input (human-like moves, key taps)
    3. `pynput`             - playback of the recorded route timelines
    4. the window manager   - "where is the window called RuneLite"
    5. `pytesseract`        - reading the chat prompt and ground item labels
    6. `discord.py`         - the control channel and the relayed game chat

This package implements the *other side* of all six, so the unmodified script
(`python main.py --route route1`) can be run against a fake client that looks,
reacts and misbehaves like the real one:

    emulator/desktop.py         a virtual 1920x1080 desktop: windows, z-order,
                                a mouse cursor, key focus, click/key effects
    emulator/game_client.py     the RuneLite emulator - a real little game with
                                an inventory, prayer, a chat box, ground items
                                and a target NPC, drawn in the exact highlight
                                colours config.COLORS looks for
    emulator/discord_server.py  a Discord emulator: channels, a relay bot, an
                                operator, DMs, and the gateway the script's
                                Discord layer connects to
    emulator/render.py          the drawing primitives everything is painted
                                with - flat, un-antialiased text and fills, so
                                the bot's colour masks match exactly
    emulator/server.py          glues them together, answers the shims over a
                                unix socket (see protocol.py) and owns the OCR
                                stand-in that the fake tesseract binary calls
    emulator/protocol.py        the framing used on that socket
    emulator/shims/             drop-in replacements for the interfaces above,
                                injected into the script's process with
                                PYTHONPATH + sitecustomize (the script itself is
                                never modified and never learns about any of it)
    emulator/bin/               a fake `wmctrl` and a fake `tesseract`, put on
                                PATH for the two code paths that shell out
    emulator/scenario.py        the scripted run: what the operator types, what
                                the game does to the bot, and what is expected
    emulator/checks.py          the expectation ledger the scenario records into
    emulator/viewer.py          the live window the engineer watches, plus the
                                mp4 recording and the per-step snapshots

Nothing in here is imported by the bot itself; see EMULATOR.md for the whole
picture and `test_emulator_flow.py` for the entry point.
"""

__all__ = ["protocol", "render", "desktop", "game_client", "discord_server",
           "server", "scenario", "checks", "viewer"]
