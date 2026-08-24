"""CLI smoke: every command in the brief's surface, JSON mode, exit codes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from weaver import cli, db


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    """Run a command in JSON mode and return (exit_code, parsed_stdout)."""
    code = cli.main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


@pytest.fixture(autouse=True)
def _no_real_tab_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch/cycle ensure the shared window before the first fill — tests must
    never spawn a real chromium. Tab-host-down tests override this stub."""
    monkeypatch.setattr(cli, "_ensure_tab_host", lambda _d: "cdp://stub")
    monkeypatch.setattr(cli, "_ensure_real_chrome", lambda _d: "cdp://real-chrome-stub")


@pytest.fixture
def workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str], samples_dir: Path) -> Path:
    data = tmp_path / "data"
    code, _ = run(capsys, "--data-dir", str(data), "init")
    assert code == 0
    code, _ = run(capsys, "--data-dir", str(data), "seed-import", "--dir", str(samples_dir))
    assert code == 0
    return data


def test_init_creates_db_config_and_lenses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    code, payload = run(capsys, "--data-dir", str(data), "init")

    assert code == 0
    assert payload["ok"] is True
    assert (data / "weaver.db").exists()
    assert (data / "config.json").exists()
    assert set(payload["lenses"]) == {
        "fde",
        "fdc",
        "design-engineer",
        "multimedia",
        "creative",
        "sre",
    }
    assert payload["llm"]["provider"] == "deterministic"
    assert payload["llm"]["key_present"] is False

    code, again = run(capsys, "--data-dir", str(data), "init")
    assert code == 0 and again["already_existed"] is True


def test_seed_import_from_samples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], samples_dir: Path
) -> None:
    data = tmp_path / "data"
    run(capsys, "--data-dir", str(data), "init")
    code, payload = run(capsys, "--data-dir", str(data), "seed-import", "--dir", str(samples_dir))

    assert code == 0
    assert payload["errors"] == []
    assert payload["graph"]["facts_total"] > 20
    assert payload["profile"]["name"] == "Mira Halloway"
    assert any(s["file"] == "sample-job.txt" for s in payload["skipped"])


def test_seed_import_missing_dir_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    run(capsys, "--data-dir", str(data), "init")
    code, payload = run(capsys, "--data-dir", str(data), "seed-import", "--dir", str(tmp_path / "nope"))

    assert code == cli.EXIT_USAGE
    assert payload["ok"] is False


