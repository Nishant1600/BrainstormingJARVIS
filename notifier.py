"""JARVIS Notifier -- native notification fallback.

When JARVIS needs the user's attention but no browser tab is connected to
speak through, this posts a native Windows toast instead.

Same three contracts as the macOS version:

* Untrusted text travels as PROCESS DATA, never interpolated into script
  source. The PowerShell script below is fixed text (base64-encoded on the
  way in, so no quoting layer exists to get wrong — and Windows PowerShell
  5.1 refuses trailing arguments alongside `-EncodedCommand` anyway, so
  argv could not carry them even if it were asked to); title, message and
  subtitle travel in three dedicated environment variables, which the
  process-creation block passes as opaque bytes, never parsed as code. A
  value containing `" ; Remove-Item ...` is just a string with those
  characters in it. XML-escaping happens INSIDE the script via
  SecurityElement.Escape, for the same reason: the boundary that shapes
  the XML must be the one that escapes it.
* Truncation caps: a notification is a glance, not an essay.
* Never raises: every failure (wrong platform, no PowerShell, non-zero
  exit, timeout, spawn error) is a logged warning and False.

One honest Windows limitation, stated up front: a toast shown under a
bare executable name (no registered Start-menu shortcut / AppUserModelID)
may be silently dropped by the shell — the same way Notification Center
settings or Focus Assist can drop one on macOS. True therefore means
"handed to Windows", never "seen by the user".
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import sys

log = logging.getLogger("jarvis.notifier")

_TITLE_MAX = 120
_SUBTITLE_MAX = 120
_MESSAGE_MAX = 300

# Cold WinRT type-load measured at ~4.4 s on a loaded machine: 5 s would
# turn every first toast into a timeout. A wedged shell is still bounded,
# just less tightly than the macOS 5 s budget for near-instant osascript.
_TIMEOUT_SECONDS = 15.0

# Fixed script. Title, message and subtitle arrive in
# JARVIS_NOTIFY_TITLE/MESSAGE/SUBTITLE (see notify): environment values
# are process data, never parsed as code. ToastText02 carries a headline
# plus one body line; the subtitle is folded into the headline in
# parentheses when present, because a toast is a glance and a third line
# is not one.
_NOTIFY_PS = """\
$title = $env:JARVIS_NOTIFY_TITLE
$message = $env:JARVIS_NOTIFY_MESSAGE
$subtitle = $env:JARVIS_NOTIFY_SUBTITLE
if ($subtitle) { $title = "$title ($subtitle)" }
$et = [System.Security.SecurityElement]::Escape($title)
$em = [System.Security.SecurityElement]::Escape($message)
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml("<toast><visual><binding template=`"ToastText02`"><text id=`"1`">$et</text><text id=`"2`">$em</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("JARVIS").Show($toast)
"""

# The three variables above, and only those three, carry untrusted text.
# Distinctive names so nothing else in the environment collides with them.
_ENV_TITLE = "JARVIS_NOTIFY_TITLE"
_ENV_MESSAGE = "JARVIS_NOTIFY_MESSAGE"
_ENV_SUBTITLE = "JARVIS_NOTIFY_SUBTITLE"


def _truncate(text: str, limit: int) -> str:
    """Bound text length for a glanceable notification, marking any cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"  # ellipsis


def _encoded_script() -> str:
    """The fixed script as base64 UTF-16LE, for `-EncodedCommand`.

    Encoding (not quoting) is what carries the script past the shell:
    there is no quoting layer, so there is no quoting layer to get wrong
    — and the untrusted texts never enter this string at all.
    """
    return base64.b64encode(
        _NOTIFY_PS.encode("utf-16-le")).decode("ascii")


def available() -> bool:
    """Whether posting a notification is plausible right now.

    Cheap and side-effect free: Windows platform + a PowerShell on PATH.
    A precondition check, not a delivery guarantee — Focus Assist,
    notification settings, or a missing app registration can still
    silently drop the toast even when this returns True.
    """
    return (sys.platform == "win32"
            and (shutil.which("powershell") is not None
                 or shutil.which("pwsh") is not None))


def _shell() -> str:
    """`pwsh` when it exists, else `powershell`. Both speak `-EncodedCommand`
    the same way; preferring pwsh keeps the Windows-11 default first."""
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


async def _run(*args: str, timeout: float,
                 env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run one command, bounded by `timeout`. Never raises.

    The single seam every process in this module is spawned through, so
    tests mock one thing — the pattern preflight.py and dialog.py use.
    Argument lists only: nothing here is ever a shell string. `env`
    replaces the child's environment wholesale when given (notify passes
    one carrying the three notification texts as data).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        return -1, "", f"failed to spawn {args[0] if args else '?'}: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return -1, "", f"{args[0] if args else '?'} timed out after {timeout}s"

    return (proc.returncode if proc.returncode is not None else -1,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"))


async def notify(title: str, message: str, *, subtitle: str = "") -> bool:
    """Post a Windows toast. Returns whether it was handed off successfully.

    A fallback path for when nobody is listening on the voice channel, so
    it must never raise: any failure is logged as a warning and reported
    back as False. See the module docstring for what True does and does
    not promise.
    """
    try:
        if not available():
            log.warning("notifier: notifications unavailable on this platform")
            return False

        safe_title = _truncate(str(title or ""), _TITLE_MAX)
        safe_message = _truncate(str(message or ""), _MESSAGE_MAX)
        safe_subtitle = _truncate(str(subtitle or ""), _SUBTITLE_MAX)

        env = dict(os.environ)
        env[_ENV_TITLE] = safe_title
        env[_ENV_MESSAGE] = safe_message
        env[_ENV_SUBTITLE] = safe_subtitle
        code, _out, err = await _run(
            _shell(), "-NoProfile", "-NonInteractive", "-EncodedCommand",
            _encoded_script(), timeout=_TIMEOUT_SECONDS, env=env)
        if code != 0:
            log.warning(f"notifier: shell exited {code}: {err.strip()}")
            return False
        return True
    except Exception as e:
        # Belt and suspenders: this path must never raise into the caller.
        log.warning(f"notifier: unexpected error posting notification: {e}")
        return False
