"""The environment every OpenCode child of JARVIS is given.

Companion to `claude_env.py` for the Windows + OpenCode runtime
(see Architecture.md §3.7). The billing rule here is the INVERSE of the
Claude Code one:

* `claude_env` scrubs every `ANTHROPIC_*` variable because the Claude Code
  CLI silently prefers an inherited API key over the subscription login —
  a key in the environment moves the child onto paid API billing.
* OpenCode authenticates through provider keys in the environment plus
  `auth.json` (`opencode auth login`). A provider key in the environment is
  a SUPPORTED credential, not a silent redirect — scrubbing `ANTHROPIC_*`
  here would break the brain instead of protecting it.

So `child_env()` passes provider keys (`ANTHROPIC_*`, `OPENAI_*`, ...),
`OPENCODE_*`, and everything else through untouched. The scrub lists below
exist as a seam for variables that would redirect the binary itself or
leak test isolation; they default to empty and any addition must name the
exact redirect it prevents.

`STREAM_LINE_LIMIT` is re-exported from the same reasoning as
`claude_env.STREAM_LINE_LIMIT`: `opencode run --format json` emits one JSON
object per line, and a single line carrying a large tool result routinely
exceeds asyncio's default 64 KiB line buffer — which raises
`ValueError("Separator is not found, and chunk exceed the limit")` and
kills an otherwise-healthy run or brain. Pass `limit=` to every
`create_subprocess_exec(..., limit=...)` that reads an OpenCode child's
stdout/stderr.
"""

from __future__ import annotations

import os

# Variables that would redirect an OpenCode child away from the user's own
# configuration if inherited. Empty by default: nothing in the environment
# silently re-bills or redirects an opencode child the way ANTHROPIC_* did
# a `claude` child, so there is nothing to scrub until a concrete redirect
# is measured. Additions go here with a comment naming the redirect.
SCRUBBED_ENV_PREFIXES: tuple[str, ...] = ()
SCRUBBED_ENV_KEYS: frozenset[str] = frozenset()

# Same ceiling as claude_env.STREAM_LINE_LIMIT (see module docstring): a
# buffer ceiling, not a pre-allocation. 64 MiB comfortably covers the worst
# single-line tool results observed (low single-digit MiB) while bounding
# a runaway line.
STREAM_LINE_LIMIT = 64 * 1024 * 1024  # 64 MiB per line


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment for an OpenCode child.

    Provider credentials and `OPENCODE_*` pass through untouched; only the
    (currently empty) scrub lists above are removed. `PATH`, `HOME`,
    `USERPROFILE`, `OPENCODE_CONFIG`, and the user's own variables all
    survive — the child must see exactly the configuration the user set up
    with `opencode auth login`.
    """
    source = os.environ if base is None else base
    return {k: v for k, v in source.items()
            if not k.startswith(SCRUBBED_ENV_PREFIXES)
            and k not in SCRUBBED_ENV_KEYS}
