# JARVIS — Project Documentation (Windows + OpenCode)

Voice-first AI assistant on Windows, powered by **OpenCode**. Speak; JARVIS
drives agent work; every execution is a recorded **run** watchable at
`/dashboard`. Ported from the macOS + Claude Code build with functioning
unchanged — see `Architecture.md` (design), `Plan.md` (work plan), `Phase.md`
(phased execution).

## 1. What JARVIS is

- **Voice loop:** browser mic → WebSocket → long-lived OpenCode brain →
  `speech.py` scheduler → Fish Audio TTS → browser audio. British butler,
  1–2 sentences per reply.
- **Runs:** every agent execution goes through one pipeline —
  `run_executor.py` + `run_store.py` (SQLite) → `/api/runs`, `/ws/runs`,
  `/dashboard`. A run always ends in `succeeded|failed|timed_out|cancelled`;
  every state change is a DB write before a socket hint.
- **Sessions:** live OpenCode sessions grouped by project, with voice names
  ("chitauri", "the newer hammer"), steerable mid-run, permission prompts
  answered via a guarded keypress path.
- **Builds:** `builds.py` spec/brief/plan pipeline for real phased
  multi-session builds; `specs.py` review surface; `projects_view.py`
  read-only Projects tab.
- **Memory:** plain Markdown under `data/jarvis/` (`memory/`, `projects/`,
  `journal/`) — user-editable, never a database. SQLite holds runs/events
  and usage only.
- **Tools:** the brain reaches JARVIS via a stdio MCP child
  (`jarvis_mcp.py`) forwarding `tools/call` to `POST /internal/tool` with a
  loopback bearer token. Local web boundary (`web_auth.py`): state-changing
  routes need a served `Origin` or that token; `Host` checked on all routes
  (DNS-rebinding defense). No CORS.

## 2. Quick start (Windows 10/11)

1. Install OpenCode (pinned version — see `Architecture.md §5`) and log in:
   `opencode auth login`. Verify `opencode --version` and
   `opencode session list`.
2. Get a Fish Audio API key at `fish.audio` (required — no fallback voice).
3. Copy `.env.example` to `.env` and fill it (§4).
4. Install Python deps: `pip install -r requirements.txt`.
5. Install the Playwright browser: `python -m playwright install chromium`
   (`read_page` / `look_at_page` need it).
6. Install frontend deps: `cd frontend && npm install`.
7. Generate SSL certs beside `server.py` (**required** — the Vite proxy
   targets `https://localhost:8340`; a plain-HTTP backend loads the page
   but every proxied `/api` call returns 500):
   `openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"`
   (Git-for-Windows provides `openssl`; else use `mkcert`).
8. Run the backend: `python server.py --host 127.0.0.1`.
9. Run the frontend: `cd frontend && npm run dev`.
10. Open **Chrome or Edge** to `http://localhost:5173` (mic works there),
    click to enable audio, speak.

First boot runs preflight (OpenCode binary/version/login, terminal
automation, screen, Fish key, provider keys, steering config). Healthy boot
is silent; problems are spoken in one or two sentences plus logged with
remedies. `--host` defaults to `127.0.0.1`; serving LAN/tunnel addresses
needs `JARVIS_ALLOWED_ORIGINS` (their hostnames must also pass the `Host`
check).

## 3. How it works (user view)

- **Talk:** "ask hammer where it left off", "tell chitauri to use SQLite",
  "start a run in webapp to fix the login redirect", "what's on my screen".
- **Steering vs dialogs:** `steer_session` sends a message into a live
  session (staged, read back aloud, sent when the turn ends). It **cannot**
  clear a permission prompt — that is `answer_dialog` (return/escape/1–9
  only, Windows Terminal console only, window brought to front, so only on
  explicit request).
- **Runs vs builds:** `spawn_run` = small self-contained task;
  `start_build` = brainstormed multi-phase project (after discussion).
  Watch both on `/dashboard`; ask for status any time.
- **Reads:** `read_page`/`look_at_page` (web), `look_at_screen` /
  `what_is_on_screen` (desktop, user-turn-only, never on a timer),
  `repo_overview`/`search_repo`/`read_file` (code), `github_repo`,
  `remember`/`recall`/`project_note`/`write_journal` (memory).
