# JARVIS — Architecture (Windows + OpenCode port)

Target: keep every behavior of the macOS + Claude Code build, running on
**Windows + OpenCode**. Voice-first assistant, recorded runs at `/dashboard`,
long-lived brain, MCP tool channel, memory as Markdown, SQLite for runs/usage.

Source of truth for the current build: `CLAUDE.md`, `server.py` (~7000 lines),
`brain.py`, `run_executor.py`, `session_watch.py`, `session_steer.py`,
`dialog.py`, `actions.py`, `screen.py`, `notifier.py`, `preflight.py`,
`jarvis_mcp.py`, `claude_env.py`, `stream_parser.py`, `data_paths.py`.

## 1. System overview

### Current (macOS + Claude Code)

```
Browser (Chrome, mic) ⇄ Vite :5173 ⇄ FastAPI :8340 (HTTPS, Origin/Host gate)
  ├─ WebSocket voice: audio → Brain → speech.py → tts.py (Fish Audio) → audio
  ├─ Brain: long-lived `claude -p --input-format stream-json
  │          --output-format stream-json ... --mcp-config ...` over stdin,
  │          subscription login, CLAUDE.md persona in data/jarvis/
  ├─ MCP child: jarvis_mcp.py (stdio) → POST /internal/tool (tool token)
  ├─ Runs: RunExecutor spawns `claude -p --output-format stream-json` per run
  │         → stream_parser.py → run_store.py (SQLite) → /api/runs, /ws/runs,
  │         /dashboard
  ├─ Sessions: session_watch.py reads ~/.claude[/-orcha]/sessions/*.json +
  │            projects/<encoded-cwd>/<sessionId>.jsonl; session_steer.py posts
  │            to inbox Unix socket (CLAUDE_CODE_MESSAGING_TOKEN)
  └─ macOS: actions.py / dialog.py / screen.py / notifier.py via
            osascript + screencapture + sips + Keychain `security`
```

### Target (Windows + OpenCode)

```
Browser (Chrome/Edge, mic) ⇄ Vite :5173 ⇄ FastAPI :8340 (same gate)
  ├─ WebSocket voice: UNCHANGED (speech.py, tts.py, frontend voice/orb)
  ├─ Brain: `OpencodeBrain` — one opencode SESSION, one short-lived
  │          `opencode run --format json` child per turn (`--session <sid>
  │          --continue`, `--model`, `--auto`, `--dir` brain-home, prompt on
  │          argv). Session persists server-side; a stuck turn costs a kill,
  │          never the conversation. DECIDED 2026-09-23 by live spike:
  │          continue works headless with cache intact, `--dir` must be
  │          passed on every turn (continuing a `--dir` session without it
  │          hangs), CLAUDE.md in `--dir` loads as instructions, and `run`
  │          has no `--tools`/`--system-prompt`/`--agent` flags (allowlist
  │          moves to opencode.json; `--variant`/`--agent` omitted pending
  │          a provider mapping). Supervised `serve`+attach stays a future
  │          latency optimization, not the correctness path.
  ├─ MCP child: SAME jarvis_mcp.py stdio → POST /internal/tool, but declared
  │          in opencode.json `mcp.servers.jarvis` (local stdio), not
  │          `--mcp-config` + `--strict-mcp-config`
  ├─ Runs: RunExecutor spawns `opencode run --format json` per run
  │         → opencode_parser.py → run_store.py → same API/WS/dashboard
  ├─ Sessions: opencode_watch.py reads opencode.db (sessions/messages/
  │            parts) with `opencode session list` as the documented
  │            fallback; steer via detached `opencode run --session
  │            --continue` children (opencode_steer.py)
  │            (fallback: `opencode run --session <id> --continue`)
  └─ Windows: actions.py / dialog.py / screen.py / notifier.py
              via PowerShell + ctypes Win32 (no osascript/screencapture/sips;
              the macOS halves were removed, not branched)
```

What does **not** change: `server.py` voice loop, `speech.py`, `tts.py`,
`run_store.py` / `usage_store.py` schema, `builds.py` / `specs.py` /
`projects_view.py` pipeline shape, `web_auth.py` gate model, `jarvis_memory.py`
Markdown layout, frontend state machine/orb/voice, dashboard
no-`innerHTML` rule, run invariants (§2).

