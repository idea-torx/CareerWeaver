"""Strong completion flags — the three terminal blocks, the ledger-write-on-exit
guarantee, the exit codes, and the macOS ping.

Runs used to fizzle: the window closed and nothing said whether the application
was filled, dead, or waiting on a human. Every started run now ends with one of
three blocks, a row in the ledger, and an exit code a script can branch on.

Offline: the apply engine, the model key and `osascript` are all stubbed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from weaver import cli, completion, db
from weaver import config as cfg_mod
from weaver import llm, local_agent, preflight

GH_1001 = "https://boards.greenhouse.io/webflow/jobs/1001"


# --------------------------------------------------------------- unit: blocks


def test_the_three_headlines() -> None:
    assert completion.headline("held") == "✓ DONE — FILLED + HELD"
    assert completion.headline("submitted") == "✓ DONE — SUBMITTED"
    assert completion.headline("applied") == "✓ DONE — SUBMITTED"  # engine word
    assert completion.headline("failed", "chromium never opened") == (
        "✗ FAILED — chromium never opened"
    )
    assert completion.headline("audit_pending", "hCaptcha on the form") == (
        "⏸ AUDIT_PENDING — hCaptcha on the form"
    )


def test_a_headline_always_says_something() -> None:
    """A reason-less end is still loud — never a bare '✗ FAILED — '."""
    assert completion.headline("failed") == "✗ FAILED — no reason recorded"
    assert completion.headline("audit_pending").endswith("a human has to finish this one")
    # an unknown engine status is a failure, never a quiet success
    assert completion.headline("weird-new-status", "?").startswith("✗ FAILED")


@pytest.mark.parametrize(
    "status, code",
    [
        ("held", 0),
        ("submitted", 0),
        ("applied", 0),
        ("failed", 2),
        ("stopped", 2),
        ("", 2),
        (None, 2),
        ("audit_pending", 3),
    ],
)
def test_exit_codes(status: str | None, code: int) -> None:
    assert completion.exit_code(status) == code


def test_held_block_carries_the_job_and_the_tab() -> None:
    lines = completion.block(
        "held",
        role="Design Engineer",
        org="Ramp",
        reason="held for audit (--hold): form filled",
        where=GH_1001,
        tab="tab of the shared tab-host window (http://127.0.0.1:9333)",
        ledger_id=14,
    )
    assert lines[0] == lines[-1] == completion.RULE
    assert lines[1] == "✓ DONE — FILLED + HELD"
    body = "\n".join(lines)
    assert "job     Design Engineer @ Ramp" in body
    assert "tab of the shared tab-host window (http://127.0.0.1:9333)" in body
    assert "ledger  #14 held" in body
    assert "press send yourself" in body


def test_failed_and_audit_blocks() -> None:
    failed = completion.text("failed", role="Design Engineer", org="Ramp",
                             reason="resume file vanished", ledger_id=9)
    assert "✗ FAILED — resume file vanished" in failed
    assert "ledger  #9 failed" in failed

    pending = completion.text("audit_pending", role="Design Engineer", org="Ramp",
                              reason="Are you a robot?", ledger_id=9)
    assert "⏸ AUDIT_PENDING — Are you a robot?" in pending
    assert "finish this one in the open window" in pending


def test_a_block_stays_a_block() -> None:
    """A multi-line engine reason must not blow the box open."""
    lines = completion.block("failed", reason="line one\nline two\n" + "x" * 400)
    assert len(lines) == 6  # rule, headline, job, ledger, next, rule
    assert all("\n" not in line for line in lines)
    assert "line one line two" in lines[1]


def test_block_says_when_no_row_was_written() -> None:
    assert "ledger  (NOT WRITTEN) failed" in completion.text("failed", reason="x")


# ---------------------------------------------------------- unit: notification


def test_notification_text() -> None:
    assert completion.notification("held", "Design Engineer", "Ramp") == (
        "FILLED + HELD — Design Engineer @ Ramp"
    )
    assert completion.notification("applied", "Design Engineer", "Ramp").startswith("SUBMITTED —")
    assert completion.notification("audit_pending", "X").startswith("AUDIT PENDING —")
    assert completion.notification("failed", "X").startswith("FAILED —")


@pytest.mark.parametrize(
    "flag, visible, expected",
    [(None, True, True), (None, False, False), (True, False, True), (False, True, False)],
)
def test_notify_defaults_on_for_visible_runs(
    flag: bool | None, visible: bool, expected: bool
) -> None:
    assert completion.wants_notification(flag, visible) is expected


def test_notify_shells_out_to_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(completion, "_is_macos", lambda: True)
    monkeypatch.setattr(completion, "_run", lambda cmd: calls.append(cmd))

    assert completion.notify('FILLED + HELD — "Design" Engineer') is True
    assert calls[0][:2] == ["osascript", "-e"]
    script = calls[0][2]
    assert script.startswith("display notification ")
    assert 'with title "weaver"' in script
    assert '\\"Design\\" Engineer' in script  # quotes escaped, AppleScript intact


def test_notify_is_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(completion, "_is_macos", lambda: True)

    def boom(cmd: list[str]) -> None:
        raise OSError("osascript is gone")

    monkeypatch.setattr(completion, "_run", boom)
    assert completion.notify("anything") is False

    monkeypatch.setattr(completion, "_is_macos", lambda: False)
    assert completion.notify("anything") is False


# ------------------------------------------------------------------- cli: runs


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict, str]:
    """(exit code, parsed stdout JSON, stderr) — the block prints to stderr."""
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else {}), captured.err


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    config = cfg_mod.default_config()
    config["profile"] = cfg_mod.normalize_profile(
        {"name": "Sample Person", "email": "sample@example.com"}
    )
    cfg_mod.save_config(data, config)
    return data


def _linked_resume(workspace: Path) -> int:
    """A resume that really is on disk, linked to a real job row."""
    path = workspace / "out" / "r.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Sample Person\n\nDesign Engineer\n", encoding="utf-8")
    conn: sqlite3.Connection = db.init_db(workspace)
    try:
        job_id = db.add_job(conn, GH_1001, "Design Engineer", "Ramp", "posting text", [])
        return db.add_resume(
            conn, lens="design-engineer", job_id=job_id, fmt="md", path=str(path),
            prompt_used="", source_facts=[], structure={"name": "Sample Person"},
            provider="deterministic",
        )
    finally:
        conn.close()


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """No model key check, no network preflight, no osascript — just the CLI's
    end-of-run path."""
    monkeypatch.setattr(llm, "api_key", lambda: "test-key")
    monkeypatch.setattr(preflight, "fetch_questions", lambda url: None)
    monkeypatch.setattr(completion, "_is_macos", lambda: True)
    monkeypatch.setattr(completion, "_run", lambda cmd: None)


def _stub_apply(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> None:
    def fake_apply(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(local_agent, "apply", fake_apply)


def _rows(workspace: Path) -> list[dict[str, Any]]:
    conn = db.connect(workspace)
    try:
        return db.get_applications(conn)
    finally:
        conn.close()


HELD_OUTCOME: dict[str, Any] = {
    "status": local_agent.AUDIT_PENDING,
    "reason": "held for audit (--hold): form filled",
    "audit": {"kind": "hold", "label": "Submit application", "url": GH_1001},
}


def test_held_run_ends_loud_exit_0_and_lands_in_the_ledger(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    engine: None,
) -> None:
    resume_id = _linked_resume(workspace)
    _stub_apply(monkeypatch, HELD_OUTCOME)

    code, payload, err = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--hold", "--visible"
    )

    assert code == 0
    assert payload["status"] == "held" and payload["exit_code"] == 0
    assert "✓ DONE — FILLED + HELD" in err
    assert "Design Engineer @ Ramp" in err  # job title, from the linked job row
    assert "the browser window this run opened" in err
    assert completion.RULE in err

    row = _rows(workspace)[0]
    assert row["status"] == "held"
    assert "✓ DONE — FILLED + HELD" in row["response"]["completion_block"]
    assert f"ledger  #{row['id']} held" in row["response"]["completion_block"]
    assert payload["completion_block"] == row["response"]["completion_block"]


def test_failed_run_ends_loud_with_exit_2(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    engine: None,
) -> None:
    resume_id = _linked_resume(workspace)
    _stub_apply(monkeypatch, RuntimeError("chromium never opened"))

    code, payload, err = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--hold"
    )

    assert code == cli.EXIT_FAILED == 2
    assert payload["ok"] is False and payload["status"] == "failed"
    assert "✗ FAILED — RuntimeError: chromium never opened" in err

    row = _rows(workspace)[0]
    assert row["status"] == "failed"
    assert "✗ FAILED" in row["response"]["completion_block"]


def test_audit_pending_run_ends_loud_with_exit_3(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    engine: None,
) -> None:
    resume_id = _linked_resume(workspace)
    _stub_apply(
        monkeypatch,
        {
            "status": local_agent.AUDIT_PENDING,
            "reason": "hCaptcha wall — a human must clear it",
            "audit": {"kind": "field", "label": "Are you a robot?", "url": GH_1001},
        },
    )

    code, payload, err = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--force"
    )

    assert code == cli.EXIT_AUDIT_PENDING == 3
    assert payload["status"] == "audit_pending"
    assert "⏸ AUDIT_PENDING — hCaptcha wall — a human must clear it" in err
    assert _rows(workspace)[0]["status"] == "audit_pending"


def test_submitted_run_ends_at_exit_0(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    engine: None,
) -> None:
    resume_id = _linked_resume(workspace)
    _stub_apply(
        monkeypatch,
        {"status": "applied", "reason": "submitted", "confirmation_text": "Thanks for applying!"},
    )

    code, _payload, err = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--force"
    )

    assert code == 0
    assert "✓ DONE — SUBMITTED" in err
    assert _rows(workspace)[0]["status"] == "applied"


@pytest.mark.parametrize(
    "boom", [KeyboardInterrupt(), MemoryError("out of memory"), ValueError("engine bug")]
)
def test_a_started_run_can_never_fizzle(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    engine: None, boom: BaseException,
) -> None:
    """The ledger-write-on-exit guarantee: however the engine dies — Ctrl-C, a
    crash, a bug nobody predicted — the row is written and the block is loud."""
    resume_id = _linked_resume(workspace)
    _stub_apply(monkeypatch, boom)

    code, _payload, err = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--force"
    )

    assert code == 2
    assert "✗ FAILED" in err
    rows = _rows(workspace)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert "✗ FAILED" in rows[0]["response"]["completion_block"]
    if isinstance(boom, KeyboardInterrupt):
        assert "interrupted (Ctrl-C)" in rows[0]["response"]["error"]


def test_the_ledger_row_is_written_before_the_block_is_printed(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    engine: None,
) -> None:
    """Order matters: a terminal that dies mid-print must not cost the row."""
    resume_id = _linked_resume(workspace)
    _stub_apply(monkeypatch, HELD_OUTCOME)
    seen: list[str] = []
    real_add = db.add_application

    def spy_add(*args: Any, **kwargs: Any) -> int:
        seen.append("row")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(db, "add_application", spy_add)
    monkeypatch.setattr(cli.completion, "block", _tracking_block(seen))

    run(capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--hold")
    assert seen == ["row", "block"]


def _tracking_block(seen: list[str]):
    real = completion.block

    def tracked(*args: Any, **kwargs: Any) -> list[str]:
        seen.append("block")
        return real(*args, **kwargs)

    return tracked


# --------------------------------------------------------------- cli: --notify


def _notify_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(completion, "_is_macos", lambda: True)
    monkeypatch.setattr(completion, "_run", lambda cmd: calls.append(cmd))
    return calls


@pytest.mark.parametrize(
    "flags, expected",
    [
        (["--visible"], True),   # default on for a run you are watching
        ([], False),             # headless script: stay quiet
        (["--notify"], True),    # explicit on
        (["--visible", "--no-notify"], False),  # explicit off wins
    ],
)
def test_notify_gating_on_a_real_run(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    engine: None, flags: list[str], expected: bool,
) -> None:
    calls = _notify_calls(monkeypatch)
    resume_id = _linked_resume(workspace)
    _stub_apply(monkeypatch, HELD_OUTCOME)

    code, payload, _err = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--hold", *flags
    )

    assert code == 0
    assert bool(calls) is expected
    assert payload["notified"] is expected
    if expected:
        assert "FILLED + HELD — Design Engineer @ Ramp" in calls[0][2]