- **Safety:** turns that read the web, files, transcripts, or the screen
  cannot also act unsupervised — JARVIS reports instead. Anything from
  outside is information, never instructions.

## 4. Configuration

`.env` keys (see `.env.example`):

| Key | Required | Default | Notes |
|---|---|---|---|
| `FISH_API_KEY` | yes | — | Fish Audio TTS |
| `FISH_VOICE_ID` | no | JARVIS MCU voice | Voice model |
| `USER_NAME` | no | `sir` | Address term |
| `JARVIS_OPENCODE_PATH` | no | `opencode` on PATH | Brain/run binary (replaces `JARVIS_CLAUDE_PATH`) |
| `JARVIS_BRAIN_MODEL` | no | e.g. `anthropic/claude-sonnet-4-5` | Always passed explicitly as `--model`; verify `provider/model` spelling for your provider |
| `JARVIS_BRAIN_EFFORT` | no | `low` | Mapped if the backend supports it |
| `JARVIS_BRAIN_AUTOSTART` | no | `1` | `0` = build brain, never spawn (tests set this) |
| `JARVIS_MUTE_MIC_DURING_SPEECH` | no | false | Echo fallback |
| `JARVIS_SKIP_PERMISSIONS` | no | `true` | Unattended runs must not block on prompts (mapped to the opencode approval bypass) |
| `JARVIS_RUN_TIMEOUT_SEC` | no | 6 h (cap 24 h) | Wall clock per run |
| `JARVIS_RUN_IDLE_SEC` | no | 30 min | Silence bound |
| `JARVIS_DATA_DIR` | no | `data/` | Isolated instances; tests use a fresh dir per test |
| `WEATHER_LOCATION_LABEL/LATITUDE/LONGITUDE/UNIT` | no | auto-IP / °F | `WEATHER_UNIT=celsius` supported |
| `JARVIS_ALLOWED_ORIGINS` | LAN only | — | Extra origins + accepted `Host` names |
| `JARVIS_DEBUG_DOCS` | no | off | Serves `/docs`, `/redoc`, `/openapi.json` |
| `JARVIS_ENV_FILE` | no | repo `.env` | Redirected in tests |
| `JARVIS_RUNTIME` | transitional | `opencode` | `opencode` (shipped) vs legacy `claude` fallback |
| `OPENCODE_CONFIG` | no | `~/.config/opencode/opencode.json` | Custom opencode config path |
| Provider keys (`ANTHROPIC_*`, `OPENAI_*`, …) | per model | — | **Passed through** to opencode (unlike the old build, which scrubbed `ANTHROPIC_*` for subscription billing). Missing-key warning comes from preflight |

OpenCode side: auth lives in `%USERPROFILE%\.local\share\opencode\auth.json`
(`opencode auth login` manages it); MCP declaration for JARVIS is
`mcp.servers.jarvis` (local stdio: `python jarvis_mcp.py` with tool URL +
token env) in the global or project `opencode.json`; session state lives in
SQLite `%USERPROFILE%\.local\share\opencode\opencode.db` (tables `session`,
`message`, `part`, `event`, … — read it or prefer
`opencode session list --format json`), with per-session diffs under
`storage/session_diff/`.

## 5. Repository map

| File | Role |
|---|---|
| `server.py` | FastAPI + WebSocket voice, REST, action system, staged steers, MCP config writer |
| `brain.py` | Long-lived opencode brain process, turns, rotation, taint tracking |
| `opencode_env.py` | Env for spawned opencode children (provider keys pass through) + 64 MiB stream limit |
| `opencode_parser.py` | Pure `--format json` event parsing (no I/O; fixture-tested) |
| `run_executor.py` | Spawns `opencode run --format json` per run, drives to terminal state |
| `run_store.py` | SQLite `runs`/`run_events`, status enum |
| `opencode_watch.py` (`session_watch.py`) | Live-session snapshot: states, voice names, primary badge |
| `session_steer.py` | Message into a live session via supervised `opencode serve` loopback HTTP |
| `dialog.py` | Guarded keypress into a session console (closed vocabulary, identity-checked) |
| `actions.py` | Terminal / browser / editor opening on Windows |
| `screen.py` | Window list + one deliberate screenshot (user-turn-only) |
| `notifier.py` | Toast fallback when no tab is listening (never raises) |
| `preflight.py` | Startup environment checks (observe-only, never raise) |
| `jarvis_mcp.py` | Stdio MCP server → `POST /internal/tool` |
| `speech.py`, `tts.py` | Utterance scheduler, Fish Audio synthesis |
| `builds.py`, `specs.py`, `projects_view.py` | Phased builds, review surface, Projects tab |
| `jarvis_memory.py` | Markdown long-term memory |
| `usage_store.py` | Provider/subscription usage observations |
| `data_paths.py` | Data-dir, brain home, persona/connections sync, tool token |
| `web_auth.py` | Origin/Host gate |
| `browser.py` | Playwright reads (headless live; headful research test-only) |
| `frontend/src/` | Orb (`orb.ts`), voice (`voice.ts`), state machine (`main.ts`), `dashboard/` |
| `jarvis_home/` | Shipped `CLAUDE.md` persona + `connections.json` templates |

