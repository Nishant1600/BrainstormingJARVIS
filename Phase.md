# JARVIS — Phases: Windows + OpenCode port

Execution order for `Plan.md`. Each phase ends with a verifiable exit gate —
do not start the next phase until the gate is green. Work on a
`windows-opencode` branch; keep `pytest` green throughout.

Conventions: `JARVIS_RUNTIME=opencode|claude` is the cutover switch
(`claude` = old behavior until Phase 6). Platform-only tests use
`@pytest.mark.windows` / `pytest.mark.skipif(sys.platform != "win32")`,
mirroring the existing `browser` mark in `pytest.ini`.

## Phase 0 — Baseline and pins (no behavior change)

**Goal:** a reproducible starting point and frozen opencode facts.

Tasks:

1. `git status` clean; create branch `windows-opencode`; record baseline
   `pytest` output.
2. Install OpenCode on Windows; record:
   `opencode --version`, `opencode run --help`, `opencode session --help`,
   `opencode serve --help`, `opencode mcp --help`, `opencode auth --help`.
3. Confirm storage/auth paths: `%USERPROFILE%\.local\share\opencode\`
   (`storage/`, `auth.json`, `log/`), config
   `%USERPROFILE%\.config\opencode\opencode.json(c)`, project-local storage.
4. Capture reference transcripts: `opencode run --format json "say OK"`,
   an error case, and `opencode session list --format json` with 2 sessions.
   Save raw outputs under `tests/fixtures/opencode/` (new dir).
5. Freeze constants: `MIN_OPENCODE_VERSION`, model string
   (`JARVIS_BRAIN_MODEL=<provider/model>`), permission-bypass spelling.

**Gate:** fixtures committed; version/flags/paths written into
`Architecture.md §5`-style notes (update that file if reality differs);
baseline `pytest` log saved.

## Phase 1 — Foundations: env, tokens, config plumbing

**Goal:** the new runtime can be selected without breaking the old one.

Tasks:

1. Add `opencode_env.py`: provider-key pass-through, `STREAM_LINE_LIMIT`
   re-export, `child_env()` unit-tested on win32 + posix.
2. Add `JARVIS_OPENCODE_PATH` (+ deprecated `JARVIS_CLAUDE_PATH` alias),
   `JARVIS_RUNTIME` switch read in `server.py`/`brain.py`/`run_executor.py`.
3. Add `_write_opencode_config()` (`mcp.servers.jarvis` local stdio with
   tool URL + token env) beside the existing writer; keep serving the old
   file when `JARVIS_RUNTIME=claude`.
4. Harden `data_paths.ensure_tool_token` for Windows (no `getuid`/
   `O_NOFOLLOW`; exclusive-create + best-effort ACL tightening).
   Unit-test POSIX and Windows branches (monkeypatch `os.getuid` absence).
5. Refresh `.env.example` + `skills/jarvis-setup/SKILL.md` (opencode install,
   `opencode auth login`, Fish key, Playwright, certs note).

**Gate:** `pytest -q` green with `JARVIS_RUNTIME` unset and set to both
values; `import opencode_env, data_paths` clean on Windows; old
`claude_env` tests untouched and passing.

## Phase 2 — Runs on opencode (first working agent loop)

**Goal:** recorded runs spawn via opencode and always terminate.

Tasks:

1. Build `opencode_parser.py` from Phase 0 fixtures: `parse_line`,
   `event_kind`, `extract_init_metadata`, `extract_result_metrics`,
   `extract_assistant_usage`, `summarize_result` (same names as
   `stream_parser.py`). Unknown events → preserved verbatim.
2. Add `tests/test_opencode_parser.py` (fixture-driven, incl. malformed
   lines, non-dict JSON, missing usage keys — mirror stream_parser tests).
3. Retarget `run_executor.py` command builder to
   `opencode run --format json --model ...` when `JARVIS_RUNTIME=opencode`;
   keep `_EVENT_BATCH`, `_EVENT_FLUSH_SEC`, stderr drain, `_EOF_EXIT_GRACE`,
   `_IDLE_OUTPUT_SEC`, wall-clock bounds, permit pool, cancel paths.
4. Map `JARVIS_SKIP_PERMISSIONS` to the opencode unattended-approval mode
   (verified spelling); never leave a run on an interactive prompt.
5. Manual: `spawn_run` on a scratch project → terminal state in DB →
   visible at `/api/runs` + `/dashboard` + `/ws/runs` hint.

**Gate:** old + new parser tests green; forced timeout/cancel/spawn-failure
runs each land in a terminal state; dashboard shows one opencode run live.

## Phase 3 — Brain on opencode (voice loop back)

**Goal:** the long-lived voice brain runs on opencode.

Tasks:

1. Rework `brain.py:command()` for opencode argv (or serve+attach);
   keep `BrainConfig.from_env` shape, adding `opencode_path`/`attach_url`.
2. Rewire turn I/O to the opencode session protocol; keep `_Turn`,
   `on_delta`/`on_tool`, `turn_timeout`, stuck-turn restart, stdin-failure
   handling adapted to child-death handling.
3. Re-derive `baseline_tokens`/`conversation_tokens`/rotation from opencode
   usage events; keep budget constants and `rotate()` fallback semantics.
4. Port `launch_prompt` (time, `plain_phrase(USER_NAME)`, taint sentence,
   wrapped handover ≤1200 chars, ≤12 boot projects) to the opencode
   operator-prompt slot.
5. Reclassify fatal auth for opencode; keep `max_restarts`/`restart_window`
   and `ready/failed` emissions.
6. Manual: cold boot → warm-up `OK` → typed turn → mic turn → forced rotation.

**Gate:** `tests/test_brain*` (adapted) green; three consecutive voice turns
with a web/file read correctly refuse unsupervised action (taint gate live);
rotation with injected failure keeps the old generation serving.

## Phase 4 — Sessions on opencode (watch + steer)

**Goal:** "what's running / nudge it" works without `~/.claude`.

Tasks:

1. Replace roster + `*.jsonl` tailing with `opencode session list
   --format json` + storage-JSON reader (new `opencode_watch.py` or
   runtime-switched `session_watch.py`). Drop `encode_cwd`,
   `transcript_path`, `tail_objects`, `CLAUDE_CONFIG_DIR` on the opencode path.
2. Keep `SessionState`, states, voice naming, `_mark_primary`, `resolve`,
   `Snapshot`, `GONE_RETENTION_SEC`/`MIN_WORK_SEC`; repoint inputs to opencode
   fields. Replace `pid_alive` with opencode-state liveness.
3. Replace `session_steer.py` socket send with loopback HTTP against a
   JARVIS-supervised `opencode serve`; document the endpoint after verifying
   the local server docs; keep `sent/not_live/refused/failed` + staged flow.
4. Update `jarvis_mcp.py` tool descriptions for opencode sessions; keep names
   + 20 s staging rule.
5. Manual: 2 sessions → correct names/states; steer delivered post-read-back;
   `answer_dialog`-class prompt correctly refused by steer.

**Gate:** watcher/steer tests green (faked `opencode session list` +
   storage JSON + fake serve); live two-session + steer round-trip passes.

## Phase 5 — Windows OS layer (actions/dialog/screen/notify/preflight)

**Goal:** the machine-facing tools obey their safety contracts on Windows.

Tasks:

1. `actions_win.py` (terminal/browser/editor/tab-info) behind the same
   `server.tool_open_in_*` handlers; argv-only quoting; Windows-quote tests.
2. `dialog_win.py` (HWND identity + closed vocabulary + foreground
   save/restore + six outcomes). Test `normalize_key`/`normalize_tty`-equiv
   + outcome matrix with fakes; one live scratch-console press only.
3. `screen_win.py` (`EnumWindows` + capture + resize + blank check; same
   dataclasses/caps/temp-dir discipline; `ACTING_TOOLS` gating unchanged).
4. `notifier_win.py` (toast from argv data; truncation/timeout/never-raise).
5. `preflight.py` opencode probes (version/login/terminal-automation/screen
   informational/provider-key/config-steer). Keep concurrent never-raise
   structure + silent-when-healthy summary.
6. `browser.py` UA → Windows token; cert docs for Windows (openssl from
   Git-for-Windows → `mkcert` → fallback); `DESKTOP_PATH`/projects-root check.

**Gate:** new `test_*_win` suites green; manual matrix (open terminal,
browser, editor; list windows; screenshot; toast; scratch-console keypress;
preflight all-ok silent + each failure spoken) signed off.

## Phase 6 — Cutover, docs, cleanup

**Goal:** opencode-on-Windows is the default; the repo reads as one project.

Tasks:

1. Flip default `JARVIS_RUNTIME` to `opencode`; keep `claude` path only as a
   deprecated fallback for one release (or remove if the team agrees —
   record the decision).
2. Update `README.md`, `CLAUDE.md`, `.env.example`,
   `skills/jarvis-setup/SKILL.md`, `jarvis_home/CLAUDE.md` (+ append new
   template hashes to `KNOWN_TEMPLATE_HASHES`), `frontend` proxy/cert notes.
3. `pytest.ini` marks for `windows`; README test commands
   (`pytest`, `pytest -m browser`, `pytest -m windows` as applicable).
4. Full pass: cold boot → mic turn → spawn_run → steer → dialog (scratch) →
   screen read → dashboard watch → restart; `pytest` bare green on Windows.
5. Fill `project_documentation.md` gaps found during the pass; fix drift in
   `Architecture.md`/`Plan.md`.

**Gate (definition of done):** all `Plan.md §4` boxes ticked; bare `pytest`
green on a clean Windows clone following only `project_documentation.md`;
`git diff main...windows-opencode` reviewed; branch merged.
