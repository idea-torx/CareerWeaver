"""The applicant payload and request body — every fact an application types.

Built from the profile (data/config.json) plus the tailored resume's structure.
This is the boundary the guardrail trusts: the agent loop may only type values
that trace back to what is assembled here.

Everything runs locally. The former Skyvern and Cloudflare-worker adapters are
gone; the payload shape they defined stays, because the ledger and the loop
already speak it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any

from . import db

#: The loop's action budget when the caller does not say otherwise.
DEFAULT_MAX_ACTIONS = 60

WORKFLOW_TYPE = "job_application"

#: Hosts that are never "your portfolio site" by default — profile pages on
#: someone else's platform. Dribbble/Behance ARE portfolio-ish, but a personal
#: domain beats them; declare `profile.portfolio` (or WEAVER_PORTFOLIO_HOSTS)
#: to choose explicitly.
SOCIAL_HOSTS = (
    "linkedin.", "github.", "twitter.", "x.com", "facebook.", "mastodon.",
    "dribbble.", "behance.",
)

NAVIGATION_GOAL = (
    "Complete the job application form using the applicant data provided. Upload the "
    "attached resume file. Answer only questions that the applicant data supports; if a "
    "required question has no supporting fact, stop and report it instead of guessing."
)
COMPLETION_CRITERIA = (
    "The application is submitted and a confirmation page, confirmation message, or "
    "confirmation email reference is visible."
)

#: Engine status -> ledger status.
LEDGER_STATUS = {
    "applied": "applied",
    "stopped": "stopped",
    # Parked for a human to finish in the open window — never a submission.
    "audit_pending": "audit_pending",
    "failed": "failed",
    "max_actions": "failed",
}


def _split_name(name: str) -> tuple[str, str]:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _link(links: list[str], *needles: str) -> str:
    for link in links:
        low = link.lower()
        if any(needle in low for needle in needles):
            return link
    return ""


def _portfolio_hosts() -> tuple[str, ...]:
    raw = os.environ.get("WEAVER_PORTFOLIO_HOSTS") or ""
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _portfolio(links: list[str]) -> str:
    """Your portfolio link: an explicit host if configured, else the first
    non-social link. Nobody's personal domain is hardcoded here."""
    hosts = _portfolio_hosts()
    if hosts:
        explicit = _link(links, *hosts)
        if explicit:
            return explicit
    for link in links:
        low = link.lower()
        if not any(host in low for host in SOCIAL_HOSTS):
            return link
    return ""


def years_of_experience(conn: sqlite3.Connection) -> int:
    """Derived from the graph itself, so it does not drift with the wall clock."""
    years: list[int] = []
    for fact in db.get_facts(conn, ["role"]):
        for value in (fact.get("start"), fact.get("end")):
            match = re.search(r"(?:19|20)\d{2}", value or "")
            if match:
                years.append(int(match.group(0)))
    if not years:
        return 0
    return max(0, max(years) - min(years))


