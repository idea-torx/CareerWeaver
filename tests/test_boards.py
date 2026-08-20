"""The board registry + `weaver find --wide` — the whole-web sweep.

Offline: every feed is canned JSON stubbed over `jobs.http_get_json`, and the
politeness delay is a recording stub, so nothing here sleeps or reaches out.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from weaver import boards as boards_mod
from weaver import cli
from weaver import config as cfg_mod
from weaver import db

TARGETS: dict[str, Any] = {
    "target_titles": ["Brand Designer", "Product Designer", "Design Engineer"],
    "skills": ["Webflow", "Framer", "design systems", "branding"],
    "seniority": ["staff", "senior", "principal"],
    "locations": ["US Remote"],
}

GH_1001 = "https://boards.greenhouse.io/webflow/jobs/1001"
AGENCY_URL = "https://boards.greenhouse.io/instrument/jobs/2001"
ASHBY_URL = "https://jobs.ashbyhq.com/vercel/222"


def feed_for(slug: str, kind: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Stand-in for `jobs.fetch_board_jobs` — one posting per known board."""
    catalog = {
        ("webflow", "greenhouse"): {
            "role": "Staff Brand Designer",
            "url": GH_1001,
            "location": "Remote — US",
            "body": "branding, design systems, Webflow and Framer",
        },
        ("instrument", "greenhouse"): {
            "role": "Senior Brand Designer",
            "url": AGENCY_URL,
            "location": "Portland",
            "body": "agency brand identity, design systems",
        },
        ("vercel", "ashby"): {
            "role": "Design Engineer",
            "url": ASHBY_URL,
            "location": "Remote — US",
            "body": "Webflow, Framer, design systems, branding",
        },
    }
    posting = catalog.get((slug, kind))
    if posting is None:
        raise RuntimeError(f"could not fetch {slug} ({kind}): 404")
    return [{"org": slug, "board": kind, **posting}]


REGISTRY = [
    {"name": "Webflow", "kind": "greenhouse", "slug": "webflow", "tags": ["product"]},
    {"name": "Instrument", "kind": "greenhouse", "slug": "instrument", "tags": ["agency"]},
    {"name": "Vercel", "kind": "ashby", "slug": "vercel", "tags": ["product"]},
]


