"""Guardrail: unverifiable claims must surface, real ones must not."""

from __future__ import annotations

import copy
import sqlite3

from weaver import guardrail
from weaver import lenses as lenses_mod
from weaver import tailor as tailor_mod

PROFILE = {
    "name": "Mira Halloway",
    "email": "mira@halloway.example",
    "phone": "+1 (503) 555-0148",
    "location": "Portland, OR",
    "links": ["mirahalloway.example"],
}


def tailored(graph: sqlite3.Connection, lens_name: str = "fde") -> dict:
    lens = lenses_mod.get(graph, lens_name)
    return tailor_mod.tailor(graph, lens, PROFILE)


def test_deterministic_output_is_clean(graph: sqlite3.Connection) -> None:
    for lens_name in ("fde", "fdc", "design-engineer", "multimedia", "creative", "sre"):
        result = tailored(graph, lens_name)
        assert result["unverified_mentions"] == [], (lens_name, result["unverified_mentions"])


def test_invented_org_date_and_metric_are_flagged(graph: sqlite3.Connection) -> None:
    result = tailored(graph)
    structure = copy.deepcopy(result["structure"])
    structure["experience"][0]["bullets"].append(
        "Led platform work at Umbrella Dynamics in 1998, lifting conversion 420%."
    )

    corpus = guardrail.build_corpus(graph, extra=[PROFILE["name"]])
    findings = guardrail.scan(structure, corpus, skip_paths=("contact", "name"))

    flagged = {(f["type"], f["text"]) for f in findings}
    assert ("org", "Umbrella Dynamics") in flagged
    assert ("date", "1998") in flagged
    assert ("metric", "420%") in flagged
    assert all(f["where"].startswith("experience[0].bullets") for f in findings)


def test_domain_labels_and_tools_are_not_flagged_as_orgs(graph: sqlite3.Connection) -> None:
    corpus = guardrail.build_corpus(graph)
    structure = {
        "skills": [
            {"domain": "Full-Stack Engineering", "items": ["TypeScript"]},
            {"domain": "Direction & Delivery", "items": ["Creative direction"]},
            {"domain": "Cloud & Reliability", "items": ["Blender", "Figma"]},
            {"domain": "AI & Machine Learning", "items": ["Stable Diffusion"]},
        ]
    }
    assert guardrail.scan(structure, corpus) == []


def test_known_facts_survive_rewording(graph: sqlite3.Connection) -> None:
    corpus = guardrail.build_corpus(graph)
    structure = {
        "experience": [
            {
                "role": "Forward Deployed Engineer",
                "org": "Northwind Atlas",
                "dates": "2025 – Present",
                "bullets": ["Owned the mobile build end-to-end at Northwind Atlas."],
            }
        ]
    }
    assert guardrail.scan(structure, corpus) == []


def test_contact_block_is_skipped(graph: sqlite3.Connection) -> None:
    corpus = guardrail.build_corpus(graph)
    structure = {"contact": {"email": "someone@nowhere.example"}, "name": "Anonymous Person"}

    assert guardrail.scan(structure, corpus, skip_paths=("contact", "name")) != None
    assert guardrail.scan(structure, corpus, skip_paths=("contact", "name")) == []
    assert guardrail.scan(structure, corpus) != []


def test_walk_strings_reports_paths() -> None:
    pairs = dict(guardrail.walk_strings({"a": ["x", {"b": "y"}]}))
    assert pairs["a[0]"] == "x"
    assert pairs["a[1].b"] == "y"


def test_summarize_reads_clean_or_lists_findings() -> None:
    assert "clean" in guardrail.summarize([])
    text = guardrail.summarize([{"type": "org", "text": "Umbrella Dynamics", "where": "x"}])
    assert "1 unverified" in text
    assert "Umbrella Dynamics" in text
