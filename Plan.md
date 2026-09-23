# JARVIS — Plan: macOS + Claude Code → Windows + OpenCode

Functioning stays identical; only the engine and the OS layer change.
Read `Architecture.md` first for the as-is/to-be map. This file is the
ordered work plan with acceptance criteria. `Phase.md` turns it into dated
phases; `project_documentation.md` is the manual for the finished port.

## 0. Goals and non-goals

**Goals**

- Same voice UX (British butler, 1–2 sentences), same runs pipeline and
  `/dashboard`, same session steering, same memory Markdown, same MCP tools.
- Windows 10/11 as the dev machine; OpenCode as the only agent runtime
  (brain + runs). No `claude` binary, no AppleScript, no `screencapture/sips`.
- Every invariant in `Architecture.md §2` holds on the new stack.
- `pytest` (bare) passes on Windows without network/screen/mic.

**Non-goals**

- No new product features, no persona rewrite beyond OS/runtime wording, no
  frontend redesign, no DB schema migration (reuse `run_store.py` as-is).
- No Linux support work beyond not breaking POSIX paths that already work.
- No silent settings writes (preflight stays read-only; cross-session accept
  still requires an explicit user yes).

## 1. Prerequisites (do before code)

1. Pin the OpenCode version: install latest, record `opencode --version`,
   and freeze `MIN_OPENCODE_VERSION` + exact CLI spellings from
   `opencode run --help` / `opencode session --help` / `opencode serve --help`:
   `--format json`, `--model`, `--session/-s`, `--continue`, `--title`,
   `--attach`, `--dir`, permission bypass, MCP declaration shape
   (`opencode.json` `mcp.servers`), serve HTTP endpoints.
