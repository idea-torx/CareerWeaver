"""WEAVER_LLM_CMD routes every LLM call through a local CLI, not an endpoint.

Regression: weaver's only provider used to be an OpenAI-compatible HTTP relay,
so a stalling relay stalled the whole apply loop with no way out but swapping
one url for another. A CLI provider needs no key and opens no socket.
"""

from __future__ import annotations

import pathlib
import shlex
import subprocess
import sys

import pytest

from weaver import llm, local_agent

# A stand-in "CLI": echoes a fixed JSON object, and only if it got a prompt.
ECHO = "\n".join(
    [
        "import sys",
        "sys.exit(3) if not sys.stdin.read().strip() else None",
        'print("```json")',
        'print("{\\"ok\\": true}")',
        'print("```")',
    ]
)


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setenv("WEAVER_LLM_CMD", f"{sys.executable} -c {shlex.quote(ECHO)}")
    monkeypatch.delenv("WEAVER_MODEL", raising=False)
    monkeypatch.delenv("WEAVER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_cli_command_is_argv_or_none(cli, monkeypatch):
    assert llm.cli_command()[0] == sys.executable
    monkeypatch.delenv("WEAVER_LLM_CMD")
    assert llm.cli_command() is None


def test_cli_provider_needs_no_key_and_no_model(cli):
    assert llm.provider_name() == "cli"
    assert llm.config_error() is None
    assert llm.model() == ""
    assert llm.describe()["base_url"] is None


def test_complete_json_runs_the_cli(cli):
    assert llm.complete_json("sys", "user") == {"ok": True, "provider": "cli"}


def test_cli_gets_both_halves_of_the_prompt(cli, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["input"] = kw["input"]
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    llm.cli_complete("SYSTEM-HALF", "USER-HALF")
    assert "SYSTEM-HALF" in seen["input"] and "USER-HALF" in seen["input"]


def test_cli_failure_raises_with_the_detail(cli, monkeypatch):
    monkeypatch.setenv("WEAVER_LLM_CMD", f"{sys.executable} -c 'raise SystemExit(9)'")
    with pytest.raises(RuntimeError, match="exited 9"):
        llm.cli_complete("sys", "user")


def test_complete_json_never_raises_on_cli_failure(cli, monkeypatch):
    monkeypatch.setenv("WEAVER_LLM_CMD", f"{sys.executable} -c 'raise SystemExit(9)'")
    assert llm.complete_json("sys", "user")["_fallback"] is True


def test_make_chat_uses_the_cli_and_opens_no_socket(cli):
    def explode(*_a, **_k):  # the HTTP path must not be reached
        raise AssertionError("make_chat opened a socket with WEAVER_LLM_CMD set")

    chat = local_agent.make_chat(urlopen=explode)
    assert chat("sys", "user") == {"ok": True}


def test_make_chat_still_refuses_a_keyless_http_config(monkeypatch):
    monkeypatch.delenv("WEAVER_LLM_CMD", raising=False)
    monkeypatch.delenv("WEAVER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="no LLM key"):
        local_agent.make_chat()


def test_available_covers_both_transports(cli, monkeypatch):
    assert llm.available() is True  # CLI, no key
    monkeypatch.delenv("WEAVER_LLM_CMD")
    assert llm.available() is False
    monkeypatch.setenv("WEAVER_API_KEY", "sk-test")
    assert llm.available() is True


def test_apply_is_not_gated_on_a_key_when_a_cli_is_set(cli):
    """Regression: `weaver apply` refused to start unless WEAVER_API_KEY was set,
    so a CLI-only install could never fill a form."""
    from weaver import cli as weaver_cli

    src = pathlib.Path(weaver_cli.__file__).read_text()
    assert "if not llm.api_key():" not in src
    assert "if not llm.available():" in src
