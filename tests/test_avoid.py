"""The do-not-mention list and the never-volunteer-compensation rule.

Two rules the applicant sets and the engine enforces in CODE, the same way the
PII rule is enforced — a prompt that asks nicely is not the guarantee:

- `profile.avoid`: topics/employers/keywords that must never appear in anything
  written for an application — typically an early job the applicant does not
  want the story to run through.
- compensation: a salary/rate number is never volunteered — only a field that
  explicitly asks for one may carry it. (From a live catch: a model wrote "so
  excited to make them $29K" into an essay — unremarkable, and underselling.)

The names here are fixtures; nobody's real avoid list lives in this repo (it is
per-user config, gitignored).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from weaver import config as cfg
from weaver import db, guardrail, local_agent
from weaver import lenses as lenses_mod
from weaver import payload as payload_lib
from weaver import tailor as tailor_mod

from test_local_agent import APPLICANT, JOB, StubDriver, field, notes, run, scripted

AVOID = ["Dreamcast"]

PROFILE = {
    "name": "Mira Halloway",
    "email": "mira@halloway.example",
    "location": "Portland, OR",
    "links": ["mirahalloway.example"],
    "avoid": AVOID,
}


# ----------------------------------------------------------------- the matcher


def test_a_mention_is_a_hit_in_any_casing_or_shape() -> None:
    assert guardrail.avoid_hits("Built the pipeline at Dreamcast.", AVOID) == ["Dreamcast"]
    assert guardrail.avoid_hits("two years at DREAMCAST", AVOID) == ["Dreamcast"]
    assert guardrail.avoid_hits("Dreamcast's render farm", AVOID) == ["Dreamcast"]
    assert guardrail.mentions_avoided("dreamcast", AVOID) is True


def test_dream_is_not_a_dreamcast_mention() -> None:
    """The entries are short; a whole-word match is what keeps them usable."""
    for innocent in (
        "A dream role for a design engineer.",
        "Dreaming up new interfaces daily.",
        "Dreamt of shipping this for years.",
        "dreamcaster",
    ):
        assert guardrail.avoid_hits(innocent, AVOID) == [], innocent


def test_an_empty_list_never_matches() -> None:
    assert guardrail.avoid_hits("Dreamcast", []) == []
    assert guardrail.avoid_hits("Dreamcast", None) == []
    assert guardrail.avoid_terms([" Dreamcast ", "dreamcast", "", None]) == ["Dreamcast"]


def test_multi_word_entries_tolerate_whitespace() -> None:
    assert guardrail.avoid_hits("worked at Blue\n  Harbor Media", ["Blue Harbor Media"]) == [
        "Blue Harbor Media"
    ]


def test_scan_avoid_reports_where_the_mention_lives() -> None:
    structure = {"summary": "Shipped CGI at Dreamcast.", "awards": ["Local award"]}
    findings = guardrail.scan_avoid(structure, AVOID)
    assert findings == [{"type": "avoid", "text": "Dreamcast", "where": "summary"}]


# ------------------------------------------------------------------ the config


def test_profile_carries_an_avoid_list() -> None:
    assert cfg.DEFAULT_PROFILE["avoid"] == []
    normalized = cfg.normalize_profile({"avoid": "Dreamcast, Old Studio"})
    assert normalized["avoid"] == ["Dreamcast", "Old Studio"]
    assert cfg.normalize_profile({})["avoid"] == []


# ------------------------------------------------------------------ the adapter


def _dreamcast_role(conn: sqlite3.Connection) -> None:
    db.upsert_fact(
        conn,
        {
            "kind": "role",
            "title": "Motion Designer",
            "org": "Dreamcast",
            "start": "2014-01",
            "end": "2016-01",
            "location": "Remote",
            "bullets": ["Cut promo spots for Dreamcast's regional clients."],
            "tags": ["graphics_brand"],
            "sort_key": "2014-01",
            "fingerprint": "role:dreamcast:motion-designer",
        },
    )


def test_adapt_omits_an_avoided_employer_that_is_in_the_facts(graph: sqlite3.Connection) -> None:
    _dreamcast_role(graph)
    lens = lenses_mod.get(graph, "creative")

    kept = tailor_mod.tailor(graph, lens, {**PROFILE, "avoid": []})
    assert any(e["org"] == "Dreamcast" for e in kept["structure"]["experience"])

    result = tailor_mod.tailor(graph, lens, PROFILE)
    rendered = " ".join(text for _p, text in guardrail.walk_strings(result["structure"]))
    assert guardrail.avoid_hits(rendered, AVOID) == []
    assert not any(e["org"] == "Dreamcast" for e in result["structure"]["experience"])
    assert result["avoided"] == ["Dreamcast"]
    # The rest of the career survives — this is a removal, not a truncation.
    assert len(result["structure"]["experience"]) == len(kept["structure"]["experience"]) - 1


def test_redaction_keeps_the_clean_half_of_a_paragraph() -> None:
    structure = {
        "name": "Mira Halloway",
        "summary": "Design engineer who ships. Spent two years at Dreamcast. Now leads a team.",
        "experience": [
            {
                "role": "Design Engineer",
                "org": "Northwind Atlas",
                "bullets": ["Owned the mobile build.", "Ported the Dreamcast pipeline."],
            }
        ],
        "skills": [{"domain": "Graphics & Brand", "items": ["Blender", "Dreamcast tooling"]}],
    }
    out = guardrail.redact_avoided(structure, AVOID)
    assert out["summary"] == "Design engineer who ships. Now leads a team."
    assert out["experience"][0]["bullets"] == ["Owned the mobile build."]
    assert out["skills"][0]["items"] == ["Blender"]
    assert out["name"] == "Mira Halloway"


def test_the_adapt_prompt_carries_the_list_and_the_pay_rule(graph: sqlite3.Connection) -> None:
    lens = lenses_mod.get(graph, "creative")
    prompt = tailor_mod.build_prompt(graph, lens, PROFILE)
    assert "NEVER mention or imply: Dreamcast" in prompt
    assert "NEVER volunteer a compensation number" in prompt

    clean = tailor_mod.build_prompt(graph, lens, {**PROFILE, "avoid": []})
    assert "Dreamcast" not in clean
    assert "NEVER volunteer a compensation number" in clean


# ------------------------------------------------------------------- the apply


def test_the_apply_prompt_carries_the_list_and_the_pay_rule() -> None:
    prompt = local_agent.constraints_prompt(AVOID)
    assert "NEVER mention or imply: Dreamcast" in prompt
    assert "NEVER volunteer a compensation number" in prompt
    assert local_agent.constraints_prompt([]).strip() == guardrail.COMPENSATION_RULE

    driver = StubDriver([field("f0", "First name")])
    chat = scripted([{"action": "stop", "text": "done looking"}])
    run(driver, chat, avoid=AVOID)
    assert "NEVER mention or imply: Dreamcast" in chat.calls[0]["system"]  # type: ignore[attr-defined]


def test_an_avoided_topic_is_refused_even_though_it_is_a_real_fact() -> None:
    """The avoid list outranks the applicant's own record — that is the point."""
    applicant = {**APPLICANT, "work_experience": [{"company": "Dreamcast"}]}
    driver = StubDriver([field("f0", "Why do you want this role?", tag="textarea")])
    result = run(
        driver,
        scripted([{"action": "type", "target": "f0", "text": "I ran the pipeline at Dreamcast."}]),
        applicant=applicant,
        avoid=AVOID,
    )
    assert driver.value("f0") == ""
    assert "do-not-mention" in notes(result)
    assert "Dreamcast" in notes(result)


