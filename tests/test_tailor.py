"""Tailor engine: the deterministic fallback, lens flex, and dedupe."""

from __future__ import annotations

import sqlite3

import pytest

from weaver import db, jobs as jobs_mod, llm, textutil
from weaver import lenses as lenses_mod
from weaver import tailor as tailor_mod

PROFILE = {
    "name": "Mira Halloway",
    "email": "mira@halloway.example",
    "phone": "+1 (503) 555-0148",
    "location": "Portland, OR",
    "links": ["mirahalloway.example", "linkedin.com/in/mirahalloway"],
}


def build(graph: sqlite3.Connection, lens_name: str, job=None) -> dict:
    lens = lenses_mod.get(graph, lens_name)
    assert lens is not None
    return tailor_mod.tailor(graph, lens, PROFILE, job=job)


def test_no_key_uses_deterministic_fallback(graph: sqlite3.Connection) -> None:
    assert llm.api_key() is None

    result = build(graph, "fde")

    assert result["provider"] == "deterministic"
    assert "no api key" in (result["fallback_reason"] or "")
    assert result["structure"]["experience"]
    assert result["source_facts"]


def test_llm_failure_falls_back_without_raising(
    graph: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(
        llm, "complete_json", lambda system, user: llm.deterministic_fallback("boom")
    )

    result = build(graph, "fde")

    assert result["provider"] == "deterministic"
    assert result["fallback_reason"] == "boom"
    assert result["structure"]["experience"]


def test_invalid_llm_json_is_rejected(
    graph: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(llm, "complete_json", lambda system, user: {"experience": "not a list"})

    result = build(graph, "fde")

    assert result["provider"] == "deterministic"
    assert "validation" in (result["fallback_reason"] or "")


def test_lens_changes_the_story_not_the_facts(graph: sqlite3.Connection) -> None:
    fde = build(graph, "fde")["structure"]
    multimedia = build(graph, "multimedia")["structure"]

    assert fde["title"] == "Forward Deployed AI Engineer"
    assert multimedia["title"] == "Full Stack Multi-Media"

    # Same orgs available to both; emphasis differs.
    assert fde["experience"][0]["org"] != multimedia["experience"][0]["org"]
    assert fde["skills"][0]["domain"] != multimedia["skills"][0]["domain"]
    assert multimedia["skills"][0]["domain"] == "Video & Multimedia"

    known_orgs = {f["org"] for f in db.get_facts(graph, ["role"])}
    for structure in (fde, multimedia):
        for entry in structure["experience"]:
            assert entry["org"] in known_orgs


def test_title_variant_follows_the_lens(graph: sqlite3.Connection) -> None:
    weights = tailor_mod.lens_weights(lenses_mod.get(graph, "multimedia"))
    role = {
        "title": "AI Engineering Lead",
        "aliases": ["Multimedia Production, AI & CGI Lead"],
    }
    assert tailor_mod.pick_title(role, weights) == "Multimedia Production, AI & CGI Lead"

    weights = tailor_mod.lens_weights(lenses_mod.get(graph, "fde"))
    assert tailor_mod.pick_title(role, weights) == "AI Engineering Lead"


def test_bullets_are_deduped_within_and_across_roles(graph: sqlite3.Connection) -> None:
    structure = build(graph, "multimedia")["structure"]

    all_bullets = [b for entry in structure["experience"] for b in entry["bullets"]]
    assert all_bullets
    for i, first in enumerate(all_bullets):
        for second in all_bullets[i + 1 :]:
            assert not textutil.is_near_duplicate(first, second), (first, second)


def test_select_bullets_collapses_restatements() -> None:
    weights = {d: 1.0 for d in tailor_mod.DOMAIN_NAMES}
    bullets = [
        "Delivered AI concept visualization for the Super Bowl campaign, producing hybrid teasers.",
        "Delivered AI concept visualization for the Super Bowl campaign with the creative team.",
        "Directed motion design and video editing across retail and DTC channels.",
    ]
    chosen = tailor_mod._select_bullets(bullets, weights, limit=6)
    assert len(chosen) == 2


def test_summary_is_a_single_paragraph(graph: sqlite3.Connection) -> None:
    assert len(db.get_facts(graph, ["summary"])) >= 2

    fde = build(graph, "fde")["structure"]["summary"]
    multimedia = build(graph, "multimedia")["structure"]["summary"]

    assert "\n\n" not in fde
    assert "\n\n" not in multimedia
    assert fde != multimedia
    assert "orchestrating AI coding agents" in fde
    assert "concept board" in multimedia


def test_summaries_come_from_the_graph(graph: sqlite3.Connection) -> None:
    known = {f["title"] for f in db.get_facts(graph, ["summary"])}
    assert build(graph, "creative")["structure"]["summary"] in known


def test_contact_links_are_deduped_by_host() -> None:
    block = tailor_mod._contact_block(
        {
            "links": [
                "linkedin.com/in/one",
                "linkedin.com/in/two",
                "mirahalloway.example",
                "https://linkedin.com/in/three",
            ]
        }
    )
    assert block["links"] == ["linkedin.com/in/one", "mirahalloway.example"]


def test_job_biases_the_build(graph: sqlite3.Connection, sample_job) -> None:
    job = jobs_mod.add(graph, str(sample_job))
    assert job["title"] == "Forward Deployed AI Engineer"
    assert job["company"] == "Verdant Systems"
    assert job["skills_required"]

    result = build(graph, "fde", job=job)

    assert result["job_id"] == job["id"]
    assert result["structure"]["experience"]
    assert result["unverified_mentions"] == []


def test_max_roles_is_respected(graph: sqlite3.Connection) -> None:
    lens = lenses_mod.get(graph, "fde")
    result = tailor_mod.tailor(graph, lens, PROFILE, max_roles=2)
    assert len(result["structure"]["experience"]) == 2
