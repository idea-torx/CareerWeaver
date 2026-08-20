"""Preflight must see through custom-domain Greenhouse embeds.

2026-08-19 live test #1: two Brex postings (`brex.com/careers/…?gh_jid=…`)
preflighted as `unknown` — the generic scraper found no questions — and both
burned a browser session on pages the engine never filled. The gh_jid param
plus one HTML fetch resolves the same public boards API the native path uses.
"""

from __future__ import annotations

from typing import Any

import pytest

from weaver import payload, preflight

EMBED_HTML = (
    "<html><body><div id='grnhse_app'></div><script src="
    "'https://boards.greenhouse.io/embed/job_app?for=brex&token=8198574002'>"
    "</script></body></html>"
)

QUESTIONS_JSON = {
    "questions": [
        {"label": "First Name", "required": True},
        {"label": "Security clearance?", "required": True},
        {"label": "Portfolio", "required": False},
    ]
}


def test_a_custom_domain_embed_resolves_to_the_boards_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[str] = []

    def fake_fetch(url: str) -> dict[str, Any] | None:
        fetched.append(url)
        return QUESTIONS_JSON

    monkeypatch.setattr(preflight, "_fetch_text", lambda _url: EMBED_HTML)
    monkeypatch.setattr(preflight, "_fetch", fake_fetch)

    questions = preflight.greenhouse("https://www.brex.com/careers/8198574002?gh_jid=8198574002")

    assert questions is not None and len(questions) == 3
    assert fetched == [
        "https://boards-api.greenhouse.io/v1/boards/brex/jobs/8198574002?questions=true"
    ]


def test_the_embed_report_carries_a_real_verdict_not_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "_fetch_text", lambda _url: EMBED_HTML)
    monkeypatch.setattr(preflight, "_fetch", lambda _url: QUESTIONS_JSON)

    report = preflight.preflight(
        "https://www.brex.com/careers/8198574002?gh_jid=8198574002",
        {"first_name": "Mira"},
    )

    assert report["provider"] == "greenhouse"
    assert report["verdict"] == "fail"  # clearance has no supporting fact
    assert report["missing"][0]["question"] == "Security clearance?"


def test_a_js_rendered_page_falls_back_to_the_domain_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real brex.com page is JS-rendered: zero greenhouse references in
    the static HTML. The domain's own label is the guess, and the boards API
    validates it — a wrong slug 404s and the verdict honestly stays unknown."""
    fetched: list[str] = []

    def fake_fetch(url: str) -> dict | None:
        fetched.append(url)
        return QUESTIONS_JSON if "/boards/brex/" in url else None

    monkeypatch.setattr(
        preflight, "_fetch_text", lambda _url: "<html>react app, no markers</html>"
    )
    monkeypatch.setattr(preflight, "_fetch", fake_fetch)

    questions = preflight.greenhouse("https://www.brex.com/careers/8198574002?gh_jid=8198574002")

    assert questions is not None and len(questions) == 3
    assert fetched[-1].startswith("https://boards-api.greenhouse.io/v1/boards/brex/")

    # and a domain whose label is NOT a real board stays honestly unresolved
    monkeypatch.setattr(preflight, "_fetch", lambda url: None)
    assert preflight.greenhouse("https://www.notaboard.com/jobs?gh_jid=123") is None


def test_a_custom_domain_without_gh_jid_never_fetches_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight, "_fetch_text", lambda _url: pytest.fail("no gh_jid — must not fetch")
    )
    assert preflight.greenhouse("https://example.test/careers/rocket-engineer") is None


def test_a_native_greenhouse_url_still_skips_the_html_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight, "_fetch_text", lambda _url: pytest.fail("native URL — must not fetch HTML")
    )
    monkeypatch.setattr(preflight, "_fetch", lambda _url: QUESTIONS_JSON)
    questions = preflight.greenhouse("https://job-boards.greenhouse.io/gitlab/jobs/8464608002")
    assert questions is not None and len(questions) == 3


# ------------------------------------------------------------------- workable

WORKABLE_FORM = [
    {
        "name": "Personal information",
        "fields": [
            {"id": "firstname", "required": True, "label": "First name", "type": "text"},
            {"id": "email", "required": True, "label": "Email", "type": "email"},
            {"id": "phone", "required": True, "label": "Phone", "type": "phone"},
            {"id": "address", "required": True, "label": "Address", "type": "text"},
            {"id": "avatar", "required": False, "label": "Photo", "type": "file"},
        ],
    },
    {
        "name": "Profile",
        "fields": [
            {
                "id": "education",
                "required": False,
                "label": "Education",
                "type": "group",
                # Inner requireds only bind once an entry is added — the
                # group question carries its OWN flag, not its children's.
                "fields": [{"id": "school", "required": True, "label": "School", "type": "text"}],
            },
            {"id": "resume", "required": True, "label": "Resume", "type": "file"},
        ],
    },
]


def test_a_workable_form_enumerates_fields_with_required_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_fetch(url: str) -> Any:
        seen["url"] = url
        return WORKABLE_FORM

    monkeypatch.setattr(preflight, "_fetch", fake_fetch)
    questions = preflight.workable("https://apply.workable.com/acme/j/3A2AE898F0/")
    assert seen["url"] == "https://apply.workable.com/api/v1/jobs/3A2AE898F0/form"
    assert questions is not None and len(questions) == 7
    required = {q["text"] for q in questions if q["required"]}
    assert required == {"First name", "Email", "Phone", "Address", "Resume"}
    # the optional group did not leak its required child as a top-level demand
    assert "School" not in {q["text"] for q in questions}


def test_a_workable_shortlink_preflights_without_the_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The form endpoint is keyed by shortcode alone, so the org-less
    shortlink (apply.workable.com/j/<CODE>) must preflight identically."""
    monkeypatch.setattr(preflight, "_fetch", lambda _url: WORKABLE_FORM)
    questions = preflight.workable("https://apply.workable.com/j/3A2AE898F0")
    assert questions is not None and len(questions) == 7


