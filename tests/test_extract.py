"""Extraction: documents in, canonical facts out."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from weaver import db, extract


def test_read_markdown_marks_bullets_and_headings(sample_resume: Path) -> None:
    lines = extract.read_lines(sample_resume)
    assert lines[0].text == "MIRA HALLOWAY"
    assert any(line.is_bullet for line in lines)
    assert any(extract.section_of(line.text) == "experience" for line in lines)


def test_parse_document_extracts_roles(sample_resume: Path) -> None:
    doc = extract.parse_document(extract.read_lines(sample_resume), sample_resume.name)

    assert doc.name == "Mira Halloway"
    assert doc.headline == "Full Stack Multimedia Engineer"
    assert len(doc.roles) == 5

    first = doc.roles[0]
    assert first.title == "Forward Deployed Engineer"
    assert first.org == "Northwind Atlas"
    assert first.start == "2025"
    assert first.end == "Present"
    assert len(first.bullets) == 4
    assert all(len(b) > 20 for b in first.bullets)


def test_markdown_role_heading_is_not_a_section(sample_resume: Path) -> None:
    """'### Title · Org (2019 – 2021)' is short enough to look like a heading."""
    doc = extract.parse_document(extract.read_lines(sample_resume), sample_resume.name)
    orgs = {role.org for role in doc.roles}
    assert "Pinegrove Collective" in orgs


def test_parse_role_line_variants() -> None:
    role, trailing = extract.parse_role_line(
        "Lead Multimedia Engineer  ·  Northwind Atlas\tJan 2025 – Jan 2026"
    )
    assert role is not None
    assert (role.title, role.org, role.start, role.end) == (
        "Lead Multimedia Engineer",
        "Northwind Atlas",
        "Jan 2025",
        "Jan 2026",
    )
    assert trailing == ""

    # Inline 'earlier experience' one-liner: dates in parens, prose after.
    role, trailing = extract.parse_role_line(
        "Motion Designer  |  Kestrel Labs  (2019–2021).  Built motion systems for launch."
    )
    assert role is not None
    assert role.org == "Kestrel Labs"
    assert (role.start, role.end) == ("2019", "2021")
    assert trailing.startswith("Built motion systems")

    # Title continues past the first separator; org is the last segment.
    role, _ = extract.parse_role_line("AI Lead & Project Manager — CGI Specialist | Kestrel Labs")
    assert role is not None
    assert role.org == "Kestrel Labs"
    assert "CGI Specialist" in role.title

    assert extract.parse_role_line("Built a thing that did a job for someone")[0] is None


def test_metric_and_project_extraction() -> None:
    bullets = [
        'Contributed to "Tidewater Bloom," the first national broadcast spot.',
        "Shipped product imagery at 98.4% fidelity across 400+ production assets.",
        "For Marlin Goods, a $6M ecommerce brand, grew revenue ~$18K through email.",
    ]
    metrics = {m for b in bullets for m in extract.extract_metrics(b)}
    assert "98.4%" in metrics
    assert "400+" in metrics
    assert "$6M" in metrics

    projects = dict(extract.extract_projects(bullets))
    assert "Tidewater Bloom" in projects


def test_dates_and_sort_key() -> None:
    assert extract.parse_dates("Jan 2025 – Present") == ("Jan 2025", "Present")
    assert extract.parse_dates("2024 – 2025") == ("2024", "2025")
    assert extract.sort_key_for("May 2026", "Present") == "2026-05"
    assert extract.sort_key_for("2021", "2022") == "2021-00"


def test_import_directory_builds_graph(conn: sqlite3.Connection, samples_dir: Path) -> None:
    result = extract.import_directory(conn, samples_dir, use_llm=False)

    assert result["errors"] == []
    assert any(entry["file"] == "sample-job.txt" for entry in result["skipped"])

    kinds = db.stats(conn)["facts_by_kind"]
    for kind in ("role", "skill", "award", "client", "education", "summary"):
        assert kinds.get(kind, 0) > 0, f"no {kind} facts imported"

    roles = db.get_facts(conn, ["role"])
    assert len(roles) == 5
    assert all(role["bullets"] for role in roles)
    assert all(role["fingerprint"].startswith("role|") for role in roles)


def test_reimport_merges_instead_of_duplicating(
    conn: sqlite3.Connection, samples_dir: Path
) -> None:
    extract.import_directory(conn, samples_dir, use_llm=False)
    before = len(db.get_facts(conn, ["role"]))
    bullets_before = sum(len(r["bullets"]) for r in db.get_facts(conn, ["role"]))

    extract.import_directory(conn, samples_dir, use_llm=False)

    assert len(db.get_facts(conn, ["role"])) == before
    assert sum(len(r["bullets"]) for r in db.get_facts(conn, ["role"])) == bullets_before


def test_education_and_profile(graph: sqlite3.Connection, samples_dir: Path) -> None:
    education = db.get_facts(graph, ["education"])
    assert len(education) == 1
    assert "Cascade Institute" in (education[0]["org"] or "")
    assert education[0]["end"] == "2019"
    assert "Graduated" not in (education[0]["title"] or "")


def test_looks_like_job_posting(sample_job: Path, sample_resume: Path) -> None:
    assert extract.looks_like_job_posting(sample_job)
    assert not extract.looks_like_job_posting(sample_resume)


def test_unreadable_file_does_not_abort_import(
    conn: sqlite3.Connection, tmp_path: Path, sample_resume: Path
) -> None:
    directory = tmp_path / "seeds"
    directory.mkdir()
    (directory / "sample-resume.md").write_text(
        sample_resume.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (directory / "broken.docx").write_bytes(b"this is not a docx")

    result = extract.import_directory(conn, directory, use_llm=False)

    assert len(result["errors"]) == 1
    assert result["errors"][0]["file"] == "broken.docx"
    assert db.get_facts(conn, ["role"])
