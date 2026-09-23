"""Press one key in the console window a session is running in.

This is the most dangerous module in the project: it sends a synthetic
keystroke to a window on the user's machine. Aimed wrong, it types into
whatever the user is actually working in. Every decision here exists to make
that impossible, so read the three of them before changing anything.

**1. The target is found by console attachment, never by focus.** A pid
either owns a console or it does not. `AttachConsole(pid)` succeeds only
for the former and hands back exactly that console's window — there is no
lookup to go stale and no "front window" to guess. A session hosted by
Windows Terminal (ConPTY, not a console) fails the attach: those tabs
cannot be addressed or even enumerated per-pid, so they come back
`not_found` and NOTHING is pressed. There is deliberately no "send it to
the focused tab" fallback, for the same reason dialog.py has no "send it
to the front window" fallback: a guess wearing a fact's clothes, typed
into an unrelated window every single time.

**2. The vocabulary is closed.** Return, Escape and a single digit 1-9.
That is the whole set, and it is a safety boundary rather than a
convenience: anything else — free text, an empty string, a
multi-character string, a shell fragment — is refused before anything is
touched, let alone pressed. This module never types text.

**3. Nothing here raises.** Every path returns an outcome string, so the
caller can always say something useful and always has something to audit.

Console windows only (conhost powershell.exe/cmd.exe): one window per
session, so window identity IS session identity for as long as the attach
holds. `ctypes` only — no pywin32, no pywinauto. All Win32 interaction
lives in ConsoleBackend below, which tests replace wholesale: no test in
this project may press a real key.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import time

log = logging.getLogger("jarvis.dialog")

# --- outcomes ---------------------------------------------------------------
SENT = "sent"
NO_TTY = "no_tty"           # the pid is dead, or has no console to own
NOT_FOUND = "not_found"     # no console could be attached (a WT tab, a GUI
                            # app, a pid that died mid-lookup) — press nothing
NOT_PERMITTED = "not_permitted"   # elevation gap, UIPI refusal, or a
                                  # SetForegroundWindow the OS would not grant
FAILED = "failed"           # the press did not happen for any other reason
BAD_KEY = "bad_key"         # defensive: a key outside the closed vocabulary

# How long each phase may take. The Win32 calls themselves do not block,
# so these bound scheduling stalls, not syscalls — belt and suspenders
# around a voice turn with a person waiting on it, mirroring dialog.py.
LOOKUP_TIMEOUT = 10.0
SEND_TIMEOUT = 20.0

# Settle delays around the focus change, mirroring dialog.py's 0.2 s /
# 0.1 s: the foreground switch must land before the keystroke, and the
# restore must land after it.
_FOCUS_SETTLE_SEC = 0.2
_AFTER_PRESS_SEC = 0.1

# Virtual-key codes. Digits use their ASCII codes (VK_1 == 0x31), exactly
# as the OS defines them; Return and Escape are unambiguous codes rather
# than characters, so they cannot be reinterpreted as text.
_VK_RETURN = 0x0D
_VK_ESCAPE = 0x1B

_ALIASES = {
    "enter": "return",
    "return": "return",
    "yes": "return",       # "yes" answers a permission prompt with Return
    "y": "return",
    "escape": "escape",
    "esc": "escape",
    "cancel": "escape",
    "no": "escape",
    "n": "escape",
}


def normalize_key(key) -> str | None:
    """The closed vocabulary, or None. None means REFUSE — never interpret.

    Returns "return", "escape", or a single digit "1".."9". Anything else,
    including free text that merely starts with an accepted word, is None.
    Identical to dialog.normalize_key on purpose: the MCP tool description
    promises one vocabulary, and both backends keep it.
    """
    if not isinstance(key, str):
        return None
    k = key.strip().lower()
    if k in _ALIASES:
        return _ALIASES[k]
    if len(k) == 1 and k in "123456789":
        return k
    return None


def spoken_key(normalized: str) -> str:
    """How the read-back names the key. Must match what actually gets sent."""
    return {"return": "Return", "escape": "Escape"}.get(normalized, normalized)


def _parse_pid(pid) -> int | None:
    """A positive integer pid, or None — never a process group, never zero."""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def tty_for_pid_async(pid) -> str | None:
    """Always None on this backend: sessions carry no pids and consoles
    have no tty names, so there is no terminal to resolve one to.

    Kept because the server's dialog-staging path asks it per pid before
    concluding "nothing to press" — with no pids to ask about, that path
    reaches the honest refusal on its own, and deleting the name would
    break the call site for no behavior change.
    """
    return None


# --- the one Win32 boundary ------------------------------------------------

class ConsoleBackend:
    """Every Win32 call this module makes, in one replaceable object.

    Production uses ctypes directly (standard library — no new
    dependency); tests swap the module-global `_backend` for a fake, so
    no test ever touches the real input queue. Method contracts:

    * `pid_state(pid)` — "live", "dead", or "denied". Advisory only: the
      attach below is authoritative, and a pid that dies between the two
      reads as `not_found`, never as a press.
    * `press_in_console(pid, normalized)` — attach, focus, verify, press,
      restore, detach, atomically from the caller's point of view — and
      return "sent", "not_found", "not_permitted" or "failed". NOTHING is
      pressed on any path but a verified one.
    """

    def pid_state(self, pid: int) -> str:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return "live"
        err = ctypes.get_last_error()
        if err == 5:  # ERROR_ACCESS_DENIED: alive, above our integrity level
            return "denied"
        return "dead"  # ERROR_INVALID_PARAMETER and the rest: no such pid

    def press_in_console(self, pid: int, normalized: str) -> str:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        ATTACH_PARENT_PROCESS = 0xFFFFFFFF
        own = user32.GetConsoleWindow()
        # The server normally runs attached to its own console; attaching
        # to another requires detaching first. Without a console there is
        # nothing to detach, and the attach below either works or refuses.
        detached = False
        if own:
            kernel32.FreeConsole()
            detached = True
        try:
            if not kernel32.AttachConsole(pid):
                err = ctypes.get_last_error()
                if err == 5:
                    return "not_permitted"
                return "not_found"
            try:
                hwnd = user32.GetConsoleWindow()
                if not hwnd:
                    return "not_found"
                prior = user32.GetForegroundWindow()
                if not user32.SetForegroundWindow(hwnd):
                    return "not_permitted"
                time.sleep(_FOCUS_SETTLE_SEC)
                if user32.GetForegroundWindow() != hwnd:
                    # Focus went elsewhere in the settle window. Pressing
                    # now would type into the wrong window: refuse, loudly.
                    return "failed"
                if not self._send_key(user32, normalized):
                    # The keystroke left nothing in the input queue. With
                    # focus verified a moment ago, that is UIPI refusing an
                    # elevated window, not a missed key — say so rather
                    # than reporting a failure to retry.
                    return "not_permitted"
                time.sleep(_AFTER_PRESS_SEC)
                if prior and prior != hwnd:
                    try:
                        user32.SetForegroundWindow(prior)
                    except Exception:
                        pass
                return "sent"
            finally:
                kernel32.FreeConsole()
        finally:
            if detached:
                try:
                    kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
                except Exception:
                    pass

    @staticmethod
    def _send_key(user32, normalized: str) -> bool:
        """Inject Return / Escape / a digit. True only if the whole
        keystroke (down AND up) entered the input queue."""
        if normalized == "return":
            vk = _VK_RETURN
        elif normalized == "escape":
            vk = _VK_ESCAPE
        else:
            vk = ord(normalized)

        class _KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort),
                        ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class _INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong),
                        ("ki", _KEYBDINPUT)]

        KEYEVENTF_KEYUP = 0x0002
        extra = ctypes.c_ulong(0)
        down = _INPUT(0, _KEYBDINPUT(vk, 0, 0, 0, ctypes.pointer(extra)))
        up = _INPUT(0, _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0,
                                   ctypes.pointer(extra)))
        events = (_INPUT * 2)(down, up)
        try:
            sent = user32.SendInput(2, events, ctypes.sizeof(_INPUT))
        except Exception:
            return False
        return sent == 2


_backend = ConsoleBackend()


async def answer(pid, key: str) -> str:
    """Press one key in the console that owns `pid`. Never raises.

    Returns `sent`, `no_tty`, `not_found`, `not_permitted`, `failed`, or —
    defensively, for a caller that skipped its own validation — `bad_key`.
    Only `sent` means a keystroke actually left this machine's event queue.
    """
    normalized = normalize_key(key)
    if normalized is None:
        # Before anything is touched: nothing about the rejected key ever
        # reaches the OS, so there is nothing to escape and nothing to
        # get wrong.
        log.warning(f"refusing a key outside the vocabulary: {key!r}")
        return BAD_KEY
    number = _parse_pid(pid)
    if number is None:
        return NO_TTY
    try:
        try:
            state = await asyncio.wait_for(
                asyncio.to_thread(_backend.pid_state, number),
                timeout=LOOKUP_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning(f"console lookup for pid {number} timed out")
            return FAILED
        if state == "dead":
            return NO_TTY
        if state == "denied":
            return NOT_PERMITTED
        try:
            outcome = await asyncio.wait_for(
                asyncio.to_thread(_backend.press_in_console, number,
                                  normalized),
                timeout=SEND_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning(f"keypress for pid {number} timed out")
            return FAILED
        if outcome not in (SENT, NOT_FOUND, NOT_PERMITTED, FAILED):
            log.warning(f"unexpected keypress outcome: {outcome!r}")
            return FAILED
        return outcome
    except Exception as e:
        log.warning(f"answering a dialog for pid {pid} failed: {e}",
                    exc_info=True)
        return FAILED