def applicant_from_profile(profile: dict[str, Any], links: list[str]) -> dict[str, Any]:
    """Profile+links facts only — what preflight checks and every application shares."""
    return {
        "first_name": profile.get("first_name") or ((profile.get("name") or "").split() or [""])[0],
        "last_name": profile.get("last_name") or ((profile.get("name") or "").split() or [""])[-1],
        "full_name": profile.get("name") or "",
        "email": profile.get("email") or "",
        "phone": profile.get("phone") or "",
        "location": profile.get("location") or "",
        "authorized_to_work": profile.get("authorized_to_work") or "",
        # Voluntary self-ID: only present when you explicitly declare them in
        # your profile; the agent answers "I do not wish to disclose" otherwise.
        "date_of_birth": profile.get("date_of_birth") or "",
        "gender": profile.get("gender") or "",
        # Forms ask for pronouns as a free-text/dropdown question; without a
        # declared key the engine has no answer and the typed-value guard
        # (correctly) refuses a page-sourced one, so the run wedges.
        "pronouns": profile.get("pronouns") or "",
        "disability_status": profile.get("disability_status") or "",
        "race_ethnicity": profile.get("race_ethnicity") or "",
        "veteran_status": profile.get("veteran_status") or "",
        "consents": profile.get("consent_ats_data_retention") or "",
        "how_did_you_hear": profile.get("how_did_you_hear") or "",
        # "When can you join / earliest start date" is a required text question
        # on many postings. Without a declared datum the typed-value guard
        # (correctly) refuses every page-sourced answer and the run burns its
        # action budget before settling on a placeholder — run 137 (Plane,
        # 2026-08-20) spent six actions to end up typing "N/A".
        "availability": profile.get("availability") or "",
        "visa_sponsorship_required": profile.get("visa_sponsorship_required") or "",
        # Relocation / in-office / remote questions are PREFERENCE questions —
        # without a declared preference the guardrail (rightly) refuses to
        # guess and the radio group stays empty (run 89's Ramp SF question).
        "work_preference": profile.get("work_preference") or "",
        # Declared ONLY so a field that asks for compensation can be answered —
        # job 71 (Workable) burned its whole stop budget on "desired salary"
        # with the figure sitting unread in the profile. It is not a
        # free-to-mention datum: guardrail.COMPENSATION_ONLY_KEYS keeps
        # `volunteered_compensation` from waving it into essays or open text.
        "salary_expectation": str(profile.get("salary_expectation") or "").strip(),
        "employment_history": profile.get("employment_history") or "",
        "linkedin_url": _link(links, "linkedin"),
        # An explicitly declared portfolio beats the heuristic — run 83 offered
        # a dribbble profile for "Portfolio" while the applicant's own site sat
        # unlisted. Declare it once in the profile and it always wins.
        "portfolio_url": str(profile.get("portfolio") or "").strip() or _portfolio(links),
        "websites": links,
    }