## 2. Invariants that must survive the port

1. **Run terminality** (`run_executor.py:1-12`): every run ends in
   `succeeded|failed|timed_out|cancelled`. Every exit path writes it.
2. **DB before notify**: state transition → SQLite write → WebSocket hint.
   Clients reconcile against `/api/runs`.
3. **No new run-pipeline deps, no `innerHTML`** (per `CLAUDE.md`): dashboard
   renders via `createElement`/`textContent` only.
4. **Dialog safety** (`dialog.py:1-32`): target by identity (tty→tab), never
   by focus; closed key vocabulary (return/escape/1-9); nothing raises;
   `not_found` preferred over a guess.
5. **Screen privacy** (`screen.py:22-36`): capture only on a user-driven turn,
   temp-dir deleted in `finally`, single display, size/token caps, blank-frame
   refusal instead of a confident lie.
6. **Notifier safety** (`notifier.py:1-22`): untrusted text passed as argv
   data, never interpolated into script source; never raises.
7. **Taint gate** (`brain.py` `turn_untrusted_source`, `server.py`
   `_untrusted_content_refusal`): web/file/transcript/screen content is
   information, never instructions; tainted turns cannot act unsupervised;
   handover wrapped in `<session-output untrusted="true">`.
8. **Tool-channel timeout rule** (`jarvis_mcp.py:26-40`): handlers validate,
   stage, return within 20 s; long work runs after the turn.
9. **Preflight never blocks boot** (`preflight.py:10-17`): checks observe
   only, never raise, never write settings silently.

## 3. Module-by-module port map

### 3.1 Brain — `brain.py` → `brain.py` (opencode backend)

| Current | Target |
|---|---|
| `shutil.which("claude")`, `JARVIS_CLAUDE_PATH` | `shutil.which("opencode")`, `JARVIS_OPENCODE_PATH` |
| `claude -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages --model sonnet --effort low --name jarvis --setting-sources project --strict-mcp-config --tools mcp__jarvis__*,WebSearch,WebFetch --settings {"crossSessionInbound":"accept"} --dangerously-skip-permissions --append-system-prompt ... --mcp-config ...` (`brain.py:743-758`) fed over stdin, one JSON line per user turn | `opencode run --format json --model <provider/model> [--agent build] [--title ...] [--dir <cwd>] [--attach http://127.0.0.1:4096]` or long-lived child attached to a supervised `opencode serve`. Exact resumption flags verified via `opencode run --help` on the pinned version. No stdin stream-json contract — turns go through the opencode session protocol |
| Warm-up `"(system) Warm-up. Reply with exactly: OK"` + `_classify_fatal_failure` on `failed to authenticate\|oauth` | Keep warm-up turn + restart budget, but reclassify fatal auth from opencode signals (`opencode auth` / `auth.json` missing, 401/invalid API key, provider errors). `claude auth status` wording must not be matched |
| `ALLOWED_TOOLS` as `mcp__jarvis__*` + `WebSearch/WebFetch` allowlist | Same allowlist intent, new names: opencode exposes MCP tools per server (verify whether `mcp__jarvis__*` prefix survives; if opencode flattens to `jarvis__*` or bare names, keep a translation table in one place). Web tools become whatever the opencode model provides natively or via opencode MCP search servers |
| `--append-system-prompt` launch prompt with `plain_name`/`plain_phrase` walls + `wrap_handover` + `MAX_BOOT_PROJECTS=12` | Keep all three walls verbatim; only the transport of the prompt changes (CLI flag vs config `instructions` vs first message — pick the opencode-supported slot that is operator prose, not user text) |
| `claude_env.child_env()` scrub of `ANTHROPIC_*`/`CLAUDE_CODE_*` | Replace with `opencode_env.child_env()`: **pass provider keys through** (opencode bills providers, not a single subscription). Scrub only variables that would redirect the binary itself. See §3.7 |
| Baseline/context accounting off `input_tokens + cache_read` | Re-derive from opencode usage events; keep `baseline_tokens` = warm-up cost, `conversation_tokens` = current − baseline, same rotation thresholds. Do not sum cache-creation twice (see `brain.py:370-384` comment) |
| `active_projects()` from session watcher | Same callable, new watcher (opencode sessions) |

