"""Lane 1 — `weaver find`: fit scorer, board parsers, ranking, and the CLI.

Everything here is offline: feeds are canned JSON stubbed over the single
network touchpoint (`jobs.http_get_json`). The live-feed smoke test is marked
`live` and deselected by default (run it with: pytest -m live).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from weaver import cli
from weaver import config as cfg_mod
from weaver import jobs as jobs_mod

TARGETS: dict[str, Any] = {
    "target_titles": ["Brand Designer", "Product Designer", "Design Engineer"],
    "skills": ["Webflow", "Framer", "design systems", "branding"],
    "seniority": ["staff", "senior", "principal"],
    "locations": ["US Remote", "CA Remote", "Canada"],
}

GREENHOUSE_FEED: dict[str, Any] = {
    "jobs": [
        {
            "title": "Staff Brand Designer",
            "absolute_url": "https://boards.greenhouse.io/webflow/jobs/1001",
            "location": {"name": "Remote — US"},
            "content": "&lt;p&gt;Webflow &amp;amp; Framer; own branding and our design systems.&lt;/p&gt;",
        },
        {
            "title": "Brand Designer",
            "absolute_url": "https://boards.greenhouse.io/webflow/jobs/1002",
            "location": {"name": "Canada"},
            "content": "&lt;p&gt;brand identity work&lt;/p&gt;",
        },
        {
            "title": "Senior Tax Accountant",
            "absolute_url": "https://boards.greenhouse.io/webflow/jobs/1003",
            "location": {"name": "San Francisco"},
            "content": "&lt;p&gt;CPA and Excel.&lt;/p&gt;",
        },
        {  # untitled feed noise — dropped
            "title": "",
            "absolute_url": "https://boards.greenhouse.io/webflow/jobs/1004",
            "location": {"name": "NY"},
            "content": "",
        },
    ]
}

LEVER_FEED: list[dict[str, Any]] = [
    {
        "text": "Product Designer",
        "hostedUrl": "https://jobs.lever.co/acme/111",
        "categories": {"location": "Remote — US"},
        "descriptionPlain": "Framer prototypes.",
        "lists": [{"text": "Requirements", "content": "&lt;li&gt;Webflow&lt;/li&gt;"}],
    }
]

ASHBY_FEED: dict[str, Any] = {
    "jobs": [
        {
            "title": "Design Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/acme/222",
            "location": "Canada Remote",
            "descriptionHtml": "&lt;p&gt;Webflow&lt;/p&gt;",
        }
    ]
}

WORKABLE_FEED: dict[str, Any] = {
    "name": "Acme",
    "jobs": [
        {
            "title": "Product Designer",
            "shortcode": "3A2AE898F0",
            "url": "https://apply.workable.com/j/3A2AE898F0",
            "shortlink": "https://apply.workable.com/j/3A2AE898F0",
            "country": "Canada",
            "city": "Vancouver",
            "state": "",
            "telecommuting": True,
            "description": "&lt;p&gt;Webflow and Framer.&lt;/p&gt;",
        },
        {  # untitled feed noise — dropped
            "title": "",
            "shortcode": "0000000000",
            "description": "",
        },
    ],
}


def posting(**overrides: Any) -> dict[str, Any]:
    base = {
        "role": "Staff Brand Designer",
        "org": "webflow",
        "board": "greenhouse",
        "url": "https://boards.greenhouse.io/webflow/jobs/1",
        "location": "Remote — US",
        "body": "Own branding and design systems. Webflow and Framer required.",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ fit scorer


def test_fit_scores_a_perfect_posting_at_1() -> None:
    fit, reasons = jobs_mod.score_fit(posting(), TARGETS)
    assert fit == 1.0
    assert "title 2/2 (brand designer)" in reasons
    assert "skills 4/4 (webflow, framer, design systems, branding)" in reasons
    assert "seniority: staff" in reasons
    assert "location: us remote" in reasons


def test_fit_scores_an_irrelevant_posting_low() -> None:
    fit, reasons = jobs_mod.score_fit(
        posting(role="Senior Tax Accountant", location="Berlin", body="CPA, Excel."), TARGETS
    )
    assert fit == 0.10  # seniority bump only
    assert "skills 0/4" in reasons
    assert "seniority: senior" in reasons


def test_fit_without_targets_is_zero_with_a_hint() -> None:
    fit, reasons = jobs_mod.score_fit(posting(), {"target_titles": [], "skills": []})
    assert fit == 0.0
    assert reasons and reasons[0].startswith("no targets configured")


def test_fit_component_bumps() -> None:
    # title overlap only -> 0.45
    fit, reasons = jobs_mod.score_fit(posting(role="Brand Designer", location="", body="art direction"), TARGETS)
    assert fit == 0.45
    assert not any(r.startswith("seniority") for r in reasons)

    # + seniority word in the posting's own title -> 0.55
    fit, _ = jobs_mod.score_fit(posting(role="Principal Brand Designer", location="", body="art direction"), TARGETS)
    assert fit == 0.55

    # + location matched in the location field -> 0.55
    fit, reasons = jobs_mod.score_fit(posting(role="Brand Designer", location="Canada", body="art direction"), TARGETS)
    assert fit == 0.55
    assert "location: canada" in reasons

    # location matched only in the body still counts
    fit, reasons = jobs_mod.score_fit(
        posting(role="Brand Designer", location="Remote", body="art direction; open to CA candidates"), TARGETS
    )
    assert fit == 0.55
    assert "location: ca remote" in reasons


def test_fit_partial_overlap_and_bounds() -> None:
    fit, reasons = jobs_mod.score_fit(posting(role="Motion Artist", location="", body="branding in Webflow"), TARGETS)
    # no target title matched at all; skills 2/4 (webflow, branding)
    assert fit == round(0.35 * 0.5, 2)
    assert "skills 2/4 (webflow, branding)" in reasons
    assert not any(r.startswith("title") for r in reasons)
    assert 0.0 <= fit <= 1.0


def test_fit_is_deterministic() -> None:
    one = jobs_mod.score_fit(posting(), TARGETS)
    for _ in range(3):
        assert jobs_mod.score_fit(posting(), TARGETS) == one


# -------------------------------------------------------------- board parsers


def test_parse_greenhouse_payload() -> None:
    parsed = jobs_mod.parse_board_payload("greenhouse", "webflow", GREENHOUSE_FEED)
    assert len(parsed) == 3  # the untitled entry is dropped
    top = parsed[0]
    assert top["role"] == "Staff Brand Designer"
    assert top["org"] == "webflow" and top["board"] == "greenhouse"
    assert top["url"] == "https://boards.greenhouse.io/webflow/jobs/1001"
    assert top["location"] == "Remote — US"
    assert "<" not in top["body"] and "Webflow & Framer" in top["body"]


def test_parse_lever_payload() -> None:
    parsed = jobs_mod.parse_board_payload("lever", "acme", LEVER_FEED)
    assert len(parsed) == 1
    job = parsed[0]
    assert job["role"] == "Product Designer"
    assert job["url"] == "https://jobs.lever.co/acme/111"
    assert job["location"] == "Remote — US"
    assert "Requirements" in job["body"] and "Webflow" in job["body"] and "Framer" in job["body"]


def test_parse_ashby_payload() -> None:
    parsed = jobs_mod.parse_board_payload("ashby", "acme", ASHBY_FEED)
    assert len(parsed) == 1
    job = parsed[0]
    assert job["role"] == "Design Engineer"
    assert job["url"] == "https://jobs.ashbyhq.com/acme/222"
    assert job["location"] == "Canada Remote"
    assert "Webflow" in job["body"] and "<" not in job["body"]


def test_parse_workable_payload() -> None:
    parsed = jobs_mod.parse_board_payload("workable", "acme", WORKABLE_FEED)
    assert len(parsed) == 1  # the untitled entry is dropped
    job = parsed[0]
    assert job["role"] == "Product Designer"
    assert job["org"] == "acme" and job["board"] == "workable"
    # NOT the feed's own url field — that's a 301-ing shortlink with no org.
    assert job["url"] == "https://apply.workable.com/acme/j/3A2AE898F0/"
    assert job["location"] == "Vancouver, Canada (Remote)"
    assert "<" not in job["body"] and "Webflow" in job["body"]


def test_board_url_and_unknown_board() -> None:
    assert jobs_mod.board_url("greenhouse", "Webflow").startswith(
        "https://boards-api.greenhouse.io/v1/boards/webflow/"
    )
    assert jobs_mod.board_url("lever", "acme") == "https://api.lever.co/v0/postings/acme?mode=json"
    assert jobs_mod.board_url("ashby", "acme") == "https://api.ashbyhq.com/posting-api/job-board/acme"
    assert (
        jobs_mod.board_url("workable", "Acme")
        == "https://apply.workable.com/api/v1/widget/accounts/acme?details=true"
    )
    with pytest.raises(jobs_mod.FindError):
        jobs_mod.board_url("bamboohr", "acme")
    with pytest.raises(jobs_mod.FindError):
        jobs_mod.parse_board_payload("bamboohr", "acme", {})


def test_resolve_orgs() -> None:
    configured = {"webflow": "greenhouse"}
    assert jobs_mod.resolve_orgs([], configured) == configured
    assert jobs_mod.resolve_orgs([], {}) == jobs_mod.DEFAULT_ORGS
    assert jobs_mod.resolve_orgs(["Webflow"], configured) == {"webflow": "greenhouse"}
    assert jobs_mod.resolve_orgs(["acme:lever"], configured) == {"acme": "lever"}
    with pytest.raises(jobs_mod.FindError):
        jobs_mod.resolve_orgs(["acme:bogus"], configured)
    with pytest.raises(jobs_mod.FindError):
        jobs_mod.resolve_orgs(["acme"], {})


# ----------------------------------------------------------------- find_jobs


def stub_feeds(monkeypatch: pytest.MonkeyPatch, feeds: dict[str, Any] | None = None) -> None:
    """Point the single network touchpoint at canned payloads (by URL substring)."""

    def fake_get(url: str, timeout: float = 15.0) -> Any:
        for key, payload in (feeds or {}).items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(jobs_mod, "http_get_json", fake_get)


def test_find_jobs_ranks_filters_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_feeds(monkeypatch, {"greenhouse": GREENHOUSE_FEED, "lever": LEVER_FEED})
    result = jobs_mod.find_jobs(
        {"webflow": "greenhouse", "acme": "lever"}, TARGETS, threshold=0.6, limit=10
    )
    assert result["errors"] == []
    assert result["scanned"] == 4
    assert [job["role"] for job in result["jobs"]] == ["Staff Brand Designer", "Product Designer"]
    top = result["jobs"][0]
    assert top["fit"] == 1.0 and top["reasons"]
    assert 0.7 <= result["jobs"][1]["fit"] <= 0.75

    limited = jobs_mod.find_jobs({"webflow": "greenhouse", "acme": "lever"}, TARGETS, threshold=0.6, limit=1)
    assert len(limited["jobs"]) == 1


def test_find_jobs_collects_board_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def flaky(url: str, timeout: float = 15.0) -> Any:
        if "lever" in url:
            raise RuntimeError("could not fetch: boom")
        return GREENHOUSE_FEED

    monkeypatch.setattr(jobs_mod, "http_get_json", flaky)
    result = jobs_mod.find_jobs({"webflow": "greenhouse", "acme": "lever"}, TARGETS)
    assert len(result["errors"]) == 1
    assert result["errors"][0]["org"] == "acme"
    assert result["jobs"]  # the healthy board still lands in the shortlist


def test_write_shortlist(tmp_path: Path) -> None:
    result = {
        "jobs": [
            {"role": "A", "org": "x", "board": "greenhouse", "url": "u1", "location": "", "fit": 0.9, "reasons": []},
            {"role": "B", "org": "x", "board": "greenhouse", "url": "u2", "location": "", "fit": 0.8, "reasons": []},
        ],
        "scanned": 2,
        "errors": [],
    }
    path = jobs_mod.write_shortlist(tmp_path, result, TARGETS)
    assert path == tmp_path / "jobs.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["generated_at"]
    assert [job["role"] for job in saved["jobs"]] == ["A", "B"]
    assert saved["targets"]["skills"] == TARGETS["skills"]


# ----------------------------------------------------------------------- cli


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code = cli.main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


@pytest.fixture
def find_workspace(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    config = cfg_mod.default_config()
    config["profile"] = cfg_mod.normalize_profile(
        {
            "target_roles": TARGETS["target_titles"],
            "target_skills": TARGETS["skills"],
        }
    )
    config["find"]["locations"] = TARGETS["locations"]
    config["find"]["orgs"] = {"webflow": "greenhouse", "acme": "lever"}
    cfg_mod.save_config(data, config)
    return data


def test_cli_find_writes_jobs_json(
    find_workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_feeds(monkeypatch, {"greenhouse": GREENHOUSE_FEED, "lever": LEVER_FEED})
    code, payload = run(capsys, "--data-dir", str(find_workspace), "find")

    assert code == 0
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["scanned"] == 4
    assert payload["jobs"][0]["fit"] == 1.0
    assert payload["shortlist"] == str(find_workspace / "jobs.json")

    saved = json.loads((find_workspace / "jobs.json").read_text(encoding="utf-8"))
    assert [job["role"] for job in saved["jobs"]] == [job["role"] for job in payload["jobs"]]
    assert saved["targets"]["skills"] == TARGETS["skills"]


def test_cli_find_threshold_and_limit_flags(
    find_workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_feeds(monkeypatch, {"greenhouse": GREENHOUSE_FEED})
    code, payload = run(
        capsys, "--data-dir", str(find_workspace), "find", "webflow:greenhouse", "--fit", "0.6", "--limit", "1"
    )
    assert code == 0
    assert payload["count"] == 1
    assert payload["jobs"][0]["role"] == "Staff Brand Designer"
    assert payload["orgs"] == {"webflow": "greenhouse"}


def test_cli_find_human_table(
    find_workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_feeds(monkeypatch, {"greenhouse": GREENHOUSE_FEED, "lever": LEVER_FEED})
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)  # force the human path
    code = cli.main(["--data-dir", str(find_workspace), "find"])
    out = capsys.readouterr().out

    assert code == 0
    assert "scanned 4 posting(s)" in out
    assert "Staff Brand Designer — webflow (greenhouse)" in out
    assert "https://boards.greenhouse.io/webflow/jobs/1001" in out


def test_cli_find_usage_and_network_errors(
    find_workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_feeds(monkeypatch, {})  # nothing stubbed -> every board fails

    code, payload = run(capsys, "--data-dir", str(find_workspace), "find", "nope")
    assert code == cli.EXIT_USAGE
    assert "unknown org" in payload["error"]

    def dead(url: str, timeout: float = 15.0) -> Any:
        raise RuntimeError("could not fetch: offline")

    monkeypatch.setattr(jobs_mod, "http_get_json", dead)
    code, payload = run(capsys, "--data-dir", str(find_workspace), "find")
    assert code == cli.EXIT_ERROR
    assert "all boards failed" in payload["error"]


# ------------------------------------------------------------------ live feed


@pytest.mark.live
def test_live_find_webflow() -> None:
    """Real Greenhouse feed smoke — skipped when the network is off."""
    result = jobs_mod.find_jobs({"webflow": "greenhouse"}, TARGETS, threshold=0.0, limit=5)
    if result["errors"] and not result["jobs"]:
        pytest.skip(f"network unavailable: {result['errors'][0]['error']}")
    assert result["jobs"], "webflow board returned no postings"
    for job in result["jobs"]:
        assert job["url"].startswith("http")
        assert 0.0 <= job["fit"] <= 1.0
        assert job["reasons"]


def test_jobs_add_fetch_answers_an_ashby_url_from_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Job 6 regression: scraping a jobs.ashbyhq.com page stored ~137 chars of
    JS bootstrap shell, and the lens auto-picker, starved of signal, dressed a
    Principal Product Designer role as sre. An ashby job URL must be answered
    from the public posting API (the feed `weaver find` already reads) — the
    HTML scrape is never touched."""
    from weaver import db as weaver_db

    url = "https://jobs.ashbyhq.com/acme/ba8a41d2-4198-481a-a7f4-e09c5364ff7f"
    feed = {
        "jobs": [
            {
                "title": "Principal Product Designer",
                "jobUrl": url,
                "location": "North America Remote",
                "descriptionHtml": (
                    "&lt;p&gt;Own brand systems end to end. Ship marketing pages in "
                    "Webflow and Figma. Design engineering mindset.&lt;/p&gt;"
                ),
            }
        ]
    }
    seen: dict[str, str] = {}

    def fake_get(feed_url: str, timeout: float = 15.0):
        seen["url"] = feed_url
        return feed

    def explode(*_a, **_k):  # pragma: no cover — reaching it IS the failure
        raise AssertionError("scraped the HTML shell instead of the posting API")

    monkeypatch.setattr(jobs_mod, "http_get_json", fake_get)
    monkeypatch.setattr(jobs_mod.urllib.request, "urlopen", explode)

    text = jobs_mod.fetch(url)
    assert seen["url"] == "https://api.ashbyhq.com/posting-api/job-board/acme"
    assert len(text) > 100 and "Webflow" in text and "<" not in text

    conn = weaver_db.init_db(tmp_path)
    job = jobs_mod.add(conn, url, fetch_url=True)
    assert job["title"] == "Principal Product Designer"
    assert job["company"] == "Acme"
    skills = job.get("skills_required")
    if isinstance(skills, str):
        skills = json.loads(skills)
    assert "Figma" in (skills or [])  # the tool vocabulary got real signal
    assert len(job.get("raw_text") or "") > 100


