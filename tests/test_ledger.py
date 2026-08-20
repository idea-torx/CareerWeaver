"""`weaver ledger` — the application ledger, finder dedupe, and the `held` status.

Offline: the only network touchpoint (`jobs.http_get_json`) is stubbed with the
same canned feeds Lane 1's tests use, and the apply engine is stubbed too — no
browser, no model, no keys.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from weaver import cli, db
from weaver import config as cfg_mod
from weaver import jobs as jobs_mod
from weaver import ledger as ledger_mod
from weaver import llm, local_agent

GH_1001 = "https://boards.greenhouse.io/webflow/jobs/1001"
GH_1002 = "https://boards.greenhouse.io/webflow/jobs/1002"

TARGETS: dict[str, Any] = {
    "target_titles": ["Brand Designer", "Product Designer"],
    "skills": ["Webflow", "Framer", "design systems", "branding"],
    "locations": ["US Remote", "Canada"],
}

GREENHOUSE_FEED: dict[str, Any] = {
    "jobs": [
        {
            "title": "Staff Brand Designer",
            "absolute_url": GH_1001,
            "location": {"name": "Remote — US"},
            "content": "&lt;p&gt;Webflow &amp;amp; Framer; own branding and our design systems.&lt;/p&gt;",
        },
        {
            "title": "Brand Designer",
            "absolute_url": GH_1002,
            "location": {"name": "Canada"},
            "content": "&lt;p&gt;brand identity and design systems work&lt;/p&gt;",
        },
    ]
}


# ------------------------------------------------------------------- unit: urls


@pytest.mark.parametrize(
    "raw, expected",
    [
        (GH_1001, "boards.greenhouse.io/webflow/jobs/1001"),
        (GH_1001 + "/", "boards.greenhouse.io/webflow/jobs/1001"),
        (GH_1001 + "?gh_src=abc&utm_source=x", "boards.greenhouse.io/webflow/jobs/1001"),
        ("HTTPS://Boards.Greenhouse.IO/webflow/jobs/1001", "boards.greenhouse.io/webflow/jobs/1001"),
        ("https://www.jobs.lever.co/acme/111", "jobs.lever.co/acme/111"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_url(raw: str | None, expected: str) -> None:
    assert ledger_mod.normalize_url(raw) == expected


def test_normalize_url_distinguishes_different_postings() -> None:
    assert ledger_mod.normalize_url(GH_1001) != ledger_mod.normalize_url(GH_1002)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (GH_1001, "webflow"),
        ("https://jobs.lever.co/acme/111", "acme"),
        ("https://jobs.ashbyhq.com/linear/abc-123", "linear"),
        ("https://boards.greenhouse.io/embed/job_app?token=1", "job_app"),
        ("https://example.com/careers/42", ""),
        ("", ""),
    ],
)
def test_org_from_url(raw: str, expected: str) -> None:
    assert ledger_mod.org_from_url(raw) == expected


# ------------------------------------------------------------------ unit: held


def test_hold_status_marks_a_held_run() -> None:
    held = {"audit": {"kind": "hold", "note": "form filled — submit blocked (--hold)"}}
    assert ledger_mod.hold_status(held, "audit_pending") == "held"


def test_hold_status_leaves_other_parks_and_statuses_alone() -> None:
    captcha = {"audit": {"kind": "field", "note": "captcha"}}
    assert ledger_mod.hold_status(captcha, "audit_pending") == "audit_pending"
    assert ledger_mod.hold_status({}, "audit_pending") == "audit_pending"
    assert ledger_mod.hold_status(None, "audit_pending") == "audit_pending"
    # A hold audit on a run that did not park cannot rewrite a real outcome.
    assert ledger_mod.hold_status({"audit": {"kind": "hold"}}, "applied") == "applied"
    assert ledger_mod.hold_status({"audit": {"kind": "hold"}}, "failed") == "failed"


# ------------------------------------------------------------------ unit: rows


def _artifact(data_dir: Path) -> str:
    """A resume file that really is on disk — `weaver apply` refuses a row whose
    path has rotted (the Aug-16 stale-path bug), so a CLI apply test needs one."""
    path = data_dir / "out" / "r.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Sample Person\n\nBrand Designer\n", encoding="utf-8")
    return str(path)


def _resume(conn: sqlite3.Connection, path: str = "out/r.md") -> int:
    return db.add_resume(
        conn,
        lens="brand",
        job_id=None,
        fmt="md",
        path=path,
        prompt_used="",
        source_facts=[],
        structure={"name": "Sample Person", "title": "Brand Designer"},
        provider="deterministic",
    )


def _app(conn: sqlite3.Connection, **kwargs: Any) -> int:
    resume_id = kwargs.pop("resume_id", None) or _resume(conn)
    return db.add_application(
        conn,
        resume_id=resume_id,
        job_id=kwargs.pop("job_id", None),
        status=kwargs.pop("status", "applied"),
        payload=kwargs.pop("payload", {}),
        response=kwargs.pop("response", None),
        url=kwargs.pop("url", GH_1001),
        company=kwargs.pop("company", None),
        title=kwargs.pop("title", None),
    )


def test_rows_shape_and_status_vocabulary(conn: sqlite3.Connection) -> None:
    _app(conn, status="applied", company="Webflow", title="Staff Brand Designer",
         response={"reason": "submitted", "confirmation_text": "Thanks!"})
    _app(conn, status="held", url=GH_1002,
         response={"audit": {"kind": "hold", "note": "filled — press send"}})

    entries = ledger_mod.rows(conn)
    assert [e["id"] for e in entries] == sorted((e["id"] for e in entries), reverse=True)
    held, submitted = entries[0], entries[1]

    assert submitted["status"] == "submitted"  # stored "applied", read as submitted
    assert submitted["raw_status"] == "applied"
    assert submitted["org"] == "Webflow"
    assert submitted["role"] == "Staff Brand Designer"
    assert submitted["note"] == "submitted"
    assert submitted["date"] == submitted["created_at"][:10]
    assert submitted["job_url"] == GH_1001

    assert held["status"] == "held"
    assert held["org"] == "webflow"  # no company column -> org slug from the url
    assert held["role"] == ""
    assert held["note"] == "filled — press send"


def test_rows_note_falls_back_through_error_then_trace(conn: sqlite3.Connection) -> None:
    _app(conn, status="failed", response={"error": "driver died"})
    _app(conn, status="stopped", response={"trace": [
        {"n": 1, "action": "click", "note": "opened form"},
        {"n": 2, "action": "stop", "note": "no submit button found"},
    ]})
    _app(conn, status="dry_run", response=None)

    notes = {e["raw_status"]: e["note"] for e in ledger_mod.rows(conn)}
    assert notes["failed"] == "driver died"
    assert notes["stopped"] == "no submit button found"
    assert notes["dry_run"] == ""


def test_rows_limit_and_counts(conn: sqlite3.Connection) -> None:
    for _ in range(3):
        _app(conn, status="applied")
    _app(conn, status="held", url=GH_1002)
    assert len(ledger_mod.rows(conn, limit=2)) == 2
    assert ledger_mod.rows(conn, limit=0) == []
    assert ledger_mod.counts(ledger_mod.rows(conn)) == {"held": 1, "submitted": 3}


def test_empty_and_missing_ledger_are_graceful(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert ledger_mod.rows(conn) == []
    assert ledger_mod.applied_urls(conn) == set()
    assert "no applications yet" in ledger_mod.format_table([])[0]

    # A db that predates the applications table reads as an empty ledger.
    bare = sqlite3.connect(tmp_path / "bare.db")
    bare.row_factory = sqlite3.Row
    assert ledger_mod.rows(bare) == []
    assert ledger_mod.applied_urls(bare) == set()
    bare.close()


def test_format_table_flags_held_rows(conn: sqlite3.Connection) -> None:
    _app(conn, status="held", company="Webflow", title="Staff Brand Designer",
         response={"audit": {"kind": "hold", "note": "filled — press send"}})
    table = "\n".join(ledger_mod.format_table(ledger_mod.rows(conn)))
    assert "org" in table and "status" in table
    assert "Staff Brand Designer" in table
    assert "held" in table
    assert "1 held — filled and waiting for your send" in table


# ---------------------------------------------------------------- unit: dedupe


def test_dedupe_splits_on_normalized_url() -> None:
    postings = [
        {"role": "A", "url": GH_1001 + "?gh_src=x"},
        {"role": "B", "url": GH_1002},
        {"role": "C", "url": ""},
    ]
    applied = {ledger_mod.normalize_url(GH_1001)}
    fresh, excluded = ledger_mod.dedupe(postings, applied)
    assert [j["role"] for j in fresh] == ["B", "C"]
    assert [j["role"] for j in excluded] == ["A"]


def test_applied_urls_covers_every_ledger_row(conn: sqlite3.Connection) -> None:
    _app(conn, status="applied", url=GH_1001 + "?utm_source=x")
    _app(conn, status="dry_run", url=GH_1002)
    _app(conn, status="failed", url=None)
    assert ledger_mod.applied_urls(conn) == {
        ledger_mod.normalize_url(GH_1001),
        ledger_mod.normalize_url(GH_1002),
    }


# ------------------------------------------------------------------------- cli


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code = cli.main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    config = cfg_mod.default_config()
    config["profile"] = cfg_mod.normalize_profile(
        {
            "name": "Sample Person",
            "email": "sample@example.com",
            "target_roles": TARGETS["target_titles"],
            "target_skills": TARGETS["skills"],
        }
    )
    config["find"]["locations"] = TARGETS["locations"]
    config["find"]["orgs"] = {"webflow": "greenhouse"}
    cfg_mod.save_config(data, config)
    return data


def stub_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs_mod, "http_get_json", lambda url, timeout=15.0: GREENHOUSE_FEED)


def test_cli_ledger_empty_db(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run(capsys, "--data-dir", str(workspace), "ledger")
    assert code == 0
    assert payload == {"ok": True, "count": 0, "by_status": {}, "applications": []}


def test_cli_ledger_json_and_human(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(workspace)
    _app(conn, status="applied", company="Webflow", title="Staff Brand Designer",
         response={"reason": "submitted"})
    _app(conn, status="held", url=GH_1002, company="Acme", title="Brand Designer",
         response={"audit": {"kind": "hold", "note": "filled — press send"}})
    conn.close()

    code, payload = run(capsys, "--data-dir", str(workspace), "ledger")
    assert code == 0
    assert payload["count"] == 2
    assert payload["by_status"] == {"held": 1, "submitted": 1}
    assert [row["status"] for row in payload["applications"]] == ["held", "submitted"]
    assert payload["applications"][0]["job_url"] == GH_1002

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert cli.main(["--data-dir", str(workspace), "ledger"]) == 0
    out = capsys.readouterr().out
    assert "Staff Brand Designer" in out and "submitted" in out
    assert "1 held — filled and waiting for your send" in out


def test_cli_ledger_status_and_limit_filters(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = db.init_db(workspace)
    _app(conn, status="applied")
    _app(conn, status="held", url=GH_1002)
    _app(conn, status="failed", url="https://jobs.lever.co/acme/111")
    conn.close()

    code, payload = run(capsys, "--data-dir", str(workspace), "ledger", "--status", "held")
    assert code == 0 and payload["count"] == 1
    assert payload["applications"][0]["job_url"] == GH_1002

    code, payload = run(capsys, "--data-dir", str(workspace), "ledger", "--limit", "2")
    assert code == 0 and payload["count"] == 2

    with pytest.raises(SystemExit):  # argparse rejects an unknown status
        cli.main(["--data-dir", str(workspace), "ledger", "--status", "bogus"])


# ------------------------------------------------------------- cli: find dedupe


def test_cli_find_excludes_already_applied(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_feed(monkeypatch)
    code, before = run(capsys, "--data-dir", str(workspace), "find", "--fit", "0.6")
    assert code == 0
    assert [job["url"] for job in before["jobs"]] == [GH_1001, GH_1002]
    assert before["already_applied"] == 0

    conn = db.init_db(workspace)
    _app(conn, status="applied", url=GH_1001 + "?gh_src=tracking")  # same posting, tracked link
    conn.close()

    code, after = run(capsys, "--data-dir", str(workspace), "find", "--fit", "0.6")
    assert code == 0
    assert [job["url"] for job in after["jobs"]] == [GH_1002]
    assert after["count"] == 1
    assert after["scanned"] == before["scanned"]  # dedupe filters, it does not un-scan
    assert after["already_applied"] == 1
    assert after["already_applied_jobs"] == [
        {"role": "Staff Brand Designer", "org": "webflow", "url": GH_1001}
    ]

    saved = json.loads((workspace / "jobs.json").read_text(encoding="utf-8"))
    assert [job["url"] for job in saved["jobs"]] == [GH_1002]


def test_cli_find_limit_counts_only_fresh_jobs(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_feed(monkeypatch)
    conn = db.init_db(workspace)
    _app(conn, status="applied", url=GH_1001)
    conn.close()

    code, payload = run(
        capsys, "--data-dir", str(workspace), "find", "--fit", "0.6", "--limit", "1"
    )
    assert code == 0
    # --limit 1 yields one FRESH job, not the applied top hit followed by nothing.
    assert [job["url"] for job in payload["jobs"]] == [GH_1002]


def test_cli_find_human_notes_the_exclusion(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_feed(monkeypatch)
    conn = db.init_db(workspace)
    _app(conn, status="applied", url=GH_1001)
    _app(conn, status="held", url=GH_1002)
    conn.close()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert cli.main(["--data-dir", str(workspace), "find", "--fit", "0.6"]) == 0
    out = capsys.readouterr().out
    assert "2 already applied, skipped — see `weaver ledger`" in out
    assert "already in the ledger" in out
    assert GH_1001 not in out


# ------------------------------------------------------------- cli: apply --hold


def test_cli_apply_hold_lands_in_the_ledger_as_held(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(workspace)
    resume_id = _resume(conn, _artifact(workspace))
    conn.close()

    seen: dict[str, Any] = {}

    def fake_apply(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "status": local_agent.AUDIT_PENDING,
            "reason": "held for audit (--hold): form filled",
            "trace": [{"n": 1, "action": "stop", "ok": True, "note": "held for audit"}],
            "audit": {
                "kind": "hold",
                "label": "Submit application",
                "value": "",
                "url": GH_1001,
                "note": "form filled — submit blocked (--hold)",
                "screenshot_b64": "",
            },
        }

    monkeypatch.setattr(local_agent, "apply", fake_apply)
    monkeypatch.setattr(llm, "api_key", lambda: "test-key")

    code, payload = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--hold", "--visible"
    )
    assert code == 0
    assert seen["hold"] is True and seen["headless"] is False
    assert payload["status"] == "held"

    code, ledger_payload = run(capsys, "--data-dir", str(workspace), "ledger")
    assert code == 0
    row = ledger_payload["applications"][0]
    assert row["id"] == payload["application_id"]
    assert row["status"] == "held" and row["raw_status"] == "held"
    assert row["note"] == "held for audit (--hold): form filled"


def test_cli_apply_audit_pending_stays_audit_pending(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(workspace)
    resume_id = _resume(conn, _artifact(workspace))
    conn.close()

    monkeypatch.setattr(
        local_agent,
        "apply",
        lambda payload, **kwargs: {
            "status": local_agent.AUDIT_PENDING,
            "reason": "anti-bot wall",
            "audit": {"kind": "field", "label": "Are you a robot?", "url": GH_1001},
        },
    )
    monkeypatch.setattr(llm, "api_key", lambda: "test-key")

    code, payload = run(capsys, "--data-dir", str(workspace), "apply", str(resume_id))
    assert code == cli.EXIT_AUDIT_PENDING  # 3 — a human still has to finish it
    assert payload["status"] == "audit_pending"

    _code, ledger_payload = run(capsys, "--data-dir", str(workspace), "ledger")
    assert ledger_payload["applications"][0]["status"] == "audit_pending"
    assert ledger_payload["by_status"] == {"audit_pending": 1}


def test_applied_run_is_deduped_by_the_finder_end_to_end(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held run is still an application: the finder must not re-surface it."""
    conn = db.init_db(workspace)
    job_id = db.add_job(conn, GH_1001, "Staff Brand Designer", "webflow", "posting", [])
    resume_id = db.add_resume(
        conn, lens="brand", job_id=job_id, fmt="md", path=_artifact(workspace), prompt_used="",
        source_facts=[], structure={"name": "Sample Person"}, provider="deterministic",
    )
    conn.close()

    monkeypatch.setattr(
        local_agent,
        "apply",
        lambda payload, **kwargs: {
            "status": local_agent.AUDIT_PENDING,
            "reason": "held",
            "audit": {"kind": "hold", "url": GH_1001},
        },
    )
    monkeypatch.setattr(llm, "api_key", lambda: "test-key")
    code, _payload = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--hold", "--force"
    )
    assert code == 0

    stub_feed(monkeypatch)
    code, found = run(capsys, "--data-dir", str(workspace), "find", "--fit", "0.6")
    assert code == 0
    assert found["already_applied"] == 1
    assert [job["url"] for job in found["jobs"]] == [GH_1002]