Keep: generation rotation with handover, `_Turn` bookkeeping, `on_state`
listeners, rate-limit fail-open (`BLOCKING_RATE_LIMIT_STATUSES`), per-turn
taint (`mark_untrusted_content`).

### 3.2 Run pipeline — `run_executor.py` + `stream_parser.py`

| Current | Target |
|---|---|
| Spawn `claude -p --output-format stream-json --model sonnet [--dangerously-skip-permissions]` in project cwd, `limit=STREAM_LINE_LIMIT` (64 MiB) | Spawn `opencode run --format json --model <provider/model> [--agent ...]` in project cwd with the same `limit=` on stdout/stderr readers, same `_EVENT_BATCH/_EVENT_FLUSH_SEC`, `_STDERR_*`, `_EOF_EXIT_GRACE_SEC`, `_IDLE_OUTPUT_SEC` (30 min), `_DEFAULT_TIMEOUT_SEC` (6 h), `MAX_BOUND_SEC` (24 h) semantics |
| `stream_parser.py`: `parse_line/event_kind/extract_init_metadata/extract_result_metrics/extract_assistant_usage` over claude types (`init/assistant/result/...`) | New `opencode_parser.py` with the **same function names** so the executor diff stays small. Map opencode `--format json` event vocabulary (verify live: session/message/part/permission/step events) to `{kind, text, usage, cost, is_error}`. Unknown events preserved verbatim, never interpreted. Keep recorded-fixture test pattern |
| `JARVIS_SKIP_PERMISSIONS` → `--dangerously-skip-permissions` | Map to opencode's permission bypass for unattended runs (verify flag name, e.g. approval/auto-approve mode in config or CLI; never leave a run waiting on an interactive prompt). Env var name stays `JARVIS_SKIP_PERMISSIONS` |
| `JARVIS_RUN_TIMEOUT_SEC` / `JARVIS_RUN_IDLE_SEC` bounds | Unchanged names and defaults |

Executor internals (permit pool, `_drive`, batching, stderr drain, cancel)
stay identical; only command construction + line parsing change.

### 3.3 Sessions — `session_watch.py` + `session_steer.py`

Claude roster model disappears entirely:

- Delete: `DEFAULT_ROOTS = ("~/.claude", "~/.claude-orcha")`, `config_roots()`,
  `encode_cwd`, `transcript_path` (`projects/<encoded-cwd>/<id>.jsonl`),
  `tail_objects`/`recap_from` over `ai-title/last-prompt/assistant` line types,
  `CLAUDE_CONFIG_DIR`, roster `pid/sessionId/cwd/messagingSocketPath`.