def entries(raw: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [boards_mod.normalize_entry(item) for item in (raw or REGISTRY)]


# ------------------------------------------------------------------- entries


def test_normalize_entry_accepts_every_written_form() -> None:
    explicit = boards_mod.normalize_entry({"name": "Webflow", "kind": "Greenhouse", "slug": "Webflow"})
    assert explicit == {"name": "Webflow", "kind": "greenhouse", "slug": "webflow", "url": "", "tags": []}
    # the {"name": ..., "<kind>": "<slug>"} agency shorthand from the contract
    assert boards_mod.normalize_entry({"name": "AQ", "greenhouse": "aq"})["slug"] == "aq"
    assert boards_mod.normalize_entry("acme:lever") == {
        "name": "acme", "kind": "lever", "slug": "acme", "url": "", "tags": []
    }
    assert boards_mod.normalize_entry("acme")["kind"] == "greenhouse"
    other = boards_mod.normalize_entry({"name": "Studio", "kind": "other", "url": "https://s.co/jobs"})
    assert other["kind"] == "other" and other["slug"] == ""

    with pytest.raises(boards_mod.BoardError):
        boards_mod.normalize_entry({"name": "x", "kind": "monster", "slug": "x"})
    with pytest.raises(boards_mod.BoardError):
        boards_mod.normalize_entry({"name": "x"})
    with pytest.raises(boards_mod.BoardError):
        boards_mod.normalize_entry({"kind": "greenhouse"})
    with pytest.raises(boards_mod.BoardError):
        boards_mod.normalize_entry(17)


def test_builtin_registry_is_generic_and_well_formed() -> None:
    registry = boards_mod.load_builtin()
    assert len(registry) >= 40  # a real starter sweep, not a token list
    assert {e["kind"] for e in registry} <= set(boards_mod.BOARD_KINDS)
    assert all(e["slug"] and e["name"] for e in registry)
    keys = [boards_mod.entry_key(e) for e in registry]
    assert len(keys) == len(set(keys))  # no duplicate boards
    kinds = {e["kind"] for e in registry}
    assert {"greenhouse", "lever", "ashby"} <= kinds
    assert any("agency" in e["tags"] for e in registry)  # agencies are in the same sweep


def test_load_registry_merges_user_file_config_and_builtin(tmp_path: Path) -> None:
    boards_mod.registry_path(tmp_path).write_text(
        json.dumps({"boards": [{"name": "Mine", "kind": "lever", "slug": "mine"}, {"kind": "bogus"}]}),
        encoding="utf-8",
    )
    config = {"find": {"orgs": {"acme": "lever"}, "boards": [{"name": "Ag", "greenhouse": "ag"}]}}
    problems: list[str] = []
    registry = boards_mod.load_registry(tmp_path, config, problems)

    assert [e["slug"] for e in registry[:3]] == ["mine", "ag", "acme"]  # user first, builtin last
    assert len(registry) == len(boards_mod.load_builtin()) + 3
    assert problems and "bogus" in problems[0]  # a bad entry is reported, never silent


def test_load_registry_dedupes_across_layers(tmp_path: Path) -> None:
    boards_mod.registry_path(tmp_path).write_text(
        json.dumps({"boards": [{"name": "Webflow (mine)", "kind": "greenhouse", "slug": "WEBFLOW"}]}),
        encoding="utf-8",
    )
    registry = boards_mod.load_registry(tmp_path, {})
    hits = [e for e in registry if e["slug"] == "webflow" and e["kind"] == "greenhouse"]
    assert len(hits) == 1 and hits[0]["name"] == "Webflow (mine)"


def test_add_writes_the_user_registry_and_is_idempotent(tmp_path: Path) -> None:
    entry = boards_mod.add(tmp_path, "acme", "lever")
    assert entry["kind"] == "lever" and entry["slug"] == "acme"
    again = boards_mod.add(tmp_path, "ACME", "lever")
    assert again["slug"] == "acme"
    saved = json.loads(boards_mod.registry_path(tmp_path).read_text(encoding="utf-8"))
    assert len(saved["boards"]) == 1
    boards_mod.add(tmp_path, "acme", "greenhouse")  # same slug, different board = a new entry
    saved = json.loads(boards_mod.registry_path(tmp_path).read_text(encoding="utf-8"))
    assert len(saved["boards"]) == 2
    with pytest.raises(boards_mod.BoardError):
        boards_mod.add(tmp_path, "acme", "monster")


def test_read_registry_file_survives_missing_and_broken(tmp_path: Path) -> None:
    assert boards_mod.read_registry_file(tmp_path / "nope.json") == []
    broken = tmp_path / "boards.json"
    broken.write_text("{not json", encoding="utf-8")
    problems: list[str] = []
    assert boards_mod.read_registry_file(broken, problems) == []
    assert problems


# --------------------------------------------------------------------- sweep


def test_sweep_ranks_across_every_registry_board() -> None:
    result = boards_mod.sweep(entries(), TARGETS, threshold=0.6, limit=10, delay=0, fetch=feed_for)
    assert result["scanned"] == 3
    assert result["errors"] == []
    assert result["boards_swept"] == 3 and result["boards_registered"] == 3
    assert [job["url"] for job in result["jobs"]] == [GH_1001, ASHBY_URL, AGENCY_URL]
    assert result["jobs"][0]["fit"] == 1.0
    assert result["jobs"][-1]["org"] == "instrument"  # the agency ranks in the same list


def test_sweep_is_sequential_and_polite() -> None:
    naps: list[float] = []
    order: list[str] = []

    def recording_fetch(slug: str, kind: str, timeout: float = 15.0) -> list[dict[str, Any]]:
        order.append(slug)
        return feed_for(slug, kind, timeout)

    result = boards_mod.sweep(
        entries(), TARGETS, threshold=0.0, limit=99, delay=0.5, sleep=naps.append, fetch=recording_fetch
    )
    assert order == ["webflow", "instrument", "vercel"]  # one board at a time, registry order
    assert naps == [0.5, 0.5]  # a delay *between* boards, none before the first
    assert result["boards_swept"] == 3


def test_sweep_caps_boards_and_reports_what_it_skipped() -> None:
    result = boards_mod.sweep(
        entries(), TARGETS, threshold=0.0, limit=99, max_boards=2, delay=0, fetch=feed_for
    )
    assert result["boards_swept"] == 2
    assert result["boards_over_cap"] == ["vercel:ashby"]  # named, not silently dropped
    assert {job["org"] for job in result["jobs"]} == {"webflow", "instrument"}


def test_sweep_default_cap_is_sixty() -> None:
    assert boards_mod.DEFAULT_MAX_BOARDS == 60
    many = entries([{"name": f"org{i}", "kind": "greenhouse", "slug": f"org{i}"} for i in range(75)])
    calls: list[str] = []

    def counting(slug: str, kind: str, timeout: float = 15.0) -> list[dict[str, Any]]:
        calls.append(slug)
        return []

    result = boards_mod.sweep(many, TARGETS, delay=0, fetch=counting)
    assert len(calls) == 60
    assert len(result["boards_over_cap"]) == 15


def test_sweep_carries_on_past_a_dead_board() -> None:
    registry = entries(REGISTRY + [{"name": "Gone", "kind": "lever", "slug": "gone"}])
    result = boards_mod.sweep(registry, TARGETS, threshold=0.6, limit=99, delay=0, fetch=feed_for)
    assert [e["org"] for e in result["errors"]] == ["gone"]
    assert result["jobs"]  # the healthy boards still land in the shortlist


def test_sweep_reports_kinds_with_no_feed_parser() -> None:
    registry = entries(REGISTRY + [{"name": "Studio", "kind": "bamboohr", "slug": "studio"}])
    result = boards_mod.sweep(registry, TARGETS, threshold=0.0, limit=99, delay=0, fetch=feed_for)
    assert result["boards_unsupported"] == ["Studio:bamboohr"]
    assert result["boards_swept"] == 3


def test_sweep_fetches_workable_boards() -> None:
    # workable graduated from recorded-only to FETCHABLE — a registry entry
    # must be swept, not parked in boards_unsupported.
    assert "workable" in boards_mod.FETCHABLE
    registry = entries([{"name": "Studio", "kind": "workable", "slug": "studio"}])
    calls: list[tuple[str, str]] = []

    def counting(slug: str, kind: str, timeout: float = 15.0) -> list[dict[str, Any]]:
        calls.append((slug, kind))
        return []

    result = boards_mod.sweep(registry, TARGETS, delay=0, fetch=counting)
    assert calls == [("studio", "workable")]
    assert result["boards_unsupported"] == []


# ----------------------------------------------------------------------- cli


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
        {"target_roles": TARGETS["target_titles"], "target_skills": TARGETS["skills"]}
    )
    config["find"]["locations"] = TARGETS["locations"]
    config["find"]["orgs"] = {"webflow": "greenhouse"}
    cfg_mod.save_config(data, config)
    boards_mod.registry_path(data).write_text(
        json.dumps({"version": 1, "boards": REGISTRY}), encoding="utf-8"
    )
    return data


