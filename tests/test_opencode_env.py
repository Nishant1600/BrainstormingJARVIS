"""opencode_env: provider credentials pass through to OpenCode children.

The inverse of the claude_env contract (see test_brain.py's
test_child_env_never_carries_api_credentials): scrubbing ANTHROPIC_* would
break the OpenCode brain, whose provider auth legitimately lives in the
environment alongside auth.json.
"""

import opencode_env


def test_provider_keys_pass_through(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-keep-me")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://user-gateway.example")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-keep-me")
    monkeypatch.setenv("OPENCODE_CONFIG", "/user/config.json")
    monkeypatch.setenv("FISH_API_KEY", "keep")
    env = opencode_env.child_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-keep-me"
    assert env["ANTHROPIC_BASE_URL"] == "https://user-gateway.example"
    assert env["OPENAI_API_KEY"] == "sk-openai-keep-me"
    assert env["OPENCODE_CONFIG"] == "/user/config.json"
    assert env["FISH_API_KEY"] == "keep"


def test_child_env_returns_a_copy_not_a_view(monkeypatch):
    import os
    env = opencode_env.child_env()
    assert env == dict(os.environ)
    assert env is not os.environ


def test_child_env_honours_an_explicit_base():
    base = {"KEEP": "yes", "DROP_ME_X": "no"}
    out = opencode_env.child_env(base)
    assert out == dict(base)


def test_stream_line_limit_matches_claude_env():
    import claude_env
    assert opencode_env.STREAM_LINE_LIMIT == claude_env.STREAM_LINE_LIMIT