- New `opencode_watch.py` (or retargeted `session_watch.py`) sources, in
  priority order:
  1. `opencode session list --format json` (process-free, no pid inference;
     verified on 1.18.32: returns `id/title/updated/created/projectId/
     directory` per session);
  2. on-disk state at `%USERPROFILE%\.local\share\opencode\` (Windows) /
     `~/.local/share/opencode/` (POSIX). MEASURED on 1.18.32: sessions live
     in SQLite `opencode.db` — tables `session`, `message`, `part`,
     `session_message`, `event`, `todo`, `project`, `project_directory` —
     NOT in per-session JSON files (older JSON layouts are gone); only
     `storage/session_diff/<id>.json` and `storage/migration` remain as
     files. Read via `sqlite3` (stdlib) or the `session list` CLI, never by
     assuming JSON paths;
  3. optional live attach to `opencode serve` HTTP for richer state.
- Keep the **derived layer** untouched: `SessionState`, `FRESH/NEEDS_YOU/
  WORKING/SHELL/IDLE/GONE/UNKNOWN`, `_derive_state`, voice naming
  (`_assign_voice_names`, topic→state→age), `_mark_primary` + margin,
  `resolve()`, `Snapshot.excluding()`, `GONE_RETENTION_SEC`/`MIN_WORK_SEC`,
  watcher pub/sub thread discipline (`call_soon_threadsafe`).
- `pid_alive` (`os.kill(pid,0)`) does not work on Windows: replace liveness
  with opencode-reported state + optional `OpenProcess` probe via ctypes;
  never `os.kill` on Windows.
- `brain_cwd()` filter: keep, but compare against opencode session cwd for
  the JARVIS brain/serve home instead of `data/jarvis`.
- Steering (`session_steer.py`): Unix-socket
  `{"type":"auth","token":...}` + `{"type":"user",...}` over
  `AF_UNIX` + `CLAUDE_CODE_MESSAGING_TOKEN` has **no opencode equivalent**.
  Shipped as `opencode_steer.py`: a detached `opencode run --session
  <sid> --continue` child per steer (proven live), same
  `sent/not_live/refused/failed` vocabulary, same staged flow. No
  `--auto`/`--model`/`--title` on steered turns; `--dir` always the
  session's own (continuing elsewhere hangs the CLI).

### 3.4 OS actions — `actions.py`

| macOS | Windows |
|---|---|
| `osascript` Terminal.app `do script`, Ocean theme mark | `wt.exe` / Windows Terminal: `wt -w <window> new-tab --title JARVIS powershell -NoExit -Command <cmd>`; theme mark via tab color/title instead of Ocean profile. Fallback: `powershell Start-Process` / `cmd /c start`. Never build a shell string from model text without quoting — use argv lists + PowerShell `-EncodedCommand` or `Start-Process -ArgumentList` |
| `osascript` Google Chrome / Firefox `open location` | `os.startfile(url)` or `Start-Process <url>` (default browser) + explicit `chrome.exe`/`msedge.exe`/`firefox.exe` lookup via `shutil.which` and registry/App Paths. Keep `applescript_escape` analogue: URL passed as argv data, plus a Windows-quote guard with tests |
| `open_in_editor`: `code` binary or `/Applications/Visual Studio Code.app` or `open` | `code.cmd` via `shutil.which("code")`, else `%VSCODE%`, else `Start-Process <path>` (system default). Keep no-shell argv exec + caller-side containment wall |
| `get_chrome_tab_info` via AppleScript | Optional: UI Automation / `Get-FrontMostBrowserUrl` helper; if unavailable, return `{}` (same contract) rather than a guess |

### 3.5 Permission-prompt answering — `dialog.py`

The whole tty→Terminal-tab→System-Events-keystroke chain is macOS-only
(`ps -o tty=`, `pgrep -x Terminal`, `_ENUMERATE_SCRIPT`, `_send_script`,
Accessibility `-1743/-25211`).

Windows replacement preserving the three safety decisions:

1. **Identity, not focus**: resolve PID → console HWND via
   `GetWindowThreadProcessId` / `GetConsoleWindow` / `EnumWindows`, re-verify
   HWND still belongs to the PID at press time (`gone/moved → not_found`,
   press nothing).
2. **Closed vocabulary**: keep `normalize_key`/`spoken_key` exactly
   (return/escape/1-9 + aliases). Only `SendKeys "{ENTER}"/"{ESC}"/"1"..`
   or `keybd_event`/`SendInput` virtual-key codes reach the OS.
3. **Never raises**: keep `sent/no_tty/not_found/not_permitted/failed/bad_key`
   + `LOOKUP_TIMEOUT/SEND_TIMEOUT`. `not_permitted` now maps to UIPI /
   integrity-level / UAC refusals instead of Accessibility.
4. Foreground discipline: capture foreground HWND before, `SetForegroundWindow`
   + `AttachThreadInput` to press, restore after — the analogue of the
   `priorApp` save/restore.

New dependency decision: prefer `ctypes` + PowerShell only (no `pywin32`/
`pywinauto`) unless the team explicitly accepts one; isolate in
`dialog_win.py` behind the same `answer(pid, key)` signature so
`server.tool_answer_dialog` is unchanged.

### 3.6 Seeing the machine — `screen.py`

| macOS | Windows |
|---|---|
| `screencapture -x -m/-D` | Primary: Win32 `BitBlt`/`PrintWindow` via `ctypes`, or PowerShell `System.Windows.Forms.Screen` + `Graphics.CopyFromScreen`. Single-display default, optional display index |
| `sips -Z 1280` resize, 32 px BMP blank check | Resize via .NET `System.Drawing` or Pillow **only if** the team accepts the dep; otherwise ship a tiny downscaler or keep raw + byte cap. Keep `SHOT_MAX_EDGE=1280`, `MAX_SHOT_BYTES=4M`, `BLANK_SAMPLE_EDGE=32`, `BLANK_MAD=1.5` numbers |
| `CGPreflightScreenCaptureAccess` probe; blank frame = missing permission | No OS prompt for desktop duplication on Windows; keep the probe hook returning `None` (unknown) and rely on the blank-frame check. Keep `_NO_PERMISSION` path for future restricted contexts (e.g.-least-privilege stores) |
| `osascript` System Events window list + `-1728/-25211` Accessibility gate | `EnumWindows` + `GetWindowText` + `IsWindowVisible` via `ctypes`; `MAX_WINDOWS=12`. UWP title quirks normalized in one place. No Accessibility equivalent — failure is spoken, never an empty "nothing is open" |

Keep: `_run(*args, timeout)` single seam, `Shot`/`Window` dataclasses,
`ScreenError` spoken messages, `ToolImage` MCP image-block route.

### 3.7 Environment — `claude_env.py` → `opencode_env.py`

`claude_env.py` exists for one billing rule: scrub `ANTHROPIC_*` /
`CLAUDE_CODE_*` / `CLAUDECODE` so children use the subscription login.
That rule **inverts** on opencode: provider keys in the environment are how
opencode authenticates (`auth.json` + env provider keys). Scrubbing
`ANTHROPIC_*` would break the brain.

- New `opencode_env.py`: pass through `PATH/HOME/USERPROFILE`,
  `OPENCODE_*`, provider keys (`ANTHROPIC_*`, `OPENAI_*`, ...); scrub only
  variables that redirect the binary under test (keep an explicit denylist,
  default-empty, documented).
- Keep `STREAM_LINE_LIMIT = 64 MiB` and pass `limit=` to every
  `create_subprocess_exec` reading an opencode child.
- `preflight._check_anthropic_key_leftover` flips meaning: warn when a
  provider key the configured model needs is **absent**, not when present.

### 3.8 Tool channel — `jarvis_mcp.py`, `server.py:/internal/tool`, `data_paths.tool_token`

Protocol (hand-rolled JSON-RPC stdio, `PROTOCOL_VERSION`, `TIMEOUT_SEC=20`,
forward to loopback `POST /internal/tool` with bearer token) is transportable
as-is — MCP stdio is standard.

Changes:

- `TOOL_SPECS` descriptions mentioning Terminal.app / Claude Code sessions /
  `answer_dialog` limits must be reworded for Windows Terminal + opencode
  sessions (keep tool **names** stable so the brain prompt and tests churn
  minimally; add a prefix-translation shim only if opencode requires it).
- Declaration: claude `--mcp-config <mcp.json>` + `--strict-mcp-config` →
  opencode global/project `opencode.json` `mcp.servers.jarvis = {type:local,
  command:[sys.executable, jarvis_mcp.py], environment:{JARVIS_TOOL_URL,
  JARVIS_TOOL_TOKEN_FILE}}`. `_write_mcp_config` in `server.py` gains an
  opencode writer; the origin gate on `/internal/tool` stays.
- `ensure_tool_token()`: `os.getuid`, `O_NOFOLLOW`, `0o600` are POSIX-only.
  Windows port: exclusive create (`O_CREAT|O_EXCL`), no uid check (use
  `%USERPROFILE%` path + ACL tightening via `icacls` best-effort or
  `win32security` only if accepted), same `FileExistsError → open → read →
  fill-if-empty` logic, same symlink caution documented.

### 3.9 Startup checks — `preflight.py`

| Check | New meaning |
|---|---|
| `claude_cli` (`which claude`, `--version >= 2.1.224`) | `opencode` on PATH, version parsed from `opencode --version` against a pinned minimum (pin in one constant, compare as int tuple) |
| `claude_login` (`claude auth status` + Keychain `refreshTokenExpiresAt`) | `opencode auth` state: `auth.json` presence + `opencode auth status`-equivalent (verify command) under `opencode_env.child_env()`; no Keychain — read expiry only if the provider stores it locally, else honest "could not independently verify" |
| `accessibility` (osascript System Events probe) | Windows Terminal automation probe: can we resolve a test PID to a console HWND / `wt` list? Fail maps to `answer_dialog` will fail |
| `screen_recording` (`CGPreflight...`) | Informational on Windows (usually granted); keep check returning ok/unknown, never fail boot |
| `fish_api_key`, `cross_session_inbound` read-only | Unchanged shape; second check reads opencode config for the steering-acceptance equivalent instead of `~/.claude/settings.json` |
| `anthropic_key_leftover` | Inverted (see §3.7) or removed |

Keep: `_run_subprocess` seam, concurrent `run_checks`, `spoken_summary`
silence-when-healthy.

### 3.10 Web boundary, certs, frontend, browser

- `web_auth.py` Origin/Host gate: unchanged logic; add default-allowed dev
  origins for Windows hosts if needed (`JARVIS_ALLOWED_ORIGINS` still the
  escape hatch). No CORS.
- Certs: `openssl req -x509 ...` in Quick Start assumes macOS LibreSSL.
  Windows: prefer `openssl` from Git-for-Windows, else `mkcert`, else a
  documented Python self-signed fallback. `frontend/vite.config.ts` proxy
  target `https://localhost:8340` + `secure:false` stays; plain-HTTP backend
  still yields proxied-500s (keep the CLAUDE.md warning).