def build_payload(
    conn: sqlite3.Connection,
    resume: dict[str, Any],
    profile: dict[str, Any],
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    structure = resume.get("structure") or {}
    contact = structure.get("contact") or {}
    # The resume's contact links are what is printed ON the resume — the
    # profile's links (and its declared portfolio) still belong in the
    # applicant data even when a tailored resume omits one of them. Run 84
    # answered "Portfolio" with a heuristic pick because the declared site was
    # only in the profile.
    links = list(contact.get("links") or profile.get("links") or [])
    for extra in list(profile.get("links") or []) + (
        [str(profile.get("portfolio") or "").strip()] if str(profile.get("portfolio") or "").strip() else []
    ):
        if extra and extra not in links:
            links.append(extra)
    first, last = _split_name(structure.get("name") or profile.get("name") or "")
    experience = structure.get("experience") or []

    skills: list[str] = []
    for group in structure.get("skills") or []:
        skills.extend(group.get("items") or [])

    applicant = {
        **applicant_from_profile(profile, links),
        "first_name": first,
        "last_name": last,
        "full_name": structure.get("name") or profile.get("name") or "",
        "email": contact.get("email") or profile.get("email") or "",
        "phone": contact.get("phone") or profile.get("phone") or "",
        "location": contact.get("location") or profile.get("location") or "",
        "current_title": structure.get("title") or "",
        "current_company": experience[0].get("org") if experience else "",
        "years_of_experience": years_of_experience(conn),
        "summary": structure.get("summary") or "",
        "skills": skills,
        "work_experience": [
            {
                "title": entry.get("role"),
                "company": entry.get("org"),
                "dates": entry.get("dates"),
                "location": entry.get("location"),
                "highlights": entry.get("bullets") or [],
            }
            for entry in experience
        ],
        "education": structure.get("education") or [],
        "awards": structure.get("awards") or [],
    }

    return {
        "workflow_type": WORKFLOW_TYPE,
        "title": f"CareerWeaver application — {(job or {}).get('title') or resume.get('lens') or 'lens'}",
        "url": (job or {}).get("url") or "",
        "navigation_goal": NAVIGATION_GOAL,
        "completion_criteria": COMPLETION_CRITERIA,
        "resume": {
            "resume_id": resume.get("id"),
            "path": resume.get("path"),
            "format": resume.get("format"),
            "lens": resume.get("lens"),
            "source_facts": resume.get("source_facts") or [],
        },
        "job": {
            "id": (job or {}).get("id"),
            "title": (job or {}).get("title"),
            "company": (job or {}).get("company"),
            "url": (job or {}).get("url"),
            # Context for the fill loop's open questions ("why us", "first
            # project") — grounding only; answers still come from the applicant.
            "posting_excerpt": str((job or {}).get("raw_text") or "")[:1500],
        },
        # `avoid` rides OUTSIDE `applicant` on purpose: the applicant block is
        # the set of values the loop is allowed to type, and a do-not-mention
        # topic put in there would be waved through as a declared datum.
        "parameters": {
            "applicant": applicant,
            "resume_file": resume.get("path"),
            "avoid": list(profile.get("avoid") or []),
        },
    }


def hosted_resume_url(payload: dict[str, Any] | None = None) -> str:
    """WEAVER_RESUME_URL, else the payload's resume_file when it is a URL."""
    env = os.environ.get("WEAVER_RESUME_URL", "").strip()
    if env:
        return env
    candidate = str(((payload or {}).get("parameters") or {}).get("resume_file") or "")
    return candidate if candidate.startswith(("http://", "https://")) else ""


def build_request(
    payload: dict[str, Any],
    llm_conf: dict[str, Any] | None = None,
    resume_url: str | None = None,
    max_actions: int = DEFAULT_MAX_ACTIONS,
) -> dict[str, Any]:
    """The agent-loop request body. `payload` is a `build_payload` result."""
    parameters = payload.get("parameters") or {}
    applicant = parameters.get("applicant") or {}
    job = payload.get("job") or {}
    job_url = payload.get("url") or job.get("url") or ""
    resume = payload.get("resume") or {}
    return {
        "job_url": job_url,
        "applicant": applicant,
        "avoid": list(parameters.get("avoid") or []),
        "resume_url": resume_url if resume_url is not None else hosted_resume_url(payload),
        "resume_filename": _filename(resume, applicant),
        "job": {
            "title": job.get("title"),
            "company": job.get("company"),
            "url": job_url,
            "posting_excerpt": str(job.get("posting_excerpt") or "")[:1500],
        },
        "llm": dict(llm_conf or {}),
        "max_actions": max_actions,
    }


def _filename(resume: dict[str, Any], applicant: dict[str, Any]) -> str:
    fmt = (resume.get("format") or "docx").lower()
    name = (applicant.get("full_name") or "resume").strip().lower().replace(" ", "-")
    return f"{name}-resume.{fmt}"


def redact(request: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to print and to store in the ledger."""
    out = json.loads(json.dumps(request, default=str))
    if isinstance(out.get("llm"), dict):
        out["llm"] = {**out["llm"], "api_key": "***" if out["llm"].get("api_key") else ""}
    return out


def ledger_status(response: dict[str, Any]) -> str:
    return LEDGER_STATUS.get(str(response.get("status") or ""), "failed")


def split_screenshots(response: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Peel the base64 images off a response before it goes into SQLite."""
    lean = dict(response)
    final = lean.pop("final_screenshot_b64", None)
    milestones = lean.get("milestones") or []
    lean["milestones"] = [
        {"n": m.get("n"), "label": m.get("label"), "bytes": len(m.get("b64") or "")}
        for m in milestones
        if isinstance(m, dict)
    ]
    lean["final_screenshot_bytes"] = len(final or "")
    # The audit seam carries its own screenshot — same rule: bytes, not pixels.
    audit = lean.get("audit")
    if isinstance(audit, dict):
        shot = audit.get("screenshot_b64") or ""
        lean["audit"] = {
            **{k: v for k, v in audit.items() if k != "screenshot_b64"},
            "screenshot_bytes": len(shot),
        }
        if not final:
            final = shot or None
    return lean, final
