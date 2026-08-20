"""Renderers: markdown template and the docx styled like the originals."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from weaver import lenses as lenses_mod, render
from weaver import tailor as tailor_mod

PROFILE = {
    "name": "Mira Halloway",
    "email": "mira@halloway.example",
    "phone": "+1 (503) 555-0148",
    "location": "Portland, OR",
    "links": ["mirahalloway.example"],
}

STRUCTURE = {
    "name": "Mira Halloway",
    "title": "Full Stack Multi-Media",
    "contact": {
        "email": "mira@halloway.example",
        "phone": "+1 (503) 555-0148",
        "location": "Portland, OR",
        "links": ["mirahalloway.example"],
    },
    "summary": "Multimedia engineer who ships the whole campaign.",
    "experience": [
        {
            "role": "Multimedia Production Lead",
            "org": "Kestrel Labs",
            "dates": "2022 – 2024",
            "location": "Remote",
            "bullets": ["Directed CGI across the catalog.", "Shipped 400+ assets."],
        }
    ],
    "skills": [{"domain": "Video & Multimedia", "items": ["Storyboarding", "Editing"]}],
    "education": [
        {"degree": "Bachelor of Media Arts", "school": "Cascade Institute", "year": "2019"}
    ],
    "awards": ["Gold, Pinegrove Interactive Awards 2023"],
    "clients": ["Northwind Atlas", "Halcyon Studio"],
}


def test_render_markdown_has_every_section() -> None:
    text = render.render_markdown(STRUCTURE)

    assert text.startswith("# Mira Halloway")
    assert "**Full Stack Multi-Media**" in text
    assert "## Profile" in text
    assert "## Experience" in text
    assert "### Multimedia Production Lead — Kestrel Labs" in text
    assert "- Directed CGI across the catalog." in text
    assert "**Video & Multimedia:**  Storyboarding, Editing" in text
    assert "## Education" in text
    assert "## Awards & Recognition" in text
    assert "Northwind Atlas, Halcyon Studio." in text
    assert "\n\n\n" not in text


def test_render_markdown_survives_a_sparse_structure() -> None:
    text = render.render_markdown({"name": "Nobody", "experience": []})
    assert text.strip() == "# Nobody"


def test_render_docx_round_trips(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    path = render.render(STRUCTURE, tmp_path / "out.docx", "docx")

    assert path.exists() and path.stat().st_size > 0

    document = docx.Document(str(path))
    text = [p.text for p in document.paragraphs]

    assert "Mira Halloway" in text
    assert "Full Stack Multi-Media" in text
    assert any("EXPERIENCE" == line for line in text)
    assert any("Kestrel Labs" in line and "2022 – 2024" in line for line in text)
    assert any("Directed CGI across the catalog." in line for line in text)
    assert any("Video & Multimedia:" in line for line in text)

    name_run = document.paragraphs[0].runs[0]
    assert name_run.font.name == "Arial"
    assert name_run.bold is True
    assert name_run.font.size.pt == 17


def test_render_rejects_unknown_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        render.render(STRUCTURE, tmp_path / "out.rtf", "rtf")


def test_resume_html_carries_every_section() -> None:
    """The pdf's HTML is built from the same structure the markdown reads."""
    doc = render.resume_html(STRUCTURE)

    assert doc.startswith("<!DOCTYPE html>")
    assert "Mira Halloway" in doc
    assert "Full Stack Multi-Media" in doc
    assert 'href="mailto:mira@halloway.example"' in doc
    assert 'href="https://mirahalloway.example"' in doc
    assert "Profile</h2>" in doc and "Experience</h2>" in doc
    assert "Kestrel Labs" in doc and "2022 – 2024" in doc
    assert "<li>Directed CGI across the catalog.</li>" in doc
    assert "Video &amp; Multimedia:" in doc
    assert "Cascade Institute" in doc
    assert "Northwind Atlas, Halcyon Studio." in doc


def test_resume_html_escapes_structure_text() -> None:
    doc = render.resume_html({"name": "A <b>& Co</b>", "summary": "5 > 3 & rising"})
    assert "<b>" not in doc
    assert "A &lt;b&gt;&amp; Co&lt;/b&gt;" in doc
    assert "5 &gt; 3 &amp; rising" in doc


def test_render_pdf_writes_a_real_pdf(tmp_path: Path) -> None:
    pytest.importorskip("playwright")
    path = render.render(STRUCTURE, tmp_path / "out.pdf", "pdf")

    assert path.exists()
    assert path.stat().st_size > 1024
    with path.open("rb") as handle:
        assert handle.read(4) == b"%PDF"


def test_default_filename_keeps_the_pdf_extension() -> None:
    assert render.default_filename(
        "Mira Halloway", "design-engineer", "pdf", "20260101-000000"
    ) == "Mira_Halloway_design_engineer_20260101-000000.pdf"


def test_default_filename_is_filesystem_safe() -> None:
    name = render.default_filename("Mira Halloway", "design-engineer", "docx", "20260101-000000")
    assert name == "Mira_Halloway_design_engineer_20260101-000000.docx"


def test_end_to_end_render_from_graph(graph: sqlite3.Connection, tmp_path: Path) -> None:
    lens = lenses_mod.get(graph, "multimedia")
    result = tailor_mod.tailor(graph, lens, PROFILE)

    md = render.render(result["structure"], tmp_path / "resume.md", "md")
    docx_path = render.render(result["structure"], tmp_path / "resume.docx", "docx")

    body = md.read_text(encoding="utf-8")
    assert "# Mira Halloway" in body
    assert "Kestrel Labs" in body
    assert docx_path.stat().st_size > 0


def test_job_linked_resume_is_named_for_the_job() -> None:
    """The resume file's name is the first thing a recruiter
    reads — a job-linked tailor is named for the job title and company, not
    for the lens and a timestamp."""
    from weaver import render

    named = render.default_filename(
        "Mira Halloway", "design-engineer", "docx", "20260816-013615",
        job={"title": "Senior Brand Designer | Growth", "company": "Verdant Systems"},
    )
    assert named == "Mira_Halloway_Resume_Senior_Brand_Designer_Growth_Verdant_Systems.docx"

    jobless = render.default_filename("Mira Halloway", "design-engineer", "md", "20260816-013615")
    assert jobless == "Mira_Halloway_design_engineer_20260816-013615.md"


def test_a_url_title_never_becomes_the_resume_filename() -> None:
    """Morning-of-Aug-16 regression: an unfetched job keeps its URL as the
    title, and the filename builder slugged it into
    `..._Resume_https_jobs_ashbyhq_com_linear_cd5a....pdf`. A URL is an
    address, never a name — the jobless lens+stamp name is the fallback."""
    from weaver import render

    named = render.default_filename(
        "Mira Halloway", "creative", "pdf", "20260816-090000",
        job={"title": "https://jobs.ashbyhq.com/linear/cd5ae036-0223", "company": ""},
    )
    assert named == "Mira_Halloway_creative_20260816-090000.pdf"
    assert "https" not in named


def test_ats_job_application_prefix_is_removed_from_filename() -> None:
    from weaver import render

    named = render.default_filename(
        "Leo Felix", "product-designer", "pdf", "20260817-090000",
        job={
            "title": "Job Application for Senior Product Designer AI",
            "company": "GitLab",
        },
    )
    assert named == "Leo_Felix_Resume_Senior_Product_Designer_AI_GitLab.pdf"
    assert "Job_Application" not in named