- Frontend (`orb.ts`, `voice.ts`, `main.ts`, `dashboard/`): portable TS;
  only UA strings, paths, and proxy/cert docs change. Mic-still-Chrome/Edge.
- `browser.py`: change `USER_AGENT` Mac token to Windows token; Playwright
  `python -m playwright install chromium` identical; `headless=False`
  research class stays test-only.

## 4. Configuration map (old → new)

| Old | New |
|---|---|
| `JARVIS_CLAUDE_PATH` (binary for brain) | `JARVIS_OPENCODE_PATH` |
| `JARVIS_BRAIN_MODEL=sonnet` | `JARVIS_BRAIN_MODEL=<provider/model>` e.g. `anthropic/claude-sonnet-4-5` (always passed explicitly; verify `--model` spelling) |
| `JARVIS_BRAIN_EFFORT=low` | Keep name; map to opencode effort/reasoning-effort if supported, else ignore with warning |
| `JARVIS_CLAUDE_CONFIG_DIRS`, `CLAUDE_CONFIG_DIR`, `~/.claude-orcha` | `OPENCODE_CONFIG` / `~/.config/opencode/opencode.json`, `OPENCODE_TEST_HOME` (tests), storage roots above |
| `ANTHROPIC_*` scrubbed | Provider keys passed through; `auth.json` is the login analogue |
| `JARVIS_SKIP_PERMISSIONS`, `JARVIS_RUN_TIMEOUT_SEC`, `JARVIS_RUN_IDLE_SEC`, `FISH_*`, `USER_NAME`, `WEATHER_*`, `JARVIS_DATA_DIR`, `JARVIS_ALLOWED_ORIGINS`, `JARVIS_DEBUG_DOCS`, `JARVIS_ENV_FILE` | Unchanged (permission flag remapped internally) |
| `jarvis_home/CLAUDE.md` persona | Same file, reworded examples (Windows Terminal, opencode sessions, no AppleScript); keep `KNOWN_TEMPLATE_HASHES` append discipline |