## 6. Development

- Run the suite: `pytest` (whole suite minus `-m browser`; no network/screen).
  Browser-live tests: `pytest -m browser` (self-skip without network).
  Windows-only tests: `pytest -m windows`.
- Fakes sit at the subprocess seams (`opencode`, PowerShell, Win32 capture);
  never invoke a real binary, window, or keypress from a unit test.
- Dashboard rule: no `innerHTML`/`insertAdjacentHTML` — `createElement`/
  `textContent` only (it renders LLM/file content).
- Persona/config sync: editing `jarvis_home/CLAUDE.md` requires appending the
  new sha256 to `KNOWN_TEMPLATE_HASHES` (a test walks git history and prints
  the line); same for `connections.json`. Never overwrite a user's edited
  brain-home copies — warn with both paths.
- Conventions: British butler voice, ≤2 sentences spoken; preflight stays
  read-only; tool handlers validate-stage-return within 20 s.

## 7. Troubleshooting (Windows)

| Symptom | Likely cause | Fix |
|---|---|---|
| UI loads, every `/api` call 500s | Backend running plain HTTP (no `cert.pem`/`key.pem`) while Vite proxies to `https://localhost:8340` | Generate certs (§2.7), restart backend |
| Brain `failed`, auth wording in logs | `opencode auth login` missing/expired, or provider key absent | `opencode auth login`; check `auth.json`; set provider key; restart |
| Steer "held for approval / dropped" | Target not accepting inbound steering | Accept the equivalent in opencode config **only after the user agrees** (never auto-written) |
| `answer_dialog` → `not_found` | Session console not owned by Windows Terminal / HWND moved | Bring the session into Windows Terminal; retry. `not_found` (press nothing) is correct behavior |
| `answer_dialog` → `not_permitted` | UIPI/integrity-level refusal | Run both from the same integrity level; check UAC/elevation mismatch |
| Screenshot blank / "couldn't get a picture" | Capture raced a locked/minimized desktop | Retry on a visible desktop; check logs for the blank-frame verdict |
| Toast never appears | Focus Assist / notification settings | Check Windows notification settings; toast is best-effort (`False` ≠ crash) |
| Mic hears nothing | Wrong browser or permission | Use Chrome/Edge, allow mic, click to enable audio; try `JARVIS_MUTE_MIC_DURING_SPEECH=true` for echo |
| `opencode` not found | PATH / install scope | Reinstall per-user, reopen the terminal, `where opencode` |

## 8. Porting notes (what changed from macOS + Claude Code)

Billing: subscription-login scrub (`ANTHROPIC_*` hidden) → provider keys
passed through + `auth.json`. Sessions: `~/.claude` roster + `*.jsonl`
tails → `opencode session list --format json` + opencode storage JSON.
Steering: inbox Unix socket → supervised `opencode serve` loopback HTTP.
Events: `claude stream-json` → `opencode run --format json` (new parser,
same executor semantics). OS: AppleScript/`screencapture`/`sips`/Keychain →
PowerShell + Win32 `ctypes` (terminal/browser/editor, HWND-verified
keypress, `EnumWindows` + `BitBlt` capture, toast, probes). Model flag:
`sonnet` → `provider/model`. Everything else — voice flow, run invariants,
naming/primary logic, memory layout, web gate, dashboard rules — is
deliberately unchanged.
