"""JARVIS's eyes on the machine: the window list, and one deliberate picture.

Two capabilities, priced very differently:

1. `list_windows()` — which app is in front and what its windows are
   called. One enumeration, a few kilobytes, no pixels.
2. `capture_screen()` — a PNG of one display, shrunk, which the brain
   SEES as an MCP `image` block.

**This is a camera pointed at the user's life.** A screenshot can hold a
password, a private message, a client's data. So nothing here runs on a
timer, speculatively, or as ambient context — the tools are gated to a
user-origin turn, and this module is called from nowhere else. The
capture lives in a `mkdtemp` directory for as long as it takes to shrink
it and read the bytes — milliseconds — and the directory is removed in a
`finally`, on every path out, success or failure.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import shutil
import struct
import sys
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("jarvis.screen")

# Each of these must finish WELL inside `jarvis_mcp.TIMEOUT_SEC` (20s), and
# the caller puts its own hard deadline on top.
CAPTURE_TIMEOUT_SEC = 8.0
WINDOWS_TIMEOUT_SEC = 5.0

# The longest edge the brain is shown. Images are charged by AREA, so a
# full-size capture is not a neutral thing to put in a budgeted context.
SHOT_MAX_EDGE = 1280

# A PNG bigger than this is not going through the tool channel; say so
# rather than sending something the CLI will choke on.
MAX_SHOT_BYTES = 4_000_000

# The window list is a tool result like any other: bound it here so a cut
# never lands mid-way through the untrusted block the caller wraps it in.
MAX_WINDOWS = 12

# The blank-frame check downsamples to this edge before looking at pixels:
# small enough to be free, large enough that a real screenshot is obviously
# not one flat colour.
BLANK_SAMPLE_EDGE = 32

# Per-channel mean absolute deviation, in 0-255 levels, below which a frame
# is "one colour".
BLANK_MAD = 1.5


class ScreenError(Exception):
    """Something JARVIS could not see. The message is meant to be spoken."""


@dataclass
class Shot:
    png: bytes
    width: int
    height: int


@dataclass
class Window:
    app: str
    title: str
    frontmost: bool

# Fixed capture script. Arguments: PNG path, BMP sample path, max edge,
# sample edge, 1-based display index (0 = primary). Everything it needs
# arrives as argv data; the script text itself is fixed.
_CAPTURE_PS = """\
param([string]$png, [string]$bmp, [int]$maxEdge, [int]$sampleEdge, [int]$display)
try { Add-Type -AssemblyName System.Windows.Forms } catch { exit 3 }
try { Add-Type -AssemblyName System.Drawing } catch { exit 3 }
try {
    $dpi = Add-Type -MemberDefinition '[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -Name "Dpi" -Namespace "Jarvis" -PassThru
    [void]$dpi::SetProcessDPIAware()
} catch { }
$screens = [System.Windows.Forms.Screen]::AllScreens
$s = [System.Windows.Forms.Screen]::PrimaryScreen
if ($display -ge 1 -and $display -le $screens.Count) { $s = $screens[$display - 1] }
$b = $s.Bounds
$full = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($full)
try {
    $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
} finally { $g.Dispose() }
$scale = [Math]::Min(1.0, $maxEdge / [Math]::Max($b.Width, $b.Height))
$w = [Math]::Max(1, [int]($b.Width * $scale))
$h = [Math]::Max(1, [int]($b.Height * $scale))
$small = New-Object System.Drawing.Bitmap($w, $h)
$g2 = [System.Drawing.Graphics]::FromImage($small)
try {
    $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g2.DrawImage($full, 0, 0, $w, $h)
} finally { $g2.Dispose() }
$small.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
$sample = New-Object System.Drawing.Bitmap($sampleEdge, $sampleEdge)
$g3 = [System.Drawing.Graphics]::FromImage($sample)
try {
    $g3.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g3.DrawImage($full, 0, 0, $sampleEdge, $sampleEdge)
} finally { $g3.Dispose() }
$sample.Save($bmp, [System.Drawing.Imaging.ImageFormat]::Bmp)
$full.Dispose()
$small.Dispose()
$sample.Dispose()
Write-Output ("OK {0} {1}" -f $w, $h)
"""


def screen_recording_granted() -> bool | None:
    """None — always, on purpose.

    There is no per-app Screen Recording grant on Windows to probe, so
    there is nothing truthful to return but "unknown". Callers must lean
    on the blank-frame check, which refuses a one-colour capture no
    matter what caused it.
    """
    return None


# ── PNG and BMP, read with nothing but the standard library ────────────────

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(png: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG's IHDR, or None if that is not a PNG.

    Doubles as the answer to "did the capture actually write a picture?":
    a tool can exit 0 having written something that is not one.
    """
    if len(png) < 24 or not png.startswith(_PNG_MAGIC) or png[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", png[16:24])
    if width <= 0 or height <= 0:
        return None
    return width, height


def _bmp_pixels(raw: bytes) -> list[tuple[int, int, int]]:
    """The (b, g, r) triples out of an uncompressed BMP, or [] if unreadable.

    Only has to read what the capture script writes: a 24-bit BMP whose
    pixel offset is at byte 10 and whose bit depth is at byte 28, then
    rows of 3-byte pixels.
    """
    if len(raw) < 32 or raw[:2] != b"BM":
        return []
    offset = struct.unpack_from("<I", raw, 10)[0]
    depth = struct.unpack_from("<H", raw, 28)[0]
    if depth not in (24, 32) or offset >= len(raw):
        return []
    step = depth // 8
    body = raw[offset:]
    return [(body[i], body[i + 1], body[i + 2])
            for i in range(0, len(body) - step + 1, step)]


def _is_blank(pixels: list[tuple[int, int, int]]) -> bool:
    """Is this frame one flat colour?

    Judged PER CHANNEL. A solid dark-green desktop is (44, 62, 24) everywhere:
    spread measured across all the bytes at once calls that busy, because the
    three channels differ from each other. Spread WITHIN each channel is 0,
    which is the truth.

    A little noise around black still counts as blank — an almost-black frame
    is not a screenshot either.
    """
    if not pixels:
        return False                     # nothing to judge: do not accuse
    for channel in range(3):
        values = [p[channel] for p in pixels]
        mean = sum(values) / len(values)
        mad = sum(abs(v - mean) for v in values) / len(values)
        if mad >= BLANK_MAD:
            return False
    return True


# ── the subprocess boundary ──────────────────────────────────────────────

async def _run(*args: str, timeout: float) -> tuple[int, str, str]:
    """Run one command, bounded by `timeout`. Never raises.

    The single seam every process in this module is spawned through, so
    tests mock one thing instead of `asyncio.create_subprocess_exec` per
    call — the pattern `preflight.py` and `dialog.py` already use.
    Argument lists only: nothing here is ever a shell string.
    """
    if sys.platform != "win32":
        return -1, "", "screen capture is only available on Windows"
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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


def _shell() -> str | None:
    """A PowerShell host for the capture script, or None without one."""
    return shutil.which("powershell") or shutil.which("pwsh")


# ── the picture ──────────────────────────────────────────────────────────

async def capture_screen(display: int | None = None) -> Shot:
    """A PNG of one display, shrunk to `SHOT_MAX_EDGE`.

    `display` is a 1-based index over the connected displays; None means
    the primary one. Out of range falls back to the primary with a log
    line, rather than refusing a picture over an index.

    Raises ScreenError — with a sentence fit to be spoken — rather than
    ever handing back something the brain would describe wrongly.

    Call this ONLY on a turn the user drove. See the module docstring.
    """
    shell = _shell()
    if shell is None:
        raise ScreenError("I couldn't get a picture of your screen, sir")

    workdir = Path(tempfile.mkdtemp(prefix="jarvis-screen-"))
    try:
        shot_path = workdir / "screen.png"
        sample_path = workdir / "sample.bmp"
        # The script takes no shell parsing: `-Command` carries one fixed
        # invocation — `& {` script `}` — and the five paths and numbers
        # after the closing brace bind to its `param()` block as plain
        # values, never as code. (Bare `-Command "<script>"` plus trailing
        # arguments does NOT bind them: PowerShell runs the extras as
        # further commands instead. Verified live.)
        index = display if display else 0
        rc, out, err = await _run(
            shell, "-NoProfile", "-NonInteractive", "-Command",
            "& {" + _CAPTURE_PS + "}",
            str(shot_path), str(sample_path),
            str(SHOT_MAX_EDGE), str(BLANK_SAMPLE_EDGE), str(index),
            timeout=CAPTURE_TIMEOUT_SEC)
        if rc != 0 or not shot_path.exists() or not sample_path.exists():
            log.warning(f"screen capture failed: {err.strip()[:200]}")
            raise ScreenError("I couldn't get a picture of your screen, sir")

        png = shot_path.read_bytes()
        size = _png_size(png)
        if size is None:
            raise ScreenError("I couldn't get a picture of your screen, sir")
        if max(size) > SHOT_MAX_EDGE:
            # The script was asked to shrink to this edge; a bigger frame
            # means it did not do as told. Never send the full-size one
            # instead: a Retina-width capture is thousands of tokens off
            # one turn.
            raise ScreenError(
                "I couldn't get your screen down to a sensible size, sir")
        if len(png) > MAX_SHOT_BYTES:
            raise ScreenError("that picture came out far too large to send, sir")

        if _is_blank(_bmp_pixels(sample_path.read_bytes())):
            raise ScreenError(
                "your screen came back blank, sir — which usually means "
                "there was nothing to see rather than that I can't see")

        return Shot(png=png, width=size[0], height=size[1])
    finally:
        # The capture is on disk for as long as this takes and no longer.
        shutil.rmtree(workdir, ignore_errors=True)


# ── the window list ──────────────────────────────────────────────────────

def _enum_windows() -> list[tuple[int, str, int]]:
    """(hwnd, title, pid) for every visible top-level window with a title.

    The one ctypes boundary of this half, factored out so tests can fake
    it: everything below is pure parsing. Titles come from GetWindowTextW
    (Unicode, no code-page mangling); untitled windows are skipped rather
    than listed as blank rows, which would read as information.
    """
    if sys.platform != "win32":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    collected: list[tuple[int, str, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _each(hwnd, _param):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            collected.append((hwnd, title, pid.value))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_each, 0)
    except Exception as e:
        log.warning(f"window enumeration failed: {e}")
        return []
    return collected


def _process_name(pid: int) -> str:
    """The exe name owning `pid` ("chrome.exe"), or "?" when it cannot be
    known — elevated and system processes refuse the query, and a name
    that cannot be known must not cost the window its listing."""
    if sys.platform != "win32":
        return "?"
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, pid)
        if not handle:
            return "?"
        try:
            size = wintypes.DWORD(260)
            buffer = ctypes.create_unicode_buffer(260)
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(size)):
                return "?"
            return Path(buffer.value).name or "?"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return "?"


def _foreground_hwnd() -> int:
    """The window in front right now, or 0 when that cannot be known."""
    if sys.platform != "win32":
        return 0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


async def list_windows() -> list[Window]:
    """Open windows: app name, window title, and which app is in front.

    Raises ScreenError when the list cannot be had at all. An empty list
    would have JARVIS say "nothing is open" — a lie with no remedy
    attached — so a total failure refuses instead. (Single uncooperative
    windows never fail the call: they keep "?" as their owner.)
    """
    try:
        rows = await asyncio.to_thread(_enum_windows)
        front = await asyncio.to_thread(_foreground_hwnd)
    except Exception as e:
        log.warning(f"list_windows failed: {e}")
        raise ScreenError("I couldn't read what's open, sir")
    if not rows:
        raise ScreenError("I couldn't read what's open, sir")

    windows: list[Window] = []
    owners: dict[int, str] = {}
    for hwnd, title, pid in rows:
        if pid not in owners:
            owners[pid] = await asyncio.to_thread(_process_name, pid)
        windows.append(Window(app=owners[pid], title=title,
                              frontmost=(hwnd == front and front != 0)))
        if len(windows) >= MAX_WINDOWS:
            break
    return windows