def test_jobs_add_fetch_answers_a_workable_url_from_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workable job pages are JS shells like Ashby's (~48 chars of visible
    text) — an apply.workable.com job URL must be answered from the public v2
    job API, never the HTML scrape."""
    from weaver import db as weaver_db

    url = "https://apply.workable.com/acme/j/3A2AE898F0/"
    detail = {
        "title": "Principal Product Designer",
        "shortcode": "3A2AE898F0",
        "location": {"country": "Canada", "city": "Vancouver", "display": "Vancouver, Canada"},
        "remote": True,
        "description": (
            "&lt;p&gt;Own brand systems end to end. Ship marketing pages in "
            "Webflow and Figma. Design engineering mindset.&lt;/p&gt;"
        ),
        "requirements": "&lt;p&gt;Portfolio required.&lt;/p&gt;",
        "benefits": "",
    }
    seen: dict[str, str] = {}

    def fake_get(feed_url: str, timeout: float = 15.0):
        seen["url"] = feed_url
        return detail

    def explode(*_a, **_k):  # pragma: no cover — reaching it IS the failure
        raise AssertionError("scraped the HTML shell instead of the job API")

    monkeypatch.setattr(jobs_mod, "http_get_json", fake_get)
    monkeypatch.setattr(jobs_mod.urllib.request, "urlopen", explode)

    text = jobs_mod.fetch(url)
    assert seen["url"] == "https://apply.workable.com/api/v2/accounts/acme/jobs/3A2AE898F0"
    assert len(text) > 100 and "Webflow" in text and "Portfolio" in text and "<" not in text

    conn = weaver_db.init_db(tmp_path)
    job = jobs_mod.add(conn, url, fetch_url=True)
    assert job["title"] == "Principal Product Designer"
    assert job["company"] == "Acme"
    skills = job.get("skills_required")
    if isinstance(skills, str):
        skills = json.loads(skills)
    assert "Figma" in (skills or [])
    assert len(job.get("raw_text") or "") > 100


def test_exclude_titles_veto_developer_roles_for_a_designer_profile() -> None:
    """Morning-of-Aug-16 regression: 'Senior / Staff Fullstack Engineer' and
    'Senior / Staff Product Engineer' shortlisted at 0.71 on token overlap.
    A title matching find.exclude_titles scores ZERO — unless a target title
    matches the posting title in full (explicit qualification wins)."""
    targets = {
        "target_titles": ["Product Designer", "Design Engineer", "Brand Designer"],
        "skills": ["React", "TypeScript", "Figma"],
        "seniority": ["senior", "staff"],
        "locations": ["Remote"],
        "exclude_titles": ["fullstack", "full stack", "product engineer", "backend"],
    }
    fullstack = {"role": "Senior / Staff Fullstack Engineer", "location": "Remote",
                 "body": "React TypeScript design systems product design Figma"}
    fit, reasons = jobs_mod.score_fit(fullstack, targets)
    assert fit == 0.0
    assert any("excluded" in r for r in reasons)

    product_eng = {"role": "Senior / Staff Product Engineer", "location": "Remote",
                   "body": "React TypeScript Figma"}
    fit2, reasons2 = jobs_mod.score_fit(product_eng, targets)
    assert fit2 == 0.0

    # a real design role is untouched by the veto
    designer = {"role": "Staff Product Designer", "location": "Remote",
                "body": "Figma design systems"}
    fit3, _ = jobs_mod.score_fit(designer, targets)
    assert fit3 > 0.5

    # explicit qualification beats the exclusion list
    qualified = dict(targets, target_titles=["Product Engineer"])
    fit4, _ = jobs_mod.score_fit(product_eng, qualified)
    assert fit4 > 0.0


def test_load_targets_carries_exclude_titles() -> None:
    config = {"find": {"exclude_titles": ["backend"]}, "profile": {"target_roles": ["Designer"]}}
    assert jobs_mod.load_targets(config)["exclude_titles"] == ["backend"]


def test_title_weight_never_comes_from_the_posting_body() -> None:
    """'Senior Applied Scientist, Credit Risk' scored 0.68 for a designer
    because its BODY mentioned product design — the title weight now reads
    the title only (the body still feeds the skills component)."""
    targets = {
        "target_titles": ["Product Designer"],
        "skills": ["Figma"],
        "seniority": ["senior"],
        "locations": [],
        "exclude_titles": [],
    }
    stuffed = {"role": "Senior Applied Scientist, Credit Risk", "location": "Remote",
               "body": "You will partner with product design and designer teams. Figma."}
    fit, reasons = jobs_mod.score_fit(stuffed, targets)
    assert not any(r.startswith("title") for r in reasons)
    assert fit <= 0.5  # skills + seniority at most — never the title weight

    titled = {"role": "Senior Product Designer", "location": "Remote", "body": "Figma."}
    fit2, _ = jobs_mod.score_fit(titled, targets)
    assert fit2 > fit
