"""JARVIS Action Executor — system actions (Windows).

Execute actions IMMEDIATELY, before generating any LLM response.
Each function returns {"success": bool, "confirmation": str}.

One rule governs every function here: model text NEVER passes through a
shell or a scripting language on its way to the OS. Commands ride
base64-encoded (`-EncodedCommand` has no quoting layer to escape from),
URLs and paths ride as their own argv elements or straight into
`os.startfile`, which takes a plain string. There is no quoting step to
get wrong because there is nothing to quote: `Popen(..., shell=False)`
(the default) plus argv lists, throughout.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("jarvis.actions")

_CREATION_FLAGS = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def _encoded(command: str) -> str:
    """`command` as base64 UTF-16LE for PowerShell `-EncodedCommand`.

    Encoding (not quoting) is what carries model text past every parsing
    layer: PowerShell decodes and runs the exact bytes, so a command
    containing quotes, semicolons or newlines cannot break out of
    anything — there is nothing to break out of.
    """
    return base64.b64encode(command.encode("utf-16-le")).decode("ascii")


def _powershell() -> str | None:
    """A PowerShell host, preferred first, or None when there is none."""
    return shutil.which("pwsh") or shutil.which("powershell")


def _wt() -> str | None:
    """Windows Terminal, or None when it is not installed."""
    return shutil.which("wt")


async def _spawn(argv: list[str], new_console: bool = False) -> bool:
    """Start a process from an argv list, off the event loop. True when the
    spawn itself succeeded (says nothing about what the child then did).

    The single seam tests mock: every launch in this module goes through
    here except `os.startfile`, which has its own wrapper below.
    """
    try:
        await asyncio.to_thread(
            subprocess.Popen, argv, close_fds=True,
            creationflags=_CREATION_FLAGS if new_console else 0)
    except OSError as e:
        log.warning(f"actions: could not launch {argv[0]!r}: {e}")
        return False
    return True


def _startfile(target: str) -> bool:
    """Open `target` with its default handler. A wrapper (not a direct
    `os.startfile` call) so tests can mock it — and so POSIX imports of
    this module fail at call time, loudly, instead of at import."""
    opener = getattr(os, "startfile", None)
    if opener is None:
        return False
    try:
        opener(target)
    except OSError as e:
        log.warning(f"actions: could not open {target!r}: {e}")
        return False
    return True


async def open_terminal(command: str = "") -> dict:
    """Open Windows Terminal (else a plain PowerShell console) and
    optionally run a command. The tab says JARVIS, the macOS Ocean-theme
    mark's Windows analogue: the user can see whose terminal it is."""
    encoded = _encoded(command) if command else None
    wt = _wt()
    if wt is not None:
        if encoded is not None:
            argv = [wt, "new-tab", "--title", "JARVIS",
                    "powershell.exe", "-NoExit", "-EncodedCommand", encoded]
        else:
            argv = [wt, "new-tab", "--title", "JARVIS"]
        ok = await _spawn(argv)
    else:
        shell = _powershell()
        if shell is None:
            log.error("actions: no terminal host found")
            return {"success": False, "confirmation":
                    "I had trouble opening Terminal, sir."}
        if encoded is not None:
            argv = [shell, "-NoExit", "-EncodedCommand", encoded]
        else:
            argv = [shell]
        ok = await _spawn(argv, new_console=True)
    return {
        "success": ok,
        "confirmation": "Terminal is open, sir." if ok
        else "I had trouble opening Terminal, sir.",
    }


# Explicit browser binaries, looked up in order. The system default (via
# `os.startfile`) is always the last resort, so this still works on a
# machine that has never had Chrome.
_BROWSER_EXES = {
    "chrome": (("chrome", "chrome.exe"), (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    )),
    "firefox": (("firefox", "firefox.exe"), (
        r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
    )),
}

_BROWSER_DISPLAY = {"chrome": "Chrome", "firefox": "Firefox"}


def _browser_exe(browser: str) -> str | None:
    """The executable for `browser`, or None when it is not installed."""
    names, paths = _BROWSER_EXES.get(browser, ((), ()))
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for raw in paths:
        candidate = os.path.expandvars(raw)
        if os.path.isfile(candidate):
            return candidate
    return None


async def open_browser(url: str, browser: str = "chrome") -> dict:
    """Open URL in Chrome or Firefox, else in the default browser.

    The URL is passed as its own argv element (explicit binary) or as a
    plain string to `os.startfile` (default) — never through a shell, so
    a URL ending in a quote or a backslash cannot escape into anything.
    """
    key = browser.lower()
    display = _BROWSER_DISPLAY.get(key, "Chrome")
    exe = _browser_exe(key) if key in _BROWSER_EXES else None
    if exe is not None:
        ok = await _spawn([exe, url])
    else:
        if key in _BROWSER_EXES:
            log.info(f"actions: {display} not installed; "
                     f"using the default browser")
        ok = await asyncio.to_thread(_startfile, url)
    return {
        "success": ok,
        "confirmation": f"Pulled that up in {display}, sir." if ok
        else f"{display} ran into a problem, sir.",
    }


async def open_chrome(url: str) -> dict:
    """Backward-compat alias, mirroring actions.py."""
    return await open_browser(url, "chrome")


async def get_chrome_tab_info() -> dict:
    """The current browser tab's title and URL — unavailable on Windows.

    Reading another application's tab needs UI Automation plumbing this
    port does not yet have. Returns {} like every other failure mode of
    the macOS version, rather than a guess: callers already treat empty
    as "unknown".
    """
    return {}


# --- Opening code where the user actually reads it -----------------------
#
# VS Code first when it is installed, the system default otherwise — the
# same promise actions.py makes. No AppleScript and no shell here either:
# the path rides as its own argv element, so a filename cannot be quoted
# out of anything. Containment and the sensitive-file wall are the
# CALLER's job and have already run by the time this is reached.

_VSCODE_PATHS = (
    r"%ProgramFiles%\Microsoft VS Code\Code.exe",
    r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
    r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
)


def _vscode_command(path: str) -> list[str] | None:
    """The argv that opens `path` in VS Code, or None if it is not installed."""
    binary = shutil.which("code") or shutil.which("code.cmd")
    if binary:
        return [binary, str(path)]
    for raw in _VSCODE_PATHS:
        candidate = os.path.expandvars(raw)
        if os.path.isfile(candidate):
            return [candidate, str(path)]
    return None


async def open_in_editor(path: str) -> dict:
    """Open a file or directory in VS Code, else in the system default."""
    argv = _vscode_command(path)
    editor = "VS Code"
    if argv is None:
        editor = "your editor"
        ok = await asyncio.to_thread(_startfile, str(path))
    else:
        ok = await _spawn(argv)
    return {
        "success": ok,
        "editor": editor,
        "confirmation": f"Opened that in {editor}, sir." if ok
        else f"{editor} wouldn't open that, sir.",
    }


def running_on_windows() -> bool:
    """True on win32. The server keys its backend choice off this (with
    JARVIS_RUNTIME for the agent half), and tests assert both halves."""
    return sys.platform == "win32"