def test_workable_preflight_report_covers_address_via_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "_fetch", lambda _url: WORKABLE_FORM)
    applicant = {
        "first_name": "Leo",
        "email": "x@y.z",
        "phone": "+1",
        "location": "Vancouver, Canada",
        "resume": "resume.docx",
    }
    report = preflight.preflight("https://apply.workable.com/acme/j/3A2AE898F0/", applicant)
    assert report["provider"] == "workable"
    assert report["verdict"] == "pass" and report["missing"] == []
    assert report["required_total"] == 5


def test_a_non_workable_url_is_not_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight, "_fetch", lambda _url: pytest.fail("no workable URL — must not fetch")
    )
    assert preflight.workable("https://jobs.example.com/j/ABCDEF") is None


# ------------------------------------------ applicant-fact mapping (job 71)
#
# A question family is only worth anything if its keys are the keys
# `applicant_from_profile` actually emits. Two did not: `linkedin` never
# existed (the fact is `linkedin_url`), so every posting with a required
# LinkedIn question failed preflight with the URL populated; and a desired
# -salary question mapped to nothing at all, so the live job-71 run burned its
# whole stop budget and parked held on that one field. These tests build the
# applicant the real way — from the profile — so a renamed key breaks here.

PROFILE = {
    "name": "Mira Halloway",
    "email": "mira@halloway.example",
    "phone": "+1 555 0100",
    "location": "Portland, OR",
    "salary_expectation": "$120,000",
}
LINKS = ["https://www.linkedin.com/in/mirahalloway", "https://mirahalloway.example"]

GAPS_FORM = [
    {
        "name": "Personal information",
        "fields": [
            {"id": "firstname", "required": True, "label": "First name", "type": "text"},
            {"id": "linkedin", "required": True, "label": "LinkedIn Profile", "type": "text"},
            {
                "id": "salary",
                "required": True,
                "label": "What is your desired salary?",
                "type": "text",
            },
        ],
    }
]


def test_the_linkedin_family_names_the_key_the_profile_emits() -> None:
    applicant = payload.applicant_from_profile(PROFILE, LINKS)
    assert applicant["linkedin_url"]  # the fact is populated…
    assert preflight._coverage("LinkedIn Profile", applicant) == ("linkedin_url", True)


def test_the_salary_family_names_the_key_the_profile_emits() -> None:
    applicant = payload.applicant_from_profile(PROFILE, LINKS)
    assert applicant["salary_expectation"] == "$120,000"
    for label in (
        "What is your desired salary?",
        "Salary expectations",
        "Expected compensation (USD)",
        "Desired pay",
        "What is your expected rate?",
    ):
        assert preflight._coverage(label, applicant) == ("salary_expectation", True), label


def test_a_skill_rating_question_is_not_mistaken_for_a_pay_question() -> None:
    """A false PASS is worse than a false fail — it wedges a browser run."""
    applicant = payload.applicant_from_profile(PROFILE, LINKS)
    assert preflight._coverage("How would you rate your Figma skills?", applicant) == ("", False)


def test_a_posting_asking_for_linkedin_and_salary_now_preflights_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "_fetch", lambda _url: GAPS_FORM)
    applicant = payload.applicant_from_profile(PROFILE, LINKS)
    report = preflight.preflight("https://apply.workable.com/bridgit/j/033F3E1DCF/", applicant)
    assert report["required_total"] == 3
    assert report["missing"] == []
    assert report["verdict"] == "pass"


def test_an_undeclared_salary_still_fails_honestly_and_names_the_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "_fetch", lambda _url: GAPS_FORM)
    applicant = payload.applicant_from_profile({**PROFILE, "salary_expectation": ""}, LINKS)
    report = preflight.preflight("https://apply.workable.com/bridgit/j/033F3E1DCF/", applicant)
    assert report["verdict"] == "fail"
    assert report["missing"] == [
        {"question": "What is your desired salary?", "needs_fact": "salary_expectation"}
    ]
