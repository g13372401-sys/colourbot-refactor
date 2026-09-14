#!/usr/bin/env python3
"""
hook_worker.py -- isolated low-level input hook process (Windows)
=================================================================

This process is the ONLY thing that touches user32.  It installs the same
low-level hooks a game client's anti-cheat does:

    SetWindowsHookExW(WH_KEYBOARD_LL, ...)   # id = 13
    SetWindowsHookExW(WH_MOUSE_LL,    ...)   # id = 14

and streams what it observes back to its stdout as one JSON object per line:

    {"kind": "mouse",    "detail": "left-down (10, 20)", "flags": 1}
    {"kind": "keyboard", "detail": "key-down insert vk=0x2d scan=0x52", "flags": 16}

The parent (flag_checker.py) reads this stdout and prints alerts.

WHY IT IS A SEPARATE PROCESS (crash-safety)
-------------------------------------------
Low-level-hook callbacks receive a pointer to a MSLLHOOKSTRUCT /
KBDLLHOOKSTRUCT.  That pointer is always valid in normal operation, but
reading a *bad* pointer through ctypes raises a hard access violation (SEH)
that try/except CANNOT catch -- it kills the interpreter instantly.  If that
happened inside the same process that owns the hooks AND the callback had not
yet called CallNextHookEx, the offending event would be swallowed and, worse,
a crashed process could leave input looking captured.

Isolation solves this structurally:

  * Hooks are owned by THIS short-lived process.  The moment this process
    terminates (for any reason -- clean quit, exception, or a hard AV crash),
    Windows automatically removes its hooks.  Input can NEVER stay captured.
  * The parent watchdog restarts this process on exit, so coverage is
    continuous even across crashes.

Callbacks still guarantee CallNextHookEx in a `finally`, so even a recoverable
error never blocks a single input event.
"""

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

# ---------------------------------------------------------------------------
# Constants (winuser.h)
# ---------------------------------------------------------------------------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_QUIT = 0x0012

LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002

LLKHF_EXTENDED = 0x00000001
LLKHF_LOWER_IL_INJECTED = 0x00000002
LLKHF_INJECTED = 0x00000010
LLKHF_ALTDOWN = 0x00000020
LLKHF_UP = 0x00000080

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

_MOUSE_MSG_NAMES = {
    WM_MOUSEMOVE: "move",
    WM_LBUTTONDOWN: "left-down",
    WM_LBUTTONUP: "left-up",
    WM_RBUTTONDOWN: "right-down",
    WM_RBUTTONUP: "right-up",
    WM_MBUTTONDOWN: "middle-down",
    WM_MBUTTONUP: "middle-up",
    WM_MOUSEWHEEL: "wheel",
    WM_XBUTTONDOWN: "x-down",
    WM_XBUTTONUP: "x-up",
}

_KB_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_VK_NAMES = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x10: "shift",
    0x11: "ctrl", 0x12: "alt", 0x1B: "esc", 0x20: "space", 0x2D: "insert",
    0x2E: "delete", 0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x23: "end", 0x24: "home", 0x21: "page-up", 0x22: "page-down",
}


def _vk_name(vk: int) -> str:
    if 0x30 <= vk <= 0x39:
        return chr(ord("0") + vk - 0x30)
    if 0x41 <= vk <= 0x5A:
        return _KB_LETTERS[vk - 0x41]
    if 0x70 <= vk <= 0x7B:
        return f"f{vk - 0x70 + 1}"
    return _VK_NAMES.get(vk, f"0x{vk:02x}")


_WINERROR_NAMES = {
    5: "ERROR_ACCESS_DENIED",
    87: "ERROR_INVALID_PARAMETER",
    113: "ERROR_HOOK_NOT_INSTALLED",
    126: "ERROR_MOD_NOT_FOUND",
    1428: "ERROR_HOOK_NEEDS_HMOD",
    1429: "ERROR_GLOBAL_ONLY_HOOK",
    1419: "ERROR_INVALID_HOOK_FILTER",
}


def _winerror_name(code: int) -> str:
    return _WINERROR_NAMES.get(code, "")


def _sign64(x: int) -> int:
    """Interpret x's low 64 bits as a signed 64-bit integer.  ctypes callback
    params such as lparam (LPARAM) can arrive as a positive Python int whose
    value exceeds 2**63; CallNextHookEx's c_ssize_t LPARAM then raises
    OverflowError ("int too long to convert").  This restores the C-level
    signed value so the event passes through unchanged."""
    x &= (1 << 64) - 1
    if x >= (1 << 63):
        x -= (1 << 64)
    return x