def test_without_the_list_the_same_text_types_fine() -> None:
    applicant = {**APPLICANT, "work_experience": [{"company": "Dreamcast"}]}
    driver = StubDriver([field("f0", "Why do you want this role?", tag="textarea")])
    run(
        driver,
        scripted([{"action": "type", "target": "f0", "text": "Dreamcast"}]),
        applicant=applicant,
    )
    assert driver.value("f0") == "Dreamcast"


def test_a_dream_answer_is_not_refused() -> None:
    driver = StubDriver([field("f0", "How did you hear about us?")])
    result = run(
        driver,
        scripted([{"action": "type", "target": "f0", "text": "LinkedIn"}]),
        applicant={**APPLICANT, "how_did_you_hear": "LinkedIn — a dream role"},
        avoid=AVOID,
    )
    assert driver.value("f0") == "LinkedIn"
    assert "do-not-mention" not in notes(result)


def test_the_payload_carries_the_list_outside_the_applicant_block(
    graph: sqlite3.Connection,
) -> None:
    resume = {"id": 1, "path": "/tmp/r.pdf", "format": "pdf", "lens": "creative",
              "structure": {"name": "Mira Halloway", "contact": {}, "experience": []}}
    payload = payload_lib.build_payload(graph, resume, PROFILE, None)
    assert payload["parameters"]["avoid"] == AVOID
    # In `applicant` it would read as a declared datum and be typed happily.
    assert "Dreamcast" not in str(payload["parameters"]["applicant"])
    assert payload_lib.build_request(payload)["avoid"] == AVOID


# ----------------------------------------------------------- compensation rule


def test_salary_shapes_are_compensation_and_costs_are_not() -> None:
    assert guardrail.compensation_amounts("so excited to make them $29K") == ["$29K"]
    assert guardrail.compensation_amounts("base was $120,000") == ["$120,000"]
    assert guardrail.compensation_amounts("billed $95/hr") == ["$95/hr"]
    assert guardrail.compensation_amounts("current salary 85k") == ["85k"]

    assert guardrail.compensation_amounts("costs $10 to run") == []
    assert guardrail.compensation_amounts("cut spend by $8 per build") == []
    assert guardrail.compensation_amounts("grew the channel to 100k subscribers") == []
    assert guardrail.mentions_compensation("a $4 coffee") is False


