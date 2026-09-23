# tests/test_actions.py
"""Unit tests for actions.py -- system actions.

Nothing here opens a real terminal, browser, or editor: `_spawn` (argv
launches) and `_startfile` (default-handler opens) are faked at the seam,
and the assertions are about WHAT would be launched — argv lists, never
shell strings.
"""
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import actions


@pytest.fixture
def spawned(monkeypatch):
    """Capture `_spawn` argv; the launch itself always 'succeeds'."""
    calls = []

    async def fake_spawn(argv, new_console=False):
        calls.append({"argv": list(argv), "new_console": new_console})
        return True

    monkeypatch.setattr(actions, "_spawn", fake_spawn)
    return calls


@pytest.fixture
def opened(monkeypatch):
    """Capture `_startfile` targets."""
    targets = []
    monkeypatch.setattr(actions, "_startfile",
                        lambda target: targets.append(target) or True)
    return targets


# --- terminal ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_terminal_with_command_uses_encoded_command(spawned):
    out = await actions.open_terminal("echo hello; rm -rf /")
    assert out["success"] is True
    assert out["confirmation"] == "Terminal is open, sir."
    argv = spawned[0]["argv"]
    assert "powershell" in argv[0].lower() or argv[0].lower() == "wt"
    assert "-EncodedCommand" in argv
    payload = argv[argv.index("-EncodedCommand") + 1]
    assert base64.b64decode(payload).decode("utf-16-le") == \
        "echo hello; rm -rf /"
    # The hostile text rides encoded: no argv element contains it raw.
    assert not any("rm -rf" in a and a != payload for a in argv)


@pytest.mark.asyncio
async def test_open_terminal_without_command_opens_plain(spawned):
    out = await actions.open_terminal()
    assert out["success"] is True
    argv = spawned[0]["argv"]
    assert "-EncodedCommand" not in argv


@pytest.mark.asyncio
async def test_open_terminal_failure_speaks_plainly(monkeypatch):
    async def fail(argv, new_console=False):
        return False
    monkeypatch.setattr(actions, "_spawn", fail)
    out = await actions.open_terminal("x")
    assert out["success"] is False
    assert "trouble" in out["confirmation"]


@pytest.mark.asyncio
async def test_open_terminal_with_no_host_is_a_clean_failure(monkeypatch):
    monkeypatch.setattr(actions, "_wt", lambda: None)
    monkeypatch.setattr(actions, "_powershell", lambda: None)
    out = await actions.open_terminal("x")
    assert out["success"] is False


# --- browser ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_browser_prefers_an_installed_chrome(spawned, monkeypatch,
                                                        tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")  # exists: isfile is what matters
    monkeypatch.setattr(actions.shutil, "which",
                        lambda name: str(exe) if "chrome" in name else None)
    hostile = 'https://x.example/?q="onmouseover=1'
    out = await actions.open_browser(hostile, "chrome")
    assert out["success"] is True
    assert out["confirmation"] == "Pulled that up in Chrome, sir."
    argv = spawned[0]["argv"]
    assert argv == [str(exe), hostile]  # the URL is one argv element


@pytest.mark.asyncio
async def test_open_browser_falls_back_to_the_default(opened, monkeypatch):
    monkeypatch.setattr(actions.shutil, "which", lambda name: None)
    monkeypatch.setattr(actions.os.path, "isfile", lambda p: False)
    out = await actions.open_browser("https://example.com/", "firefox")
    assert out["success"] is True
    assert out["confirmation"] == "Pulled that up in Firefox, sir."
    assert opened == ["https://example.com/"]


@pytest.mark.asyncio
async def test_open_chrome_is_open_browser_chrome(spawned, monkeypatch,
                                                  tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(actions.shutil, "which",
                        lambda name: str(exe) if "chrome" in name else None)
    out = await actions.open_chrome("https://example.com/")
    assert out["success"] is True
    assert spawned[0]["argv"][0] == str(exe)


@pytest.mark.asyncio
async def test_chrome_tab_info_is_unknown_not_a_guess():
    assert await actions.get_chrome_tab_info() == {}


# --- editor ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_in_editor_prefers_vscode(spawned, monkeypatch, tmp_path):
    code = tmp_path / "code.cmd"
    code.write_bytes(b"")
    monkeypatch.setattr(actions.shutil, "which",
                        lambda name: str(code) if name == "code" else None)
    out = await actions.open_in_editor(str(tmp_path / "a.py"))
    assert out["success"] is True
    assert out["editor"] == "VS Code"
    assert spawned[0]["argv"] == [str(code), str(tmp_path / "a.py")]


@pytest.mark.asyncio
async def test_open_in_editor_falls_back_to_the_default(opened, monkeypatch):
    monkeypatch.setattr(actions.shutil, "which", lambda name: None)
    monkeypatch.setattr(actions.os.path, "isfile", lambda p: False)
    out = await actions.open_in_editor("C:\\work\\a.py")
    assert out["success"] is True
    assert out["editor"] == "your editor"
    assert opened == ["C:\\work\\a.py"]


@pytest.mark.asyncio
async def test_open_in_editor_reports_failure(spawned, monkeypatch, tmp_path):
    code = tmp_path / "code.cmd"
    code.write_bytes(b"")
    monkeypatch.setattr(actions.shutil, "which",
                        lambda name: str(code) if name == "code" else None)

    async def fail(argv, new_console=False):
        return False
    monkeypatch.setattr(actions, "_spawn", fail)
    out = await actions.open_in_editor("x.py")
    assert out["success"] is False
    assert "wouldn't open that" in out["confirmation"]


# --- the rule itself --------------------------------------------------------------

def test_no_shell_anywhere_in_this_module():
    """Model text must never pass through a shell: grep the source for
    the flags and functions that would allow it."""
    src = Path(actions.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src
    assert "os.system" not in src
    assert "os.popen" not in src
    assert "subprocess.call" not in src
    assert "cmd /c" not in src.lower()