# ---------------------------------------------------------------------------
# Canonical callback + struct types
# ---------------------------------------------------------------------------
class _W:
    LRESULT = ctypes.c_ssize_t
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t
    HHOOK = ctypes.c_void_p
    HINSTANCE = ctypes.c_void_p
    HANDLE = ctypes.c_void_p
    DWORD = ctypes.c_ulong


HOOKPROC = ctypes.WINFUNCTYPE(_W.LRESULT, ctypes.c_int, _W.WPARAM, _W.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


LPMSLLHOOKSTRUCT = ctypes.POINTER(MSLLHOOKSTRUCT)
LPKBDLLHOOKSTRUCT = ctypes.POINTER(KBDLLHOOKSTRUCT)


# ---------------------------------------------------------------------------
# Hook engine
# ---------------------------------------------------------------------------
def _out(payload: dict) -> None:
    """Write one JSON observation to stdout and flush immediately (no buffering
    latency for the parent)."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


class Worker:
    def __init__(self, parent_pid: int = 0):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._declare_prototypes()
        self._mouse_hook = None
        self._key_hook = None
        self._mouse_cb = None
        self._key_cb = None
        self.dbg_errors = 0
        self._parent_pid = int(parent_pid)

    # IMPORTANT: ctypes.WinDLL() is NOT cached per name, so the prototypes MUST
    # be applied to the very same object this instance stores (self.user32 /
    # self.kernel32).  A freshly-created WinDLL has NO argtypes, and an untyped
    # ctypes call converts integers as 32-bit -- a pointer-sized lparam then
    # raises "OverflowError: int too long to convert", which is exactly the
    # crash we are guarding against.  Do not create a second WinDLL here.
    def _declare_prototypes(self) -> None:
        user32 = self.user32
        kernel32 = self.kernel32

        kernel32.GetCurrentThreadId.restype = _W.DWORD
        kernel32.GetCurrentProcessId.restype = _W.DWORD

        # HHOOK SetWindowsHookExW(int idHook, HOOKPROC lpfn, HINSTANCE hMod,
        #                         DWORD dwThreadId)
        user32.SetWindowsHookExW.restype = _W.HHOOK
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, _W.HINSTANCE, _W.DWORD]
        # BOOL UnhookWindowsHookEx(HHOOK hhk)
        user32.UnhookWindowsHookEx.restype = ctypes.c_int
        user32.UnhookWindowsHookEx.argtypes = [_W.HHOOK]
        # LRESULT CallNextHookEx(HHOOK hhk, int nCode, WPARAM wParam, LPARAM lParam)
        user32.CallNextHookEx.restype = _W.LRESULT
        user32.CallNextHookEx.argtypes = [
            _W.HHOOK, ctypes.c_int, _W.WPARAM, _W.LPARAM]
        # BOOL GetMessageW(LPMSG, HWND, UINT, UINT)
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_uint]
        user32.TranslateMessage.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_void_p
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

        # HANDLE OpenProcess(DWORD dwDesiredAccess, BOOL bInheritHandle,
        #                    DWORD dwProcessId)
        kernel32.OpenProcess.restype = _W.HANDLE
        kernel32.OpenProcess.argtypes = [_W.DWORD, ctypes.c_int, _W.DWORD]
        # BOOL GetExitCodeProcess(HANDLE hProcess, LPDWORD lpExitCode)
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [
            _W.HANDLE, ctypes.POINTER(_W.DWORD)]
        # BOOL CloseHandle(HANDLE hObject)
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [_W.HANDLE]
        # BOOL PostThreadMessageW(DWORD idThread, UINT Msg, WPARAM wParam,
        #                         LPARAM lParam)
        user32.PostThreadMessageW.restype = ctypes.c_int
        user32.PostThreadMessageW.argtypes = [
            _W.DWORD, ctypes.c_uint, _W.WPARAM, _W.LPARAM]

    # ---------------- callbacks (on this process's pump thread) ------------
    # SAFETY: CallNextHookEx is ALWAYS reached via finally.  Even if the field
    # read below faults (see module docstring), this process's death de-registers
    # the hooks at the OS level on exit, so no event is lost and input is never
    # left captured.  Never "optimise" the finally away.
    def _on_mouse(self, code, wparam, lparam):
        try:
            if code >= 0 and lparam:
                data = ctypes.cast(lparam, LPMSLLHOOKSTRUCT).contents
                flags = int(data.flags)
                msg = _MOUSE_MSG_NAMES.get(int(wparam), f"msg {int(wparam):#06x}")
                xy = (int(data.pt.x), int(data.pt.y))
                _out({"kind": "mouse", "detail": f"{msg} {xy}", "flags": flags})
        except Exception:
            # Observation must never break the input pipeline; continue to the
            # mandatory CallNextHookEx below (this is why `except` does not
            # re-raise -- the event still passes through unchanged).
            self.dbg_errors += 1
        # ALWAYS forward the event.  _sign64 restores the C-level signed LPARAM
        # so pointer-sized values pass through without an overflow crash.
        return self.user32.CallNextHookEx(
            None, code, wparam, _sign64(lparam))

    def _on_key(self, code, wparam, lparam):
        try:
            if code >= 0 and lparam:
                data = ctypes.cast(lparam, LPKBDLLHOOKSTRUCT).contents
                flags = int(data.flags)
                vk = int(data.vkCode)
                scan = int(data.scanCode)
                prefix = "key-up " if flags & LLKHF_UP else "key-down "
                _out({"kind": "keyboard",
                      "detail": f"{prefix}{_vk_name(vk)} vk=0x{vk:02x} "
                                f"scan=0x{scan:02x}",
                      "flags": flags})
        except Exception:
            self.dbg_errors += 1
        # ALWAYS forward the event (see _on_mouse).
        return self.user32.CallNextHookEx(
            None, code, wparam, _sign64(lparam))

    # ---------------- parent watchdog --------------------------------------
    # When the monitor (flag_checker.py) dies - closed terminal, crash, kill -
    # a windowless child process is NOT told about it by the console.  Without
    # this, the worker would keep running (and keep its hooks/shared handles)
    # after the terminal is long gone.  A daemon thread polls the monitor's
    # PID and posts WM_QUIT to the pump thread, which exits the normal way so
    # the `finally` unhooks cleanly.
    @staticmethod
    def _parent_alive(kernel32, pid: int) -> bool:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, pid)
        if not handle:
            # 87 = "the pid does not exist" -> the parent is gone.  Anything
            # else (e.g. ACCESS_DENIED) proves nothing, so stay alive.
            return kernel32.GetLastError() != ERROR_INVALID_PARAMETER
        try:
            code = _W.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    def _watch_parent(self, pump_thread_id: int) -> None:
        while True:
            time.sleep(0.5)
            try:
                if not self._parent_alive(self.kernel32, self._parent_pid):
                    # Wakes the pump; GetMessage returns 0 for WM_QUIT, the
                    # loop exits and the `finally` tears the hooks down.
                    self.user32.PostThreadMessageW(pump_thread_id, WM_QUIT,
                                                   0, 0)
                    return
            except Exception:
                return

    # ---------------- the pump loop ----------------------------------------
    def run(self) -> int:
        # Death-watch: if the monitor dies (crash, killed, or the terminal was
        # closed) this process must go with it, or its hooks and file handles
        # linger forever.  The watchdog runs on the same thread that pumps, so
        # GetCurrentThreadId below is the pump thread's id.
        if self._parent_pid:
            pump_thread = self.kernel32.GetCurrentThreadId()
            threading.Thread(target=self._watch_parent,
                             args=(pump_thread,), daemon=True).start()

        # Low-level hooks ignore hMod; pass NULL (avoids 126 on some builds).
        self._mouse_cb = HOOKPROC(self._on_mouse)
        self._key_cb = HOOKPROC(self._on_key)

        self._mouse_hook = self.user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._mouse_cb, None, 0)
        mouse_err = ctypes.get_last_error() if not self._mouse_hook else 0

        self._key_hook = self.user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._key_cb, None, 0)
        key_err = ctypes.get_last_error() if not self._key_hook else 0

        hijack = None
        if not self._mouse_hook:
            hijack = self._mouse_hook
        elif not self._key_hook:
            hijack = self._key_hook
        if hijack is not None:
            name = _winerror_name(mouse_err or key_err)
            sys.stderr.write(
                "HOOK_FAIL mouse_err=%d key_err=%d%s\n"
                % (mouse_err, key_err, f" ({name})" if name else ""))
            sys.stderr.flush()
            return 2

        _out({"kind": "ready", "pid": os.getpid()})

        message = wintypes.MSG()
        try:
            while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
        finally:
            # Tear hooks down on this thread's exit so a quit never leaves them
            # behind for the brief window before the process terminates.
            if self._mouse_hook:
                self.user32.UnhookWindowsHookEx(self._mouse_hook)
            if self._key_hook:
                self.user32.UnhookWindowsHookEx(self._key_hook)
        return 0


def main() -> int:
    parent_pid = int(os.environ.get("FLAG_CHECKER_PARENT_PID", "0") or 0)
    return Worker(parent_pid).run()


if __name__ == "__main__":
    sys.exit(main())
