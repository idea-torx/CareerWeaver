"""One runnable check for the MCP stdio loop: handshake, tool list, tool call."""

import io
import json
import os

import pytest

from weaver import mcp


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Never touch the real ./data — it is live, and its env file holds real keys."""
    monkeypatch.setenv("WEAVER_DATA_DIR", str(tmp_path))


def _exchange(*messages: dict) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    mcp.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_handshake_list_and_call():
    replies = _exchange(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # no id -> no reply
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "weaver_stats", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
        {"jsonrpc": "2.0", "id": 5, "method": "bogus/method"},
    )
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5], "notifications must not get a reply"

    assert replies[0]["result"]["protocolVersion"] == "2025-06-18", "echo the client's version"
    assert replies[0]["result"]["capabilities"] == {"tools": {}}

    names = [t["name"] for t in replies[1]["result"]["tools"]]
    assert set(names) == set(mcp.TOOLS)
    for tool in replies[1]["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        for field in tool["inputSchema"]["required"]:
            assert field in tool["inputSchema"]["properties"], f"{tool['name']}: {field} unschemad"

    # A real CLI round trip: stats returns JSON on stdout, captured not leaked.
    assert replies[2]["result"]["isError"] is False
    assert json.loads(replies[2]["result"]["content"][0]["text"]), "stats must return JSON"

    assert replies[3]["error"]["code"] == -32603  # unknown tool
    assert replies[4]["error"]["code"] == -32601  # unknown method


def test_argv_builders():
    build = lambda name, args: mcp.TOOLS[name][3](args)
    assert build("weaver_tailor", {"job": "7", "lens": "pm"}) == [
        "tailor", "graph", "--job", "7", "--lens", "pm"]
    assert build("weaver_jobs_add", {"value": "http://x", "fetch": True})[-1] == "--fetch"
    # The hold flag is not optional — this server never submits.
    assert "--hold" in build("weaver_apply_hold", {"resume_id": 3})


def test_run_cli_restores_the_environment():
    """Every command loads <data-dir>/env into os.environ. In a long-lived server
    that would bleed one call's credentials into the next — and, in the suite,
    into every test that runs after this one."""
    before = dict(os.environ)
    mcp.run_cli(["stats"])
    assert dict(os.environ) == before


def test_data_dir_does_not_depend_on_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("WEAVER_DATA_DIR", str(tmp_path / "explicit"))
    assert mcp.data_dir() == str(tmp_path / "explicit")
    monkeypatch.delenv("WEAVER_DATA_DIR")
    monkeypatch.chdir(tmp_path)
    assert mcp.data_dir() == str(tmp_path / "data")