@pytest.fixture
def stub_sweep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Canned feeds + a non-sleeping, recording politeness delay."""
    naps: list[float] = []
    real_sweep = boards_mod.sweep

    def patched(entries_arg, targets, **kwargs):
        kwargs.setdefault("fetch", feed_for)
        kwargs["sleep"] = naps.append
        return real_sweep(entries_arg, targets, **kwargs)

    monkeypatch.setattr(cli.boards_mod, "sweep", patched)
    return naps


def _app(conn: sqlite3.Connection, url: str) -> None:
    resume_id = db.add_resume(
        conn,
        lens="brand",
        job_id=None,
        fmt="md",
        path="out/r.md",
        prompt_used="",
        source_facts=[],
        structure={},
        provider="deterministic",
    )
    db.add_application(
        conn, resume_id=resume_id, job_id=None, status="applied", payload={}, response=None, url=url
    )


def test_cli_find_wide_sweeps_the_registry(
    workspace: Path, stub_sweep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = run(capsys, "--data-dir", str(workspace), "find", "--wide", "--fit", "0.6")
    assert code == 0
    assert payload["wide"] is True
    # the user registry is swept ahead of the committed starter set
    assert list(payload["orgs"])[:3] == ["webflow", "instrument", "vercel"]
    assert payload["registry"]["max_boards"] == 60
    assert payload["registry"]["swept"] <= 60
    assert [job["url"] for job in payload["jobs"]][:3] == [GH_1001, ASHBY_URL, AGENCY_URL]
    assert payload["shortlist"] == str(workspace / "jobs.json")
    saved = json.loads((workspace / "jobs.json").read_text(encoding="utf-8"))
    assert [j["url"] for j in saved["jobs"]] == [j["url"] for j in payload["jobs"]]
    assert stub_sweep and set(stub_sweep) == {0.5}  # politeness delay actually applied


def test_cli_find_wide_skips_jobs_already_in_the_ledger(
    workspace: Path, stub_sweep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    conn = db.init_db(workspace)
    _app(conn, GH_1001 + "?gh_src=tracking")  # same posting, tracked link
    conn.close()

    code, payload = run(capsys, "--data-dir", str(workspace), "find", "--wide", "--fit", "0.6")
    assert code == 0
    assert payload["already_applied"] == 1
    assert payload["already_applied_jobs"] == [
        {"role": "Staff Brand Designer", "org": "webflow", "url": GH_1001}
    ]
    assert GH_1001 not in [job["url"] for job in payload["jobs"]]
    saved = json.loads((workspace / "jobs.json").read_text(encoding="utf-8"))
    assert GH_1001 not in [job["url"] for job in saved["jobs"]]


def test_cli_find_wide_human_reports_the_sweep_and_the_skips(
    workspace: Path, stub_sweep: list[float], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.init_db(workspace)
    _app(conn, GH_1001)
    conn.close()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)  # force the human path

    code = cli.main(
        ["--data-dir", str(workspace), "find", "--wide", "--fit", "0.6", "--max-boards", "3"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "wide sweep — 3/" in out and "cap 3" in out
    assert "1 already applied, skipped — see `weaver ledger`" in out
    assert "past the --max-boards cap" in out  # the cap is never silent
    assert "Design Engineer" in out


def test_cli_find_wide_flags_and_guards(
    workspace: Path, stub_sweep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = run(
        capsys, "--data-dir", str(workspace), "find", "--wide", "--max-boards", "1",
        "--delay", "0", "--limit", "1",
    )
    assert code == 0
    assert payload["registry"]["swept"] == 1 and payload["count"] == 1
    assert stub_sweep == []  # --delay 0 means no naps at all

    code, payload = run(capsys, "--data-dir", str(workspace), "find", "--wide", "webflow")
    assert code == cli.EXIT_USAGE
    assert "drop the org arguments" in payload["error"]


def test_cli_find_without_wide_still_uses_configured_orgs(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.jobs_mod, "fetch_board_jobs", feed_for)
    code, payload = run(capsys, "--data-dir", str(workspace), "find", "--fit", "0.6")
    assert code == 0
    assert payload["wide"] is False
    assert payload["orgs"] == {"webflow": "greenhouse"}  # registry untouched
    assert [job["url"] for job in payload["jobs"]] == [GH_1001]


def test_cli_boards_list_and_add(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run(capsys, "--data-dir", str(workspace), "boards", "list")
    assert code == 0
    baseline = payload["count"]
    # the fixture's three boards are all in the starter registry too — merged, not doubled
    assert baseline == len(boards_mod.load_builtin())
    assert payload["by_kind"]["greenhouse"] >= 1
    assert payload["problems"] == []

    code, payload = run(capsys, "--data-dir", str(workspace), "boards", "add", "acme", "lever")
    assert code == 0
    assert payload["added"] == {"name": "acme", "kind": "lever", "slug": "acme", "url": "", "tags": []}
    assert payload["count"] == baseline + 1

    code, payload = run(capsys, "--data-dir", str(workspace), "boards")  # bare = list
    assert code == 0 and payload["count"] == baseline + 1
    assert any(e["slug"] == "acme" for e in payload["boards"])

    with pytest.raises(SystemExit):  # argparse rejects an unknown board kind
        cli.main(["--data-dir", str(workspace), "boards", "add", "acme", "monster"])
