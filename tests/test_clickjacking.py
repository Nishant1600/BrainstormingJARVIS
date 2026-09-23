"""OriginGuard's premise is that `Origin` proves which page made the request.

Framing breaks that premise without forging anything. A hostile page puts
`http://localhost:8340/dashboard` in an iframe, makes it invisible, lays its
own bait over the top, and waits for one click. The click lands on JARVIS's
own button, so the POST that follows is made *by JARVIS's own page*: it
carries `Origin: http://localhost:8340`, which is exactly the origin the
guard is built to admit. One click is `POST /api/restart`,
`POST /api/runs/{id}/retry`, or `DELETE /api/runs/{id}`.

The only defence is to refuse to be framed at all, and that has to be said
in a response header — `X-Frame-Options` for the browsers that still only
read that, `Content-Security-Policy: frame-ancestors 'none'` for the ones
that prefer the modern spelling. Both, on every response: a JSON body
framed with `<object>` is a framed response too, and the read endpoints are
the ones with the conversations in them.

`nosniff` and `Referrer-Policy` ride along in the same middleware. They are
cheap and they close two adjacent holes: a JSON response re-interpreted as
HTML, and a run id or project path leaking into an outbound `Referer`.

Deliberately NOT here: a full CSP. Verified against both source pages
(`frontend/index.html`, `frontend/dashboard.html`) and both built ones
(`frontend/dist/*.html`) — every script is an external `<script
type="module" src=...>`, every style an external stylesheet, and there is
not one `onclick=` between them, so `script-src 'self'` would in fact hold
today. It is still not added: it would only ever apply to the pages served
off the API port (in development the pages come from Vite, which sends none
of these headers), and it would fail closed and silently the first time
anyone inlined a script. `frame-ancestors` is the directive that is doing
security work here.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

DASHBOARD_ORIGIN = "http://localhost:5173"
HOSTILE_ORIGIN = "http://evil.example"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JARVIS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("JARVIS_PORT", "8340")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()
    with TestClient(server.app) as c:
        yield c, server


def _assert_unframeable(response, what):
    assert response.headers.get("x-frame-options") == "DENY", (
        f"{what} can be put in an iframe: no X-Frame-Options "
        f"(headers were {dict(response.headers)})")
    csp = response.headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp, (
        f"{what} has no frame-ancestors directive (CSP was {csp!r})")


# -- the page the clickjack actually needs --------------------------------


def test_the_dashboard_cannot_be_framed(env):
    """The exploit's first step: `<iframe src=.../dashboard>`."""
    c, _ = env
    _assert_unframeable(c.get("/dashboard"), "/dashboard")


def test_the_voice_page_cannot_be_framed(env):
    """`/` holds the mic. Framed and overlaid, a click enables audio."""
    c, _ = env
    _assert_unframeable(c.get("/"), "/")


# -- and every other response, because <object> frames JSON too ------------


@pytest.mark.parametrize("path", ["/api/health", "/api/runs", "/api/usage"])
def test_json_reads_cannot_be_framed_either(env, path):
    c, _ = env
    r = c.get(path)
    assert r.status_code == 200, r.text
    _assert_unframeable(r, path)


def test_a_refusal_carries_the_headers_too(env):
    """The 403 the guard writes itself bypasses the router entirely."""
    c, _ = env
    r = c.post("/api/runs", json={"prompt": "x", "project_path": "/tmp",
                                  "project_name": "t"},
               headers={"Origin": HOSTILE_ORIGIN})
    assert r.status_code == 403
    _assert_unframeable(r, "an OriginGuard refusal")


def test_the_host_refusal_carries_the_headers_too(env):
    """So does the DNS-rebinding refusal, which is written even earlier."""
    c, _ = env
    r = c.get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 403
    _assert_unframeable(r, "a rebinding refusal")


def test_a_404_carries_the_headers_too(env):
    """A header applied per-route is a header somebody forgets."""
    c, _ = env
    r = c.get("/no-such-path-at-all")
    assert r.status_code == 404
    _assert_unframeable(r, "a 404")


def test_a_static_asset_carries_the_headers_too(env, tmp_path):
    """`/assets` is a StaticFiles mount, not a route JARVIS writes."""
    c, server = env
    dist = server.FRONTEND_DIST / "assets"
    if not dist.exists():
        pytest.skip("frontend has not been built")
    asset = next((p for p in sorted(dist.iterdir()) if p.is_file()), None)
    if asset is None:
        pytest.skip("no built assets")
    r = c.get(f"/assets/{asset.name}")
    assert r.status_code == 200
    _assert_unframeable(r, f"/assets/{asset.name}")


# -- the two that ride along ----------------------------------------------


@pytest.mark.parametrize("path", ["/dashboard", "/api/health"])
def test_responses_are_not_content_sniffed(env, path):
    c, _ = env
    assert c.get(path).headers.get("x-content-type-options") == "nosniff"


@pytest.mark.parametrize("path", ["/dashboard", "/api/health"])
def test_no_referrer_leaves_the_machine(env, path):
    """A `Referer` on an outbound request would carry the run id or the
    project path in it."""
    c, _ = env
    assert c.get(path).headers.get("referrer-policy") == "no-referrer"


# -- and the headers must not break what already worked -------------------


def test_the_dashboard_is_still_served(env):
    c, server = env
    if not (server.FRONTEND_DIST / "dashboard.html").exists():
        # A fresh clone that runs `pytest` before `npm run build` — the
        # README's order — has no page to serve yet. Its sibling below
        # skips the same way; this one failed with a 404 instead.
        pytest.skip("frontend has not been built")
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "<title>" in r.text


def test_an_allowed_origin_still_gets_through(env, monkeypatch):
    c, server = env
    calls = []

    async def fake_spawn(*a, **k):
        calls.append(a)
        return "run-id"

    monkeypatch.setattr(server.run_executor_instance, "spawn", fake_spawn)
    r = c.post("/api/runs",
               json={"prompt": "x", "project_path": "/tmp",
                     "project_name": "t"},
               headers={"Origin": DASHBOARD_ORIGIN})
    assert r.status_code == 200, r.text
    assert calls


def test_a_websocket_still_opens(env):
    """The guard wraps `send`; a WebSocket scope has no response start to
    add headers to, and must not be broken by the attempt."""
    c, _ = env
    with c.websocket_connect("/ws/runs",
                             headers={"Origin": DASHBOARD_ORIGIN}) as ws:
        assert ws is not None