2. Record opencode storage/auth paths on Windows:
   `%USERPROFILE%\.local\share\opencode\` (storage, `auth.json`, `log/`),
   `~/.config/opencode/opencode.json(c)` config locations, project-local
   `./<slug>/storage` behavior. Confirm with a real `opencode run`.
3. Decide the new-dependency policy: default is **stdlib + ctypes +
   PowerShell only** (no `pywin32`/`pywinauto`/`mss`/`Pillow`). Any exception
   is a recorded decision with a fallback.
4. Snapshot a green baseline: `pytest` on the current machine + `git status`
   clean. New work happens on a `windows-opencode` branch.

## 2. Workstreams (in dependency order)

### WS1 — Runtime shim + config plumbing

- Add `opencode_env.py` (pass-through provider env; keep `STREAM_LINE_LIMIT`).
  Keep `claude_env.py` untouched until the cutover so old tests still pass.
- Add `JARVIS_OPENCODE_PATH`, `JARVIS_BRAIN_MODEL=<provider/model>` handling;
  keep `JARVIS_CLAUDE_PATH` as deprecated alias with a warning.
- Add `opencode.json` writer beside the existing MCP-config writer in
  `server.py` (`_write_opencode_config`): `mcp.servers.jarvis` local stdio
  entry carrying `JARVIS_TOOL_URL` + token file env.
- Update `.env.example`, `skills/jarvis-setup/SKILL.md`, and
  `jarvis_home/CLAUDE.md` wording (no behavior change yet).
- **Accept:** `python -c "import opencode_env"` works on win32+posix;
  `pytest tests/test_claude_env* -q` still green (old path intact).

### WS2 — Event parsing + run pipeline on opencode

- Capture fixtures: `opencode run --format json "say OK"` for success, error,
  permission-hold, and long-tool cases. Save under `tests/fixtures/opencode/`.
- Add `opencode_parser.py` mirroring `stream_parser.py` API
  (`parse_line/event_kind/extract_*`). Unknown events preserved verbatim.
- Retarget `run_executor.py` command builder to `opencode run --format json`
  behind a `JARVIS_RUNTIME=opencode|claude` switch (default flips at cutover).
  Keep timeouts, batching, stderr drain, permit pool, terminal-state logic.
- **Accept:** existing `stream_parser` tests green; new
  `test_opencode_parser` green; a scratch `spawn_run` reaches a terminal
  state and appears in `/api/runs` + `/dashboard`.

### WS3 — Brain on opencode

- Rework `brain.py:command()` to build the opencode launch argv; route turns
  through the opencode session protocol (or supervised `serve` + attach).
- Re-derive usage/baseline/rotation from opencode usage events; keep
  `HANDOVER_MAX_CHARS=1200`, `wrap_handover`, `plain_name/plain_phrase`,
  `MAX_BOOT_PROJECTS=12`, `granted_tools()`, `WEB_CONTENT_TOOLS` analogue.
- Reclassify fatal-auth for opencode (`auth.json`/provider errors), keep
  restart budget + warm-up semantics.
- **Accept:** cold boot → `ready`, warm-up `OK`, one voice turn end-to-end
  (type or mic), rotation forced by a tiny test budget succeeds with fallback
  to the old generation on failure.

### WS4 — Sessions: watch + steer on opencode

- Replace roster/`*.jsonl` reading with `opencode session list --format json`
  + storage-JSON reader (`opencode_watch.py` or reworked `session_watch.py`
  behind the runtime switch). Replace `pid_alive`/`tty` with opencode state.
- Keep `SessionState`, naming (`topic→state→age`), `_mark_primary`, `resolve`,
  `Snapshot`, retention constants unchanged — repoint their inputs.
- Replace `session_steer.py` Unix-socket send with loopback HTTP to
  supervised `opencode serve` (+ documented fallback to
  `opencode run --session <id> --continue`). Keep `sent/not_live/refused/
  failed` vocabulary and staged-steer flow (`_perform_staged_steers`).
- **Accept:** two live opencode sessions show correct projects/states/names;
  `steer_session` staged → read-back → delivered; permission-held target
  reports honestly instead of claiming success.

### WS5 — Windows OS integration

- `actions_win.py`: `wt`/PowerShell terminal open, `os.startfile`/
  `Start-Process` browser open, `code.cmd` editor open, tab-info best-effort.
  Keep `{"success","confirmation"}` contracts.
- `dialog_win.py`: PID→HWND identity + `SendInput` closed vocabulary +
  foreground save/restore + `sent/no_tty/not_found/not_permitted/failed/
  bad_key`. No focus fallback. Real keypress tested **only** on a scratch
  console the tester owns.
- `screen_win.py`: `EnumWindows` list + `BitBlt`/`CopyFromScreen` capture +
  resize + blank-frame check. Keep `Shot/Window/ScreenError`, caps, temp-dir
  discipline, user-turn-only gating in `server.ACTING_TOOLS`.
- `notifier_win.py`: toast via PowerShell/WinRT from argv data (no string
  interpolation of title/message into script source). Keep truncation caps,
  5 s timeout, never-raise.
- `data_paths.ensure_tool_token`: Windows-safe exclusive-create path (no
  `getuid`/`O_NOFOLLOW`); tighten ACLs best-effort.
- **Accept:** each module unit-tested with fakes; manual matrix (open
  terminal/browser/editor, list windows, capture screen, toast, answer dialog
  on scratch console) all pass.

### WS6 — Preflight, server wiring, frontend docs

- `preflight.py`: `opencode` version/login probes, terminal-automation probe,
  screen informational, inverted provider-key check, opencode-config
  steering check. Keep never-raise + `spoken_summary` silence-when-healthy.
- `server.py`: runtime switch for brain/executor/watcher/steerer, opencode
  config writer, updated `tool_open_in_*` handlers, `DESKTOP_PATH`/
  projects-root handling on Windows (`%USERPROFILE%\Desktop` + configurable
  root), `JARVIS_ALLOWED_ORIGINS` guidance for LAN/tunnel use.
- `frontend/vite.config.ts`: keep proxy; document Windows cert flow
  (Git-for-Windows openssl → `mkcert` → Python fallback).
- `browser.py`: Windows UA token; Playwright steps unchanged.
- **Accept:** `python server.py --host 127.0.0.1` + `npm run dev` on Windows,
  Chrome/Edge mic turn works, preflight speaks only real problems.

### WS7 — Tests, cleanup, cutover

- Extend `pytest.ini` marks (`windows` like `browser`); new tests mock
  `opencode`/Win32/PowerShell at existing seams; keep POSIX coverage via the
  runtime switch or `skipif`.
- Update `requirements.txt` only if the dependency decision (§1.3) allows;
  otherwise no new deps.
- Update `README.md`, `CLAUDE.md` (or successor), `.env.example`,
  `skills/jarvis-setup/SKILL.md`, `jarvis_home/CLAUDE.md` + template hashes.
- Remove (or gate behind `JARVIS_RUNTIME=claude`) the macOS-only paths only
  **after** the Windows suite is green; never strand macOS users mid-branch.
- **Accept:** bare `pytest` green on Windows; `pytest -m browser` still
  opt-in; docs match the shipped behavior.

## 3. Risks and mitigations

| Risk | Mitigation |
|---|---|
| opencode JSON event vocabulary differs from docs/version | Fixture-first: capture real `--format json` output for every path before coding the parser; preserve unknown events |
| No inbox-socket equivalent for steering | Supervised `opencode serve` as the primary path; `--session --continue` fallback; honest `not_live` when neither answers |
| `SendInput` hits the wrong window | HWND re-verification at press time + closed vocabulary + no focus fallback + scratch-console-only testing |
| Screen capture needs a new dep | Default stdlib/ctypes+.NET; Pillow/`mss` only as a recorded exception |
| Provider billing confusion (old `ANTHROPIC_*` scrub) | `opencode_env` passes keys through; preflight warns on **missing** keys; docs state the billing model up front |
| Cert/Vite proxy confusion on Windows | Keep `https://localhost:8340` + `secure:false`; document the plain-HTTP-500 symptom verbatim from `CLAUDE.md` |

## 4. Definition of done

- [ ] Brain boots on opencode, holds a voice conversation, rotates cleanly.
- [ ] Runs spawn on opencode, always reach a terminal state, show live on
      `/dashboard`, reconcile via `/api/runs`.
- [ ] Sessions list/steer correctly; permission prompts route to
      `answer_dialog`, not `steer_session`.
- [ ] Terminal/browser/editor, window list, screenshot, toast, and dialog
      keypress all work on Windows within their safety contracts.
- [ ] Preflight reports honestly; healthy boot is silent.
- [ ] Bare `pytest` green on Windows; docs (the four files + README/SKILL)
      match reality.