def test_lens_list_show_create(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run(capsys, "--data-dir", str(workspace), "lens", "list")
    assert code == 0 and payload["count"] == 6

    code, payload = run(capsys, "--data-dir", str(workspace), "lens", "show", "multimedia")
    assert code == 0
    assert payload["lens"]["lead_domains"][0] == "video_multimedia"

    code, payload = run(capsys, "--data-dir", str(workspace), "lens", "show", "nope")
    assert code == cli.EXIT_USAGE

    code, payload = run(
        capsys,
        "--data-dir",
        str(workspace),
        "lens",
        "create",
        "--name",
        "platform",
        "--lead-domains",
        "sre_cloud,fullstack_engineering",
        "--titles",
        "Platform Engineer;Infrastructure Engineer",
    )
    assert code == 0
    assert payload["lens"]["target_titles"] == ["Platform Engineer", "Infrastructure Engineer"]

    code, payload = run(
        capsys,
        "--data-dir",
        str(workspace),
        "lens",
        "create",
        "--name",
        "bogus",
        "--lead-domains",
        "not_a_domain",
    )
    assert code == cli.EXIT_USAGE
    assert "unknown domain" in payload["error"]


def test_tailor_writes_a_resume_and_reports_json(
    workspace: Path, capsys: pytest.CaptureFixture[str], sample_resume: Path
) -> None:
    code, payload = run(
        capsys,
        "--data-dir",
        str(workspace),
        "tailor",
        str(sample_resume),
        "--lens",
        "fde",
    )

    assert code == 0
    assert payload["provider"] == "deterministic"
    assert payload["unverified_mentions"] == []
    assert payload["structure"]["title"] == "Forward Deployed AI Engineer"
    assert payload["source_facts"]
    assert Path(payload["path"]).exists()
    assert Path(payload["path"]).read_text(encoding="utf-8").startswith("# Mira Halloway")


def test_tailor_docx_and_explicit_out(
    workspace: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "custom.docx"
    code, payload = run(
        capsys,
        "--data-dir",
        str(workspace),
        "tailor",
        "graph",
        "--lens",
        "multimedia",
        "--format",
        "docx",
        "--out",
        str(out),
    )

    assert code == 0
    assert payload["format"] == "docx"
    assert out.exists() and out.stat().st_size > 0


def test_tailor_requires_a_lens_or_job(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run(capsys, "--data-dir", str(workspace), "tailor", "graph")
    assert code == cli.EXIT_USAGE
    assert "--lens" in payload["error"]


def test_tailor_with_job_picks_a_lens(
    workspace: Path, capsys: pytest.CaptureFixture[str], sample_job: Path
) -> None:
    code, payload = run(
        capsys, "--data-dir", str(workspace), "tailor", "graph", "--job", str(sample_job)
    )

    assert code == 0
    assert payload["lens"] in {"fde", "sre", "design-engineer", "fdc", "multimedia", "creative"}
    assert payload["job_id"] is not None
    assert payload["unverified_mentions"] == []


def test_jobs_add_and_list(
    workspace: Path, capsys: pytest.CaptureFixture[str], sample_job: Path
) -> None:
    code, payload = run(capsys, "--data-dir", str(workspace), "jobs", "add", str(sample_job))
    assert code == 0
    assert payload["job"]["title"] == "Forward Deployed AI Engineer"
    assert payload["job"]["company"] == "Verdant Systems"
    assert payload["job"]["raw_text_chars"] > 100

    code, payload = run(capsys, "--data-dir", str(workspace), "jobs", "list")
    assert code == 0 and payload["count"] == 1

    code, payload = run(
        capsys, "--data-dir", str(workspace), "jobs", "add", "https://example.test/jobs/42"
    )
    assert code == 0
    assert payload["job"]["url"] == "https://example.test/jobs/42"
    assert payload["job"]["raw_text_chars"] == 0  # no network without --fetch


def test_stats(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run(capsys, "--data-dir", str(workspace), "stats")

    assert code == 0
    assert payload["facts_total"] > 20
    assert payload["lenses"] == 6
    assert payload["facts_by_kind"]["role"] == 5
    assert payload["llm"]["provider"] == "deterministic"


def test_json_is_automatic_when_piped(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """capsys stdout is not a tty, so plain `weaver stats` must emit JSON."""
    code = cli.main(["--data-dir", str(workspace), "stats"])
    out = capsys.readouterr().out

    assert code == 0
    assert json.loads(out)["ok"] is True


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main([])
    assert code == cli.EXIT_USAGE
    assert "usage: weaver" in capsys.readouterr().out


def test_apply_dry_run_records_an_application(
    workspace: Path, capsys: pytest.CaptureFixture[str], sample_job: Path
) -> None:
    code, tailored = run(
        capsys,
        "--data-dir",
        str(workspace),
        "tailor",
        "graph",
        "--lens",
        "fde",
        "--job",
        str(sample_job),
    )
    assert code == 0
    resume_id = tailored["resume_id"]

    code, payload = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--dry-run"
    )

    assert code == 0
    assert payload["dry_run"] is True
    assert payload["network_call"] is False
    assert "llm_key_present" in payload  # env dependent — the key is presence, not value

    request = payload["request"]
    assert payload["provider"] == "local"
    applicant = request["applicant"]
    assert applicant["full_name"] == "Mira Halloway"
    assert applicant["email"] == "mira@halloway.example"
    assert applicant["work_experience"]
    assert request["resume_filename"].endswith(".md")

    conn = db.connect(workspace)
    try:
        rows = db.get_applications(conn)
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "dry_run"
    assert rows[0]["resume_id"] == resume_id
    assert rows[0]["id"] == payload["application_id"]


def test_apps_list_and_show(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, tailored = run(capsys, "--data-dir", str(workspace), "tailor", "graph", "--lens", "sre")
    assert code == 0
    run(capsys, "--data-dir", str(workspace), "apply", str(tailored["resume_id"]), "--dry-run")

    code, payload = run(capsys, "--data-dir", str(workspace), "apps", "list")
    assert code == 0
    assert payload["count"] == 1
    entry = payload["applications"][0]
    assert entry["status"] == "dry_run"
    assert entry["lens"] == "sre"
    assert entry["resume_path"].endswith(".md")

    code, payload = run(capsys, "--data-dir", str(workspace), "apps", "show", str(entry["id"]))
    assert code == 0
    assert "applicant" in payload["application"]["payload"]

    code, payload = run(capsys, "--data-dir", str(workspace), "apps", "show", "999")
    assert code == cli.EXIT_USAGE


def test_apply_without_key_refuses_real_submit(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WEAVER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code, tailored = run(capsys, "--data-dir", str(workspace), "tailor", "graph", "--lens", "fde")
    code, payload = run(capsys, "--data-dir", str(workspace), "apply", str(tailored["resume_id"]))

    assert code == cli.EXIT_USAGE
    assert "WEAVER_API_KEY" in payload["error"]

    conn = db.connect(workspace)
    try:
        assert db.get_applications(conn) == []
    finally:
        conn.close()


def test_apply_unknown_resume(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run(capsys, "--data-dir", str(workspace), "apply", "404", "--dry-run")
    assert code == cli.EXIT_USAGE
    assert payload["ok"] is False


def test_declared_portfolio_beats_the_link_heuristic() -> None:
    """RUN 83 offered a dribbble profile for "Portfolio" while the applicant's
    own site sat unlisted. An explicit profile.portfolio always wins; without
    one, portfolio-platform hosts (dribbble/behance) no longer masquerade as
    the personal site."""
    from weaver import payload as payload_lib

    links = ["github.com/example", "dribbble.com/example", "example-site.com"]
    explicit = payload_lib.applicant_from_profile({"portfolio": "example-site.com"}, links)
    assert explicit["portfolio_url"] == "example-site.com"

    heuristic = payload_lib.applicant_from_profile({}, links)
    assert heuristic["portfolio_url"] == "example-site.com"  # dribbble skipped


def test_the_apply_payload_honors_the_declared_portfolio(tmp_path) -> None:
    """RUN 84 (critical): `build_payload` re-ran the bare link heuristic over
    the RESUME's contact links and silently overwrote the declared portfolio
    with a side-project domain. The declared portfolio must survive the full
    payload path, and profile links must reach `websites` even when the
    tailored resume omits them."""
    from weaver import db as weaver_db
    from weaver import payload as payload_lib

    conn = weaver_db.init_db(tmp_path)
    profile = {
        "name": "Mira Halloway",
        "portfolio": "mira-portfolio.example",
        "links": ["github.com/mira", "mira-portfolio.example", "dribbble.com/mira"],
    }
    resume = {
        "structure": {
            "name": "Mira Halloway",
            # the tailored resume prints only these — no portfolio site
            "contact": {"links": ["github.com/mira", "sideproject.example"]},
        }
    }

    payload = payload_lib.build_payload(conn, resume, profile)

    applicant = payload["parameters"]["applicant"]
    assert applicant["portfolio_url"] == "mira-portfolio.example"
    assert "mira-portfolio.example" in applicant["websites"]


def test_tailor_refuses_auto_lens_on_a_textless_job(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Morning-of-Aug-16 regression: jobs added without --fetch have no posting
    text; the lens auto-pick silently defaulted to `creative` and produced
    Creative Director resumes for engineering roles. Blind guessing is refused."""
    code, added = run(
        capsys, "--data-dir", str(workspace), "jobs", "add", "https://example.test/careers/42"
    )
    assert code == 0
    assert "no posting text" in (added.get("warning") or "")

    job_id = added["job"]["id"]
    code, payload = run(
        capsys, "--data-dir", str(workspace), "tailor", "graph", "--job", str(job_id)
    )
    assert code == cli.EXIT_USAGE
    assert "no posting text" in payload["error"]
    assert "--fetch" in payload["error"]

    # an explicit lens still works on the same textless job
    code, tailored = run(
        capsys, "--data-dir", str(workspace), "tailor", "graph",
        "--job", str(job_id), "--lens", "fde",
    )
    assert code == 0 and tailored.get("resume_id")


# ------------------------------------------------- stale resume paths (Aug-16)
#
# The 3-application loom of 2026-08-16 uploaded the WRONG resumes: `adapt`
# rendered fresh PDFs into data/out but the `resumes` rows still carried the
# paths of long-dead /tmp files, and `apply` uploaded `resume.path` — wrong
# file for one run, no file at all for two others, none of it loud.


def _resume_row(data_dir: Path, resume_id: int) -> dict:
    conn = db.connect(data_dir)
    try:
        row = db.get_resume(conn, resume_id)
    finally:
        conn.close()
    assert row is not None
    return row


def test_adapt_pdf_row_points_at_the_file_it_just_rendered(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("playwright")
    code, tailored = run(
        capsys, "--data-dir", str(workspace), "adapt", "graph",
        "--lens", "fde", "--format", "pdf",
    )
    assert code == 0

    row = _resume_row(workspace, tailored["resume_id"])
    path = Path(row["path"])
    assert row["format"] == "pdf"
    assert row["path"] == tailored["path"]  # the row and the report agree
    assert path.is_absolute() and path.is_file() and path.suffix == ".pdf"
    with path.open("rb") as handle:
        assert handle.read(4) == b"%PDF"  # the rendered artifact, not a stale md


def test_adapt_never_leaves_a_resume_row_outside_data_out(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every default adapt lands in <data-dir>/out — never /tmp, never a path
    from some earlier run."""
    out_dir = (workspace / "out").resolve()
    for lens in ("fde", "sre", "design-engineer"):
        code, tailored = run(
            capsys, "--data-dir", str(workspace), "adapt", "graph", "--lens", lens
        )
        assert code == 0

    conn = db.connect(workspace)
    try:
        rows = db.get_resumes(conn)
    finally:
        conn.close()
    assert len(rows) == 3
    for row in rows:
        path = Path(row["path"])
        assert path.parent == out_dir, row
        assert not str(path).startswith(("/tmp/", "/private/tmp/")), row
        assert path.is_file(), row


def test_adapt_refuses_a_row_whose_artifact_is_not_the_asked_for_format(
    workspace: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A row that says `docx` while holding a `.md` path is a mislabelled
    upload; the row is refused rather than recorded."""
    code, payload = run(
        capsys, "--data-dir", str(workspace), "adapt", "graph", "--lens", "fde",
        "--format", "docx", "--out", str(tmp_path / "mislabelled.md"),
    )
    assert code == cli.EXIT_ERROR
    assert "not a docx file" in payload["error"]

    conn = db.connect(workspace)
    try:
        assert db.get_resumes(conn) == []
    finally:
        conn.close()


def test_adapt_links_the_resume_to_the_job_id_it_was_given(
    workspace: Path, capsys: pytest.CaptureFixture[str], sample_job: Path
) -> None:
    """`--job N` links to job N. The loom's rows had drifted to job #1."""
    ids = []
    for _ in range(3):
        code, added = run(
            capsys, "--data-dir", str(workspace), "jobs", "add", str(sample_job)
        )
        assert code == 0
        ids.append(added["job"]["id"])
    third = ids[-1]
    assert third != 1

    code, tailored = run(
        capsys, "--data-dir", str(workspace), "adapt", "graph", "--job", str(third)
    )
    assert code == 0
    assert tailored["job_id"] == third
    assert _resume_row(workspace, tailored["resume_id"])["job_id"] == third


def test_adapt_refuses_an_unknown_job_id(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = run(
        capsys, "--data-dir", str(workspace), "adapt", "graph", "--job", "404", "--lens", "fde"
    )
    assert code == cli.EXIT_USAGE
    assert "no job matches" in payload["error"]

    conn = db.connect(workspace)
    try:
        assert db.get_resumes(conn) == []
    finally:
        conn.close()


def test_apply_refuses_loudly_when_the_resume_file_is_gone(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two of three applications went out with no resume attached because the
    row's path had rotted. A missing artifact now fails the run."""
    code, tailored = run(capsys, "--data-dir", str(workspace), "adapt", "graph", "--lens", "fde")
    assert code == 0
    resume_id = tailored["resume_id"]
    Path(tailored["path"]).unlink()

    # the real run refuses before it ever looks for a model key or a browser
    monkeypatch.setenv("WEAVER_API_KEY", "test-key")
    code, payload = run(capsys, "--data-dir", str(workspace), "apply", str(resume_id))
    assert code == cli.EXIT_USAGE
    assert "does not exist" in payload["error"]
    assert f"resume #{resume_id}" in payload["error"]

    # and so does the dry run — no quiet "resume_source: missing" report
    code, payload = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--dry-run"
    )
    assert code == cli.EXIT_USAGE

    conn = db.connect(workspace)
    try:
        assert db.get_applications(conn) == []
    finally:
        conn.close()


def test_apply_refuses_a_resume_row_with_no_path_or_a_directory(
    workspace: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    conn = db.connect(workspace)
    try:
        empty = db.add_resume(
            conn, lens="fde", job_id=None, fmt="md", path="", prompt_used="",
            source_facts=[], structure={"name": "Mira Halloway"}, provider="deterministic",
        )
        a_dir = db.add_resume(
            conn, lens="fde", job_id=None, fmt="md", path=str(tmp_path), prompt_used="",
            source_facts=[], structure={"name": "Mira Halloway"}, provider="deterministic",
        )
    finally:
        conn.close()

    code, payload = run(capsys, "--data-dir", str(workspace), "apply", str(empty), "--dry-run")
    assert code == cli.EXIT_USAGE
    assert "no path on record" in payload["error"]

    code, payload = run(capsys, "--data-dir", str(workspace), "apply", str(a_dir), "--dry-run")
    assert code == cli.EXIT_USAGE
    assert "is not a file" in payload["error"]


def test_apply_still_accepts_a_hosted_resume_url(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The path guard checks local files only — a hosted resume is fetched at
    run time and must not be refused."""
    conn = db.connect(workspace)
    try:
        resume_id = db.add_resume(
            conn, lens="fde", job_id=None, fmt="pdf", path="/tmp/long-gone.pdf",
            prompt_used="", source_facts=[], structure={"name": "Mira Halloway"},
            provider="deterministic",
        )
    finally:
        conn.close()

    code, payload = run(
        capsys, "--data-dir", str(workspace), "apply", str(resume_id), "--dry-run",
        "--resume-url", "https://example.test/resume.pdf",
    )
    assert code == 0
    assert payload["resume_file"] == "https://example.test/resume.pdf"


# ------------------------------------------------------------------ batch cycle


def _batch_fixture(workspace: Path) -> dict[str, int]:
    """Three resumes with URL-bearing jobs + one with no job at all."""
    conn = db.connect(workspace)
    try:
        ids: dict[str, int] = {}
        for key, (title, url) in {
            "a": ("Design Engineer", "https://boards.example.test/a"),
            "b": ("Forward Deployed", "https://boards.example.test/b"),
            "c": ("Staff Something", "https://boards.example.test/c"),
        }.items():
            job_id = db.add_job(conn, url, title, "Verdant", "text", [])
            ids[key] = db.add_resume(
                conn, "fde", job_id, "md", "out/r.md", "", [], {}, "test"
            )
        ids["jobless"] = db.add_resume(conn, "fde", None, "md", "out/r.md", "", [], {}, "test")
        return ids
    finally:
        conn.close()


def test_batch_fills_the_queue_and_relays_skips_at_the_end(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cycle of docs/batch-cycle-contract.md: preflight-skips never reach a
    browser, one failure never blocks the rest, every fill marks complete on
    fill, and the report carries all of it."""
    ids = _batch_fixture(workspace)

    def fake_preflight(url: str, _applicant: dict) -> dict:
        if url.endswith("/b"):
            return {
                "verdict": "fail",
                "missing": [{"question": "Security clearance?", "needs_fact": "clearance"}],
            }
        return {"verdict": "pass", "missing": []}

    filled_order: list[int] = []

    def fake_apply(sub: object) -> int:
        # stands in for a real --visible --hold --tab run: ledger row, exit 0
        assert sub.tab and sub.hold and sub.visible and sub.force
        assert sub.notify is False  # one ping per cycle, never one per fill
        filled_order.append(sub.resume_id)
        conn = db.connect(workspace)
        try:
            status = "failed" if sub.resume_id == ids["c"] else "held"
            db.add_application(
                conn,
                resume_id=sub.resume_id,
                job_id=None,
                status=status,
                payload={},
                response={"reason": "boom" if status == "failed" else ""},
            )
        finally:
            conn.close()
        return 0 if status == "held" else cli.EXIT_FAILED

    monkeypatch.setattr(cli.preflight, "preflight", fake_preflight)
    monkeypatch.setattr(cli, "cmd_apply", fake_apply)
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code, payload = run(
        capsys,
        "--data-dir",
        str(workspace),
        "batch",
        str(ids["a"]),
        str(ids["b"]),
        str(ids["c"]),
        str(ids["jobless"]),
        "404",
    )

    assert code == cli.EXIT_FAILED  # c failed; skips alone would be exit 0
    assert filled_order == [ids["a"], ids["c"]]  # b and jobless never reached a browser
    assert payload["held"] == 1 and payload["failed"] == 1 and payload["skipped"] == 3
    by_id = {r["resume_id"]: r for r in payload["results"]}
    assert by_id[ids["a"]]["outcome"] == "filled"
    assert "Security clearance" in by_id[ids["b"]]["reason"]
    assert by_id[ids["c"]]["outcome"] == "failed"
    assert "no job" in by_id[ids["jobless"]]["reason"]
    assert by_id[404]["reason"] == "no such resume"


def test_batch_never_refills_a_held_or_sent_application(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _batch_fixture(workspace)
    conn = db.connect(workspace)
    try:
        db.add_application(
            conn, resume_id=ids["a"], job_id=None, status="held", payload={}, response={}
        )
        # a NEWER non-terminal row must not unmask the held one (double-fill bug)
        db.add_application(
            conn, resume_id=ids["a"], job_id=None, status="dry_run", payload={}, response={}
        )
    finally:
        conn.close()

    monkeypatch.setattr(
        cli.preflight, "preflight", lambda *_a: {"verdict": "pass", "missing": []}
    )
    monkeypatch.setattr(
        cli, "cmd_apply", lambda _sub: pytest.fail("a held resume must never refill")
    )
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code, payload = run(capsys, "--data-dir", str(workspace), "batch", str(ids["a"]))

    assert code == 0
    assert payload["skipped"] == 1
    assert "already held" in payload["results"][0]["reason"]


def test_batch_reports_a_rowless_exit_as_failed_never_as_the_stale_row(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cmd_apply can bail (usage error, dead tab-host) WITHOUT writing a ledger
    row. A stale audit_pending row from an old run must not turn that into a
    'pending' success — and a raising entry must not abort the cycle."""
    ids = _batch_fixture(workspace)
    conn = db.connect(workspace)
    try:
        db.add_application(
            conn, resume_id=ids["a"], job_id=None, status="audit_pending", payload={}, response={}
        )
    finally:
        conn.close()

    def fake_apply(sub: object) -> int:
        if sub.resume_id == ids["a"]:
            return cli.EXIT_USAGE  # bailed before the browser — no row written
        raise RuntimeError("tab host exploded")

    monkeypatch.setattr(
        cli.preflight, "preflight", lambda *_a: {"verdict": "pass", "missing": []}
    )
    monkeypatch.setattr(cli, "cmd_apply", fake_apply)
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code, payload = run(
        capsys, "--data-dir", str(workspace), "batch", str(ids["a"]), str(ids["c"])
    )

    assert code == cli.EXIT_FAILED
    assert payload["failed"] == 2 and payload["pending"] == 0
    by_id = {r["resume_id"]: r for r in payload["results"]}
    assert "no ledger row" in by_id[ids["a"]]["reason"]
    assert "tab host exploded" in by_id[ids["c"]]["reason"]  # cycle reached c


def test_batch_piped_stdout_is_one_json_document(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nohup weaver batch ... > out` used to interleave N per-run JSON blobs
    (screenshots included) with the report. Inner runs are forced human AND
    redirected to stderr, so piped stdout parses as exactly one document."""
    ids = _batch_fixture(workspace)

    def fake_apply(sub: object) -> int:
        assert getattr(sub, "force_human", False) is True
        assert cli.wants_json(sub) is False  # even though stdout is not a tty
        print("PER-RUN BLOB that must not reach batch stdout")
        conn = db.connect(workspace)
        try:
            db.add_application(
                conn, resume_id=sub.resume_id, job_id=None, status="held", payload={}, response={}
            )
        finally:
            conn.close()
        return 0

    monkeypatch.setattr(
        cli.preflight, "preflight", lambda *_a: {"verdict": "pass", "missing": []}
    )
    monkeypatch.setattr(cli, "cmd_apply", fake_apply)
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code = cli.main(["--data-dir", str(workspace), "batch", str(ids["a"]), "--json"])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)  # exactly one parseable document
    assert payload["held"] == 1
    assert "PER-RUN BLOB" not in captured.out
    assert "PER-RUN BLOB" in captured.err  # visible, but on stderr


def test_batch_never_double_applies_to_a_job_via_a_readapted_resume(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh resume id for the SAME posting (re-adapt, e.g. pdf→docx) must
    not open a second application on a job that is already held or sent."""
    ids = _batch_fixture(workspace)
    conn = db.connect(workspace)
    try:
        job_id = db.get_resume(conn, ids["a"])["job_id"]
        old_resume = db.add_resume(conn, "fde", job_id, "pdf", "out/old.pdf", "", [], {}, "test")
        db.add_application(
            conn, resume_id=old_resume, job_id=job_id, status="held", payload={}, response={}
        )
    finally:
        conn.close()

    monkeypatch.setattr(
        cli.preflight, "preflight", lambda *_a: {"verdict": "pass", "missing": []}
    )
    monkeypatch.setattr(
        cli, "cmd_apply", lambda _sub: pytest.fail("a held JOB must never refill")
    )
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code, payload = run(capsys, "--data-dir", str(workspace), "batch", str(ids["a"]))

    assert code == 0
    assert payload["skipped"] == 1
    assert "one application per posting" in payload["results"][0]["reason"]


def test_standalone_preflight_never_flags_the_resume_as_missing(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply/batch always attach a rendered resume, so the standalone verdict
    must not read `fail (needs fact: resume)` on every Greenhouse job."""
    conn = db.connect(workspace)
    try:
        job_id = db.add_job(
            conn, "https://boards.greenhouse.io/acme/jobs/1", "Designer", "Acme", "text", []
        )
    finally:
        conn.close()

    seen: dict = {}

    def fake_preflight(_url: str, applicant: dict) -> dict:
        seen.update(applicant)
        return {"verdict": "pass", "missing": [], "required_total": 1, "optional_total": 0}

    monkeypatch.setattr(cli.preflight, "preflight", fake_preflight)

    code, payload = run(capsys, "--data-dir", str(workspace), "preflight", str(job_id))

    assert code == 0 and payload["verdict"] == "pass"
    assert seen.get("resume")  # the shim answers Resume/CV questions


# --------------------------------------------------- cron / harness hardening


def test_batch_refuses_when_another_cycle_holds_the_lock(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping cron ticks must not race two cycles into the same window."""
    import os

    ids = _batch_fixture(workspace)
    lock = workspace / "batch.lock"
    lock.write_text(f"{os.getpid()} test\n")  # our own pid: definitely alive
    monkeypatch.setattr(
        cli, "cmd_apply", lambda _sub: pytest.fail("locked cycle must not fill")
    )

    code, payload = run(capsys, "--data-dir", str(workspace), "batch", str(ids["a"]))

    assert code == cli.EXIT_USAGE
    assert "already running" in payload["error"]
    assert lock.exists()  # never delete a live holder's lock


def test_a_stale_lock_from_a_dead_cycle_is_replaced(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _batch_fixture(workspace)
    lock = workspace / "batch.lock"
    lock.write_text("12345 test\n")
    # every liveness probe says "no such process" — the lock is stale
    monkeypatch.setattr(cli.os, "kill", lambda _pid, _sig: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(
        cli.preflight, "preflight", lambda *_a: {"verdict": "pass", "missing": []}
    )

    def fake_apply(sub: object) -> int:
        assert (workspace / "batch.lock").exists()  # we hold it during the fill
        conn = db.connect(workspace)
        try:
            db.add_application(
                conn, resume_id=sub.resume_id, job_id=None, status="held", payload={}, response={}
            )
        finally:
            conn.close()
        return 0

    monkeypatch.setattr(cli, "cmd_apply", fake_apply)
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code, payload = run(capsys, "--data-dir", str(workspace), "batch", str(ids["a"]))

    assert code == 0 and payload["held"] == 1
    assert not lock.exists()  # released on the way out


def test_a_dead_tab_host_fails_every_fill_with_one_remediation(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron without a GUI session can't open the shared window: every fillable
    entry must fail with the SAME actionable reason, skips still relay, and
    cmd_apply is never reached."""
    ids = _batch_fixture(workspace)

    def broken_host(_d: object) -> str:
        raise RuntimeError("the tab host did not open port 9777")

    monkeypatch.setattr(cli, "_ensure_tab_host", broken_host)
    monkeypatch.setattr(
        cli.preflight, "preflight", lambda *_a: {"verdict": "pass", "missing": []}
    )
    monkeypatch.setattr(
        cli, "cmd_apply", lambda _sub: pytest.fail("no host — cmd_apply must not run")
    )
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code, payload = run(
        capsys,
        "--data-dir", str(workspace),
        "batch", str(ids["a"]), str(ids["c"]), str(ids["jobless"]),
    )

    assert code == cli.EXIT_FAILED
    assert payload["failed"] == 2 and payload["skipped"] == 1
    for entry in payload["results"]:
        if entry["outcome"] == "failed":
            assert "tab-host unavailable" in entry["reason"]
            assert "GUI session" in entry["reason"]


def test_the_data_dir_env_file_loads_before_any_command(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron/harness runs have no shell profile: <data-dir>/env must be enough,
    and a real environment variable must always win over the file."""
    import os

    monkeypatch.delenv("WEAVER_CRON_PROBE", raising=False)
    monkeypatch.setenv("WEAVER_CRON_KEEP", "from-environment")
    (workspace / "env").write_text(
        "# cron credentials\n"
        'export WEAVER_CRON_PROBE="hello cron"\n'
        "WEAVER_CRON_KEEP=from-file\n"
        "not a kv line\n"
    )

    code, _payload = run(capsys, "--data-dir", str(workspace), "stats")

    assert code == 0
    assert os.environ["WEAVER_CRON_PROBE"] == "hello cron"
    assert os.environ["WEAVER_CRON_KEEP"] == "from-environment"
    monkeypatch.delenv("WEAVER_CRON_PROBE", raising=False)


def test_cycle_is_one_argv_find_adapt_batch_one_document(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gated-harness contract: a single `weaver cycle` argv runs the whole
    pipeline, relays prep skips without dying, and stdout parses as ONE JSON
    document with the batch report inside."""
    import json as _json

    shortlist = {
        "jobs": [
            {"url": "https://boards.example.test/good-a", "role": "Product Designer", "org": "TinyCo"},
            {"url": "https://boards.example.test/bad", "role": "Brand Designer", "org": "BrokenCo"},
            {"url": "https://boards.example.test/good-b", "role": "Design Engineer", "org": "SmallCo"},
        ]
    }

    def fake_find(ns: object) -> int:
        assert ns.force_human is True
        (workspace / "jobs.json").write_text(_json.dumps(shortlist))
        return 0

    def fake_jobs_add(conn: object, url: str, fetch_url: bool = False) -> dict:
        assert fetch_url is True
        if url.endswith("/bad"):
            raise RuntimeError("could not fetch the posting")
        c = db.connect(workspace)
        try:
            job_id = db.add_job(c, url, "Role", "Org", "x" * 300, [])
        finally:
            c.close()
        return {"id": job_id}

    def fake_tailor(ns: object) -> int:
        c = db.connect(workspace)
        try:
            db.add_resume(c, "fde", int(ns.job), ns.format, "out/r.docx", "", [], {}, "test")
        finally:
            c.close()
        return 0

    def fake_apply(sub: object) -> int:
        c = db.connect(workspace)
        try:
            db.add_application(
                c, resume_id=sub.resume_id, job_id=None, status="held", payload={}, response={}
            )
        finally:
            c.close()
        return 0

    monkeypatch.setattr(cli, "cmd_find", fake_find)
    monkeypatch.setattr(cli.jobs_mod, "add", fake_jobs_add)
    monkeypatch.setattr(cli, "cmd_tailor", fake_tailor)
    monkeypatch.setattr(
        cli.preflight, "preflight", lambda *_a: {"verdict": "pass", "missing": []}
    )
    monkeypatch.setattr(cli, "cmd_apply", fake_apply)
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code = cli.main(["--data-dir", str(workspace), "cycle", "--count", "3", "--json"])
    captured = capsys.readouterr()

    assert code == 0
    payload = _json.loads(captured.out)  # exactly one parseable document
    assert payload["found"] == 3
    assert payload["adapted"] == 2
    assert payload["held"] == 2
    assert len(payload["prep_skips"]) == 1
    assert payload["prep_skips"][0]["stage"] == "add"
    assert "could not fetch" in payload["prep_skips"][0]["reason"]
    assert not (workspace / "batch.lock").exists()  # lock released


# ------------------------------------------------- workable => real chrome only

#: Captured at import, BEFORE the autouse fixture stubs the module attribute —
#: the two _ensure_real_chrome unit tests exercise the real function.
_ORIG_ENSURE_REAL_CHROME = cli._ensure_real_chrome


def test_needs_real_chrome_matches_workable_urls_only() -> None:
    assert cli._needs_real_chrome("https://apply.workable.com/acme/j/ABC123/")
    assert cli._needs_real_chrome("https://apply.workable.com/j/ABC123")
    assert not cli._needs_real_chrome("https://boards.greenhouse.io/acme/jobs/1")
    assert not cli._needs_real_chrome("https://jobs.ashbyhq.com/acme/uuid")
    assert not cli._needs_real_chrome("")


def test_ensure_real_chrome_attaches_when_cdp_is_already_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.local_agent.local_driver, "_port_open", lambda _p: True)
    monkeypatch.setattr(
        cli, "_chrome_binary", lambda: pytest.fail("port open — must not launch")
    )
    url = _ORIG_ENSURE_REAL_CHROME(tmp_path)
    assert url == f"http://127.0.0.1:{cli.DEFAULT_REAL_CDP_PORT}"


def test_ensure_real_chrome_fails_plainly_without_a_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.local_agent.local_driver, "_port_open", lambda _p: False)
    monkeypatch.setattr(cli, "_chrome_binary", lambda: None)
    with pytest.raises(RuntimeError) as err:
        _ORIG_ENSURE_REAL_CHROME(tmp_path)
    assert "real Chrome" in str(err.value)


# ---------------------------------------- whose Chrome is on the port, exactly


def _own_the_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, browser: str) -> None:
    """Make the port look open and write weaver's own launch record for it."""
    monkeypatch.delenv("WEAVER_REAL_CDP_PORT", raising=False)
    monkeypatch.setattr(cli.local_agent.local_driver, "_port_open", lambda _p: True)
    monkeypatch.setattr(cli, "_cdp_browser", lambda *_a, **_k: browser)
    cli._record_real_chrome(tmp_path, cli.DEFAULT_REAL_CDP_PORT, os.getpid())


def test_ensure_real_chrome_records_the_instance_it_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch path leaves the evidence a later attach checks against."""
    import subprocess

    class FakeChrome:
        pid = 4242

    opens = iter([False, True])  # nothing listening, then the child is up
    monkeypatch.delenv("WEAVER_REAL_CDP_PORT", raising=False)
    monkeypatch.setattr(cli.local_agent.local_driver, "_port_open", lambda _p: next(opens, True))
    monkeypatch.setattr(cli, "_chrome_binary", lambda: "/usr/bin/true")
    monkeypatch.setattr(cli, "_cdp_browser", lambda *_a, **_k: "Chrome/139.0.7258.67")
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: FakeChrome())

    url = _ORIG_ENSURE_REAL_CHROME(tmp_path)

    assert url == f"http://127.0.0.1:{cli.DEFAULT_REAL_CDP_PORT}"
    record = json.loads((tmp_path / "real-chrome.pid").read_text(encoding="utf-8"))
    assert record["pid"] == 4242
    assert record["port"] == cli.DEFAULT_REAL_CDP_PORT
    assert record["browser"] == "Chrome/139.0.7258.67"


def test_ensure_real_chrome_attaches_silently_to_its_own_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _own_the_port(tmp_path, monkeypatch, "Chrome/139.0.7258.67")
    monkeypatch.setattr(
        cli, "_chrome_binary", lambda: pytest.fail("port open — must not launch")
    )

    url = _ORIG_ENSURE_REAL_CHROME(tmp_path)

    assert url == f"http://127.0.0.1:{cli.DEFAULT_REAL_CDP_PORT}"
    assert cli._foreign_real_chrome(tmp_path) is None
    assert capsys.readouterr().err == ""  # weaver's own Chrome: nothing to say


def test_ensure_real_chrome_warns_when_a_stranger_holds_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live test #5 (2026-08-19): an unrelated Chrome on 9223 was attached to
    and the fill died at setDownloadBehavior. The attach still happens — but
    it names the port and the override first."""
    monkeypatch.delenv("WEAVER_REAL_CDP_PORT", raising=False)
    monkeypatch.setattr(cli.local_agent.local_driver, "_port_open", lambda _p: True)
    monkeypatch.setattr(cli, "_cdp_browser", lambda *_a, **_k: "Chrome/139.0.7258.67")
    monkeypatch.setattr(
        cli, "_chrome_binary", lambda: pytest.fail("port open — must not launch")
    )

    url = _ORIG_ENSURE_REAL_CHROME(tmp_path)  # still attaches, as before
    err = capsys.readouterr().err

    assert url == f"http://127.0.0.1:{cli.DEFAULT_REAL_CDP_PORT}"
    assert "did not launch" in err
    assert str(cli.DEFAULT_REAL_CDP_PORT) in err
    assert "WEAVER_REAL_CDP_PORT" in err


@pytest.mark.parametrize(
    "spoil, expected",
    [
        (lambda rec: rec.update(port=9999), "not 9223"),
        (lambda rec: rec.update(browser="Chrome/1.2.3.4"), "reports Chrome/139"),
    ],
)
def test_foreign_real_chrome_names_what_does_not_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoil: object,
    expected: str,
) -> None:
    _own_the_port(tmp_path, monkeypatch, "Chrome/139.0.7258.67")
    path = tmp_path / "real-chrome.pid"
    record = json.loads(path.read_text(encoding="utf-8"))
    spoil(record)
    path.write_text(json.dumps(record), encoding="utf-8")

    reason = cli._foreign_real_chrome(tmp_path)
    assert reason and expected in reason


def test_foreign_real_chrome_flags_a_dead_launch_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _own_the_port(tmp_path, monkeypatch, "Chrome/139.0.7258.67")
    monkeypatch.setattr(cli, "_pid_alive", lambda _p: False)
    reason = cli._foreign_real_chrome(tmp_path)
    assert reason and "is gone" in reason


def test_foreign_real_chrome_is_quiet_when_nothing_listens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed port is the launch path's business, not a stranger warning."""
    monkeypatch.setattr(cli.local_agent.local_driver, "_port_open", lambda _p: False)
    assert cli._foreign_real_chrome(tmp_path) is None
    assert cli._real_chrome_attach_hint(tmp_path) is None


def test_real_chrome_attach_hint_carries_the_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a run that dies AFTER attaching appends to its error."""
    monkeypatch.delenv("WEAVER_REAL_CDP_PORT", raising=False)
    monkeypatch.setattr(cli.local_agent.local_driver, "_port_open", lambda _p: True)
    monkeypatch.setattr(cli, "_cdp_browser", lambda *_a, **_k: None)

    hint = cli._real_chrome_attach_hint(tmp_path)
    assert hint and "WEAVER_REAL_CDP_PORT" in hint
    assert str(cli.DEFAULT_REAL_CDP_PORT) in hint

    _own_the_port(tmp_path, monkeypatch, "Chrome/139.0.7258.67")
    assert cli._real_chrome_attach_hint(tmp_path) is None  # our own Chrome: no hint


def _workable_batch_fixture(workspace: Path) -> dict[str, int]:
    """One workable job and one greenhouse job, each with a resume."""
    conn = db.connect(workspace)
    try:
        ids: dict[str, int] = {}
        for key, url in {
            "workable": "https://apply.workable.com/verdant/j/AB12CD34EF/",
            "greenhouse": "https://boards.greenhouse.io/verdant/jobs/1001",
        }.items():
            job_id = db.add_job(conn, url, f"Designer {key}", "Verdant", "text", [])
            ids[key] = db.add_resume(
                conn, "fde", job_id, "md", "out/r.md", "", [], {}, "test"
            )
        return ids
    finally:
        conn.close()


def test_batch_routes_workable_entries_to_real_chrome(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turnstile-at-submit (2026-08-19 Bridgit run): workable fills go to a
    real Chrome host; every other board keeps the Playwright tab-host."""
    ids = _workable_batch_fixture(workspace)
    hosts: list[str] = []
    monkeypatch.setattr(cli, "_ensure_tab_host", lambda _d: hosts.append("tab") or "cdp://tab")
    monkeypatch.setattr(
        cli, "_ensure_real_chrome", lambda _d: hosts.append("chrome") or "cdp://chrome"
    )
    monkeypatch.setattr(
        cli.preflight, "preflight", lambda _u, _a: {"verdict": "pass", "missing": []}
    )

    def fake_apply(sub: object) -> int:
        conn = db.connect(workspace)
        try:
            db.add_application(
                conn, resume_id=sub.resume_id, job_id=None, status="held", payload={}, response={}
            )
        finally:
            conn.close()
        return 0

    monkeypatch.setattr(cli, "cmd_apply", fake_apply)
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code, payload = run(
        capsys, "--data-dir", str(workspace), "batch", str(ids["workable"]), str(ids["greenhouse"])
    )
    assert code == 0
    assert payload["held"] == 2
    assert sorted(hosts) == ["chrome", "tab"]  # one ensure per host kind


def test_batch_real_chrome_down_fails_only_workable_entries(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _workable_batch_fixture(workspace)

    def no_chrome(_d: Path) -> str:
        raise RuntimeError("no Chrome binary found")

    monkeypatch.setattr(cli, "_ensure_real_chrome", no_chrome)
    monkeypatch.setattr(
        cli.preflight, "preflight", lambda _u, _a: {"verdict": "pass", "missing": []}
    )

    def fake_apply(sub: object) -> int:
        conn = db.connect(workspace)
        try:
            db.add_application(
                conn, resume_id=sub.resume_id, job_id=None, status="held", payload={}, response={}
            )
        finally:
            conn.close()
        return 0

    monkeypatch.setattr(cli, "cmd_apply", fake_apply)
    monkeypatch.setattr(cli.completion, "notify", lambda *a, **k: True)

    code, payload = run(
        capsys, "--data-dir", str(workspace), "batch", str(ids["workable"]), str(ids["greenhouse"])
    )
    assert code == cli.EXIT_FAILED  # the workable entry failed
    by_id = {r["resume_id"]: r for r in payload["results"]}
    assert by_id[ids["workable"]]["outcome"] == "failed"
    assert "real-chrome unavailable" in by_id[ids["workable"]]["reason"]
    assert "real Chrome" in by_id[ids["workable"]]["reason"]  # the remedy travels
    assert by_id[ids["greenhouse"]]["outcome"] == "filled"  # tab-host lane unaffected


# ------------------------------------------------- real-chrome host routing


def test_lever_urls_route_to_the_real_chrome_host() -> None:
    """Run 145 (Metabase, 2026-08-24) hit an endless CAPTCHA re-challenge in the
    Playwright tab-host: Lever flags it as a bot. Same family as workable's
    Turnstile — the fix is the host, not the fill loop."""
    assert cli._needs_real_chrome("https://jobs.lever.co/metabase/b6ab96a1/apply")
    assert cli._needs_real_chrome("https://apply.workable.com/acme/j/3A2AE898F0/")
    # the tab-host lane must stay untouched — these fill fine today
    assert not cli._needs_real_chrome("https://job-boards.greenhouse.io/customerio/jobs/8039027")
    assert not cli._needs_real_chrome("https://jobs.ashbyhq.com/browserbase/b340041a")
    assert not cli._needs_real_chrome("")


def test_real_chrome_hosts_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENTS.md rule 7: a fresh clone adds a bot-walled provider by config,
    never by editing code."""
    monkeypatch.setenv("WEAVER_REAL_CHROME_HOSTS", "boards.example.com, jobs.lever.co")
    assert cli._needs_real_chrome("https://boards.example.com/acme/apply")
    assert cli._needs_real_chrome("https://jobs.lever.co/metabase/b6ab96a1/apply")
    # an explicit set REPLACES the defaults — workable is not in this one
    assert not cli._needs_real_chrome("https://apply.workable.com/acme/j/3A2AE898F0/")


def test_blank_real_chrome_hosts_override_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty/whitespace override is an unset override, not "route nothing" —
    otherwise a stray export silently sends workable back into the tab-host."""
    monkeypatch.setenv("WEAVER_REAL_CHROME_HOSTS", "   ,  ")
    assert cli._needs_real_chrome("https://apply.workable.com/acme/j/3A2AE898F0/")
    assert cli._needs_real_chrome("https://jobs.lever.co/metabase/b6ab96a1/apply")


# ------------------------------------------------- apps set-status (the ledger)


def _one_application(workspace: Path, status: str = "held") -> int:
    conn = db.connect(workspace)
    try:
        return db.add_application(
            conn, resume_id=None, job_id=None, status=status, payload={}, response={}
        )
    finally:
        conn.close()


def test_apps_set_status_records_the_real_outcome(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ledger goes stale the moment a human sends a held tab — weaver cannot
    see the send. Without this, reconciling means hand-written SQL against the
    live db, which is how a ledger stops being trusted."""
    app_id = _one_application(workspace, "held")

    code, payload = run(
        capsys, "--data-dir", str(workspace), "apps", "set-status", str(app_id), "submitted"
    )

    assert code == 0
    assert payload["ok"] is True
    assert payload["was"] == "held" and payload["status"] == "submitted"
    conn = db.connect(workspace)
    try:
        assert db.get_application(conn, app_id)["status"] == "submitted"
    finally:
        conn.close()


def test_apps_set_status_takes_a_note_and_keeps_it(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Why a row changed matters as much as that it did — a `failed` with no
    reason is the thing nobody can act on later."""
    app_id = _one_application(workspace, "audit_pending")

    code, payload = run(
        capsys, "--data-dir", str(workspace), "apps", "set-status", str(app_id),
        "failed", "--note", "lever bot wall — repeated CAPTCHA, never submitted",
    )

    assert code == 0
    conn = db.connect(workspace)
    try:
        app = db.get_application(conn, app_id)
    finally:
        conn.close()
    assert app["status"] == "failed"
    assert "lever bot wall" in json.dumps(app["response"])


def test_apps_set_status_refuses_an_unknown_status(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo must not invent a ledger state that no report knows how to count."""
    app_id = _one_application(workspace, "held")

    code, payload = run(
        capsys, "--data-dir", str(workspace), "apps", "set-status", str(app_id), "submited"
    )

    assert code == cli.EXIT_USAGE
    assert "submitted" in payload["error"]  # the remedy names the real ones
    conn = db.connect(workspace)
    try:
        assert db.get_application(conn, app_id)["status"] == "held"  # untouched
    finally:
        conn.close()


def test_apps_set_status_unknown_application_is_a_usage_error(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = run(
        capsys, "--data-dir", str(workspace), "apps", "set-status", "9999", "submitted"
    )
    assert code == cli.EXIT_USAGE
    assert "9999" in payload["error"]