def test_a_field_that_asks_for_pay_is_recognised() -> None:
    assert guardrail.asks_compensation("Desired salary") is True
    assert guardrail.asks_compensation("Expected compensation (USD)") is True
    assert guardrail.asks_compensation(None, "What is your hourly rate?") is True
    assert guardrail.asks_compensation("Why do you want this role?") is False


def test_an_essay_that_volunteers_a_salary_is_refused() -> None:
    driver = StubDriver([field("f0", "Why do you want this role?", tag="textarea")])
    result = run(
        driver,
        scripted(
            [
                {
                    "action": "type",
                    "target": "f0",
                    "text": (
                        "I am Mira Halloway and I would be so excited to make them $29K "
                        "the way I did for the last team I joined at Verdant."
                    ),
                }
            ]
        ),
    )
    assert driver.value("f0") == ""
    assert "compensation number" in notes(result)
    assert "$29K" in notes(result)


def test_a_salary_field_may_carry_the_number() -> None:
    driver = StubDriver([field("f0", "Desired salary (USD)")])
    result = run(
        driver,
        scripted([{"action": "type", "target": "f0", "text": "$150,000"}]),
        applicant={**APPLICANT, "desired_salary": "$150,000"},
    )
    assert driver.value("f0") == "$150,000"
    assert "compensation number" not in notes(result)


def test_a_declared_metric_is_not_a_salary_leak() -> None:
    """"$2M in revenue" is the applicant's own achievement, not their pay."""
    applicant = {
        **APPLICANT,
        "work_experience": [{"highlights": ["Drove $2M in new revenue for the studio."]}],
    }
    action = {"action": "type", "target": "f0", "text": "Drove $2M in new revenue for the studio."}
    assert local_agent.volunteered_compensation(None, action, applicant) == []
    assert local_agent.volunteered_compensation(None, action, APPLICANT) == ["$2M"]


def test_a_declared_salary_expectation_answers_the_field_that_asks(
    graph: sqlite3.Connection,
) -> None:
    """Job 71 parked held on "desired salary" three attempts running: the
    figure sat in the profile and never reached the applicant record, so the
    typed-value guard (rightly) refused a number it had never been given."""
    applicant = payload_lib.applicant_from_profile(
        {**PROFILE, "salary_expectation": "$120,000"}, PROFILE["links"]
    )
    assert applicant["salary_expectation"] == "$120,000"

    driver = StubDriver([field("f0", "Desired salary (USD)")])
    result = run(
        driver,
        scripted([{"action": "type", "target": "f0", "text": "$120,000"}]),
        applicant={**APPLICANT, "salary_expectation": "$120,000"},
    )
    assert driver.value("f0") == "$120,000"
    assert "compensation number" not in notes(result)

    # …and without the declared fact the same number is still refused as a
    # value the applicant never gave.
    bare = StubDriver([field("f0", "Desired salary (USD)")])
    refused = run(bare, scripted([{"action": "type", "target": "f0", "text": "$120,000"}]))
    assert bare.value("f0") == ""
    assert "applicant" in notes(refused).lower()


def test_the_declared_salary_is_never_quotable_outside_a_pay_field() -> None:
    """Declaring the figure must not turn it into ordinary applicant data:
    the declared-datum carve-out (which lets "$2M in revenue" through) skips
    the applicant's own pay facts, so only a field that ASKS may carry it."""
    applicant = {**APPLICANT, "salary_expectation": "$120,000"}
    action = {
        "action": "type",
        "target": "f0",
        "text": "At Verdant I was making $120,000 and would love to keep growing here.",
    }
    assert local_agent.volunteered_compensation(None, action, applicant) == ["$120,000"]

    driver = StubDriver([field("f0", "Why do you want this role?", tag="textarea")])
    result = run(driver, scripted([action]), applicant=applicant)
    assert driver.value("f0") == ""
    assert "compensation number" in notes(result)
    assert "$120,000" in notes(result)


def test_a_cost_sentence_is_not_refused() -> None:
    driver = StubDriver([field("f0", "What are you good at?", tag="textarea")])
    text = (
        "Mira Halloway builds render pipelines that cost $10 to run per shot, "
        "which is how the Verdant Systems team shipped weekly."
    )
    applicant: dict[str, Any] = {
        **APPLICANT,
        "summary": text,
    }
    result = run(driver, scripted([{"action": "type", "target": "f0", "text": text}]), applicant=applicant)
    assert driver.value("f0") == text
    assert "compensation number" not in notes(result)
