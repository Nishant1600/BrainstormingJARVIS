# tests/test_screen_sight.py
"""Unit tests for screen.py — the machine's eyes.

The real PowerShell capture and the real EnumWindows run live exactly
once each, by hand (a 1280x720 PNG and an 11-window list, both correct);
everything here runs against fakes: `_run` is mocked at the single
subprocess seam, capture files are planted by the fake, and the ctypes
helpers are monkeypatched. No pixels leave the test process.
"""
import asyncio
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import screen
from screen import ScreenError


def _png(width, height, extra=b""):
    """A minimal valid PNG with correct IHDR, plus optional trailing junk
    (decoders — and `_png_size` — only read the header)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    crc = struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + crc
            + extra)


def _bmp(width, height, color=(10, 20, 30)):
    """A 24-bit BMP, one flat colour unless told otherwise."""
    row = bytes(color) * width
    padding = (-len(row)) % 4
    pixels = (row + b"\x00" * padding) * height
    offset = 14 + 40
    header = (b"BM" + struct.pack("<I", offset + len(pixels)) + b"\x00\x00\x00\x00"
              + struct.pack("<I", offset))
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                      len(pixels), 0, 0, 0, 0)
    return header + dib + pixels


def _busy_bmp():
    """32x32, half black half white: maximally un-blank."""
    row = bytes((0, 0, 0)) * 16 + bytes((255, 255, 255)) * 16
    pixels = row * 32
    offset = 14 + 40
    header = (b"BM" + struct.pack("<I", offset + len(pixels)) + b"\x00\x00\x00\x00"
              + struct.pack("<I", offset))
    dib = struct.pack("<IiiHHIIiiII", 40, 32, 32, 1, 24, 0,
                      len(pixels), 0, 0, 0, 0)
    return header + dib + pixels


class _Run:
    """Fake `_run`: plants the files the real script would have written."""

    def __init__(self, rc=0, png=None, bmp=None, out="OK 1280 720"):
        self.rc = rc
        self.png = png
        self.bmp = bmp
        self.out = out
        self.argv = None

    async def __call__(self, *args, timeout):
        self.argv = list(args)
        for path, blob in (("screen.png", self.png), ("sample.bmp", self.bmp)):
            if blob is None:
                continue
            for a in args:
                if str(a).endswith(path):
                    Path(a).write_bytes(blob)
        return self.rc, self.out, ""


@pytest.fixture
def run(monkeypatch):
    fake = _Run(png=_png(1280, 720), bmp=_busy_bmp())
    monkeypatch.setattr(screen, "_run", fake)
    monkeypatch.setattr(screen, "_shell", lambda: "powershell.exe")
    return fake


# --- capture ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_returns_a_sized_shot(run):
    shot = await screen.capture_screen()
    assert (shot.width, shot.height) == (1280, 720)
    assert shot.png.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_capture_passes_the_display_index(run):
    await screen.capture_screen(display=2)
    assert run.argv[-1] == "2"
    await screen.capture_screen()
    assert run.argv[-1] == "0"  # None means the primary display


@pytest.mark.asyncio
async def test_failed_capture_is_spoken_not_returned(monkeypatch):
    async def fail(*args, timeout):
        return 1, "", "boom"
    monkeypatch.setattr(screen, "_run", fail)
    monkeypatch.setattr(screen, "_shell", lambda: "powershell.exe")
    with pytest.raises(ScreenError):
        await screen.capture_screen()


@pytest.mark.asyncio
async def test_missing_files_are_spoken_not_returned(monkeypatch):
    async def empty(*args, timeout):
        return 0, "OK 1 1", ""
    monkeypatch.setattr(screen, "_run", empty)
    monkeypatch.setattr(screen, "_shell", lambda: "powershell.exe")
    with pytest.raises(ScreenError):
        await screen.capture_screen()


@pytest.mark.asyncio
async def test_oversize_picture_is_refused(run):
    run.png = _png(1280, 720, extra=b"x" * (5 * 1024 * 1024))
    with pytest.raises(ScreenError, match="too large"):
        await screen.capture_screen()


@pytest.mark.asyncio
async def test_blank_frame_is_refused(run):
    run.bmp = _bmp(32, 32)
    with pytest.raises(ScreenError, match="blank"):
        await screen.capture_screen()


@pytest.mark.asyncio
async def test_no_shell_means_no_picture(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("must not spawn without a shell")
    monkeypatch.setattr(screen, "_run", boom)
    monkeypatch.setattr(screen, "_shell", lambda: None)
    with pytest.raises(ScreenError):
        await screen.capture_screen()


def test_recording_permission_is_unknown_by_design():
    """There is no per-app grant to probe on Windows; the blank-frame
    check carries the load instead."""
    assert screen.screen_recording_granted() is None


# --- window list --------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_windows_names_apps_and_marks_front(monkeypatch):
    monkeypatch.setattr(screen, "_enum_windows", lambda: [
        (11, "Inbox - me@example.com", 101),
        (22, "jarvis - Visual Studio Code", 202),
        (33, "Untitled", 101),
    ])
    monkeypatch.setattr(screen, "_foreground_hwnd", lambda: 22)
    monkeypatch.setattr(screen, "_process_name",
                        lambda pid: {101: "msedge.exe",
                                     202: "Code.exe"}[pid])
    windows = await screen.list_windows()
    assert [(w.app, w.title, w.frontmost) for w in windows] == [
        ("msedge.exe", "Inbox - me@example.com", False),
        ("Code.exe", "jarvis - Visual Studio Code", True),
        ("msedge.exe", "Untitled", False),
    ]


@pytest.mark.asyncio
async def test_list_windows_caps_the_list(monkeypatch):
    monkeypatch.setattr(
        screen, "_enum_windows",
        lambda: [(i, f"w{i}", 100 + i) for i in range(50)])
    monkeypatch.setattr(screen, "_foreground_hwnd", lambda: 0)
    monkeypatch.setattr(screen, "_process_name", lambda pid: "a.exe")
    assert len(await screen.list_windows()) == screen.MAX_WINDOWS


@pytest.mark.asyncio
async def test_uncooperative_owner_becomes_unknown_not_fatal(monkeypatch):
    monkeypatch.setattr(screen, "_enum_windows",
                        lambda: [(11, "Secrets", 999)])
    monkeypatch.setattr(screen, "_foreground_hwnd", lambda: 0)
    monkeypatch.setattr(screen, "_process_name", lambda pid: "?")
    (window,) = await screen.list_windows()
    assert (window.app, window.title) == ("?", "Secrets")


@pytest.mark.asyncio
async def test_empty_list_is_a_refusal_not_good_news(monkeypatch):
    monkeypatch.setattr(screen, "_enum_windows", lambda: [])
    monkeypatch.setattr(screen, "_foreground_hwnd", lambda: 0)
    with pytest.raises(ScreenError):
        await screen.list_windows()