## 5. Verification anchors

- Pinned facts (measured 2026-09-22, Windows, opencode **1.18.32**):
  `run` supports `--format json|default`, `--model provider/model`,
  `-s/--session`, `-c/--continue`, `--fork`, `--title`, `--attach <url>`,
  `--dir`, `--agent`, `--variant`, `--auto` (dangerous: auto-approve
  permissions), `-f/--file`; `session list` supports `--format json|table`,
  `-n/--max-count`; `serve` defaults to `--hostname 127.0.0.1 --port 0`;
  `--format json` vocabulary is `step_start/text/tool_use/step_finish/error`
  (fixtures in `tests/fixtures/opencode/`). Re-verify with
  `opencode run --help` before coding against any flag.
- `pytest` (bare) stays the whole suite minus `-m browser`; new Windows-only
  tests marked `windows` or skip on non-`win32` (mirror the `browser` mark
  pattern in `pytest.ini`).
- Fakes move from `screencapture/sips/osascript/claude` to
  `opencode/BitBlt/EnumWindows/powershell` at the same seams
  (`_run_subprocess`, `_run`, `_osascript`→`_win_run`).
- Live checks: `opencode --version`, `opencode auth status`-equivalent,
  `opencode session list --format json`, `opencode run --format json "say OK"`,
  `opencode serve` + steer round-trip, dashboard run to terminal state,
  `answer_dialog` staged-then-pressed on a scratch console only.
