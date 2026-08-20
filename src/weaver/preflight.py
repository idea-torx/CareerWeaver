"""Pre-flight form intelligence: fetch an ATS posting's questions BEFORE any
browser session, diff them against the applicant facts, and report coverage.

Providers, in order:
  1. Greenhouse boards API (public, ?questions=true)
  2. Lever postings API (public, ?mode=json)
  3. Ashby job-board API (public)
  4. Workable form API (public, per-field required flags)
  5. Generic: plain fetch + regex over form inputs (best effort)

The point: a browser session is expensive (CF Browser Rendering rate limits +
minutes of LLM round-trips). Never launch one unless every required question
has a supporting fact. Missing facts -> the user fixes the profile, not the run.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 weaver-agent/0.1"
TIMEOUT = 25


def _fetch(url: str) -> Any | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# Keyword families: question text -> applicant fact keys it exercises.
_FAMILIES: list[tuple[str, set[str]]] = [
    (r"first name", {"first_name"}),
    (r"last name", {"last_name"}),
    (r"\bemail\b", {"email"}),
    (r"resume|cv\b|curriculum", {"resume"}),
    (r"portfolio|website|work sample", {"portfolio_url", "websites"}),
    (r"github", {"portfolio_url", "websites"}),
    (r"consent|retain|retention|privacy", {"consents"}),
    (r"legal authorization|authorized to work|right to work", {"authorized_to_work"}),
    (r"visa|sponsorship|sponsor", {"visa_sponsorship_required"}),
    (r"hear about|referral|source", {"how_did_you_hear"}),
    # Start-date questions are required text on many postings; without the fact
    # the run stalls against the typed-value guard rather than preflighting.
    # Narrow on purpose: a bare "start" would claim "when did you start at X".
    (r"when can you (join|start)|earliest start|start date|notice period|availability to start", {"availability"}),
    # The applicant key is `linkedin_url`; `linkedin` never existed, so every
    # posting with a required LinkedIn question preflighted as `fail` on every
    # provider even with the URL populated (job 71, 2026-08-19).
    (r"linkedin", {"linkedin_url"}),
    (r"phone", {"phone"}),
    (r"location|city", {"location"}),
    # Workable's required contact field is labeled "Address" and asks for
    # city/region/country — the location fact answers it.
    (r"\baddress\b", {"location"}),
    (r"country.*(located|residen)|current country", {"location"}),
    (r"employment agreement|post-employment restriction|worked at|worked for|consulted for", {"employment_history"}),
    (r"gender", {"gender"}),
    (r"disabil", {"disability_status"}),
    (r"race|ethnic", {"race_ethnicity"}),
    (r"veteran", {"veteran_status"}),
    (r"date of birth|birthday", {"date_of_birth"}),
    # Deliberately narrower than guardrail.COMPENSATION_LABEL_RE (which may be
    # permissive — it only decides whether a number is ALLOWED): a bare "rate"
    # would claim "how would you rate your Figma skills" and report a false
    # pass, and a false pass costs a wedged browser run.
    (
        r"salary|compensation|remuneration|\bwage|\bctc\b"
        r"|(desired|expected|target|minimum) (pay|rate|earnings)"
        r"|pay (expectation|requirement|range you)|hourly rate",
        {"salary_expectation"},
    ),
    (r"cover letter", {"cover_letter"}),
]


def _coverage(label: str, facts: dict[str, Any]) -> tuple[str, bool]:
    """Which applicant fact answers a question, and is it populated?"""
    text = label.lower()
    for pattern, keys in _FAMILIES:
        if re.search(pattern, text):
            for key in keys:
                value = facts.get(key)
                if value:
                    return key, True
            return next(iter(keys), ""), False
    return "", False


def greenhouse(job_url: str) -> list[dict[str, Any]] | None:
    """Greenhouse board -> question list. URL can be the board URL, the API
    URL, or a custom-domain page embedding a Greenhouse board (?gh_jid=...)."""
    m = re.search(r"greenhouse\.io/v1/boards/([^/]+)/jobs/(\d+)", job_url)
    if not m:
        m = re.search(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", job_url)
    if m:
        candidates = [(m.group(1), m.group(2))]
    else:
        candidates = _embedded_greenhouse_candidates(job_url)
    for org, job in candidates:
        # The API validates a guessed slug for free: a wrong org 404s and the
        # next candidate (or an honest `unknown`) takes over.
        data = _fetch(
            f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{job}?questions=true"
        )
        if data:
            return [
                {
                    "text": q.get("label") or q.get("name") or "",
                    "required": bool(q.get("required")),
                }
                for q in data.get("questions", [])
            ]
    return None


def _fetch_text(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def _embedded_greenhouse_candidates(job_url: str) -> list[tuple[str, str]]:
    """(org, job_id) guesses for a custom-domain Greenhouse embed.

    `brex.com/careers/8198574002?gh_jid=8198574002` carries the job id in the
    gh_jid param, but the board slug is nowhere reliable: server-rendered
    pages carry it in the embed markup (`…/embed/job_app?for=<slug>`), while
    JS-rendered ones (Brex) contain no greenhouse reference at all — there the
    domain's own label is the best guess. Callers validate every candidate
    against the boards API, so a wrong guess costs one 404, never a wrong
    verdict. Without this, such postings preflighted as `unknown` and burned a
    browser session on a page the engine may not be able to fill (both blind
    runs of the 2026-08-19 test #1 were exactly this shape).
    """
    parsed = urllib.parse.urlparse(job_url)
    query = urllib.parse.parse_qs(parsed.query)
    job = (query.get("gh_jid") or [""])[0]
    if not job.isdigit():
        return []
    slugs: list[str] = []
    html = _fetch_text(job_url)
    if html:
        m = re.search(r"greenhouse\.io/embed/job_app\?[^\"'<>]*\bfor=([A-Za-z0-9_-]+)", html)
        if not m:
            m = re.search(r"boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([A-Za-z0-9_-]+)", html)
        if m:
            slugs.append(m.group(1))
    labels = (parsed.hostname or "").split(".")
    if len(labels) >= 2 and labels[-2] not in slugs:
        slugs.append(labels[-2])
    return [(slug, job) for slug in slugs]


def lever(job_url: str) -> list[dict[str, Any]] | None:
    m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]+)", job_url)
    if not m:
        return None
    data = _fetch(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}?mode=json")
    if not data:
        return None
    questions: list[dict[str, Any]] = []
    for q in data.get("additionalQuestionnaire", []) + data.get("customQuestions", []):
        questions.append(
            {"text": q.get("text") or q.get("name") or "", "required": not q.get("optional")}
        )
    if data.get("applyUrl"):
        questions.append({"text": "apply form", "required": True, "link": data["applyUrl"]})
    return questions


def ashby(job_url: str) -> list[dict[str, Any]] | None:
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]+)", job_url)
    if not m:
        return None
    data = _fetch(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}")
    if not data:
        return None
    for job in data.get("jobs", []):
        if job.get("id") == m.group(2):
            return [
                {"text": q.get("name") or "", "required": q.get("isRequired", False)}
                for q in job.get("applicationQuestions", [])
            ]
    return None


def workable(job_url: str) -> list[dict[str, Any]] | None:
    """Workable form endpoint -> question list. The endpoint is keyed by the
    job shortcode alone (no org), so the org-less shortlink form of the URL
    preflights just as well as the full org path. Unlike Ashby's (which can't
    see per-field requiredness at all), this payload enumerates every form
    field with its own required flag — the strongest preflight of any
    provider."""
    m = re.search(r"apply\.workable\.com/(?:[^/?#]+/)?j/([A-Za-z0-9]{6,})", job_url)
    if not m:
        return None
    data = _fetch(f"https://apply.workable.com/api/v1/jobs/{m.group(1)}/form")
    if not isinstance(data, list):
        return None
    questions: list[dict[str, Any]] = []
    for section in data:
        for field in (section or {}).get("fields") or []:
            # Group fields (education, experience) stay one question at the
            # group's own required flag — their inner requireds only bind
            # once the applicant chooses to add an entry.
            questions.append(
                {
                    "text": field.get("label") or field.get("id") or "",
                    "required": bool(field.get("required")),
                }
            )
    return questions


def generic(job_url: str) -> list[dict[str, Any]] | None:
    """Last resort: fetch the page and regex the form inputs. Best effort."""
    try:
        req = urllib.request.Request(job_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    if not html or len(html) < 500:
        return None
    labels = set(re.findall(r"<label[^>]*>(.*?)</label>", html, re.S))
    questions: list[dict[str, Any]] = []
    for label in labels:
        text = re.sub(r"<[^>]+>", " ", label)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            questions.append({"text": text[:120], "required": "*" in text or "required" in text.lower()})
    return questions or None


def fetch_questions(job_url: str) -> list[dict[str, Any]] | None:
    for provider in (greenhouse, lever, ashby, workable, generic):
        try:
            result = provider(job_url)
        except Exception:
            result = None
        if result is not None:
            return result
    return None


def preflight(
    job_url: str,
    applicant: dict[str, Any],
) -> dict[str, Any]:
    """Return the coverage report. Never raises on provider failures —
    unknown coverage is reported, and the caller decides whether to proceed."""
    questions = fetch_questions(job_url)
    if questions is None:
        return {
            "provider": None,
            "covered": False,
            "verdict": "unknown",
            "message": "could not read questions for this posting — run without preflight assurance",
            "required_total": 0,
            "missing": [],
            "optional_total": 0,
        }
    required = [q for q in questions if q.get("required")]
    optional = [q for q in questions if not q.get("required")]
    missing: list[dict[str, Any]] = []
    for q in required:
        key, ok = _coverage(q.get("text", ""), applicant)
        if not ok:
            missing.append({"question": q.get("text", ""), "needs_fact": key})
    return {
        "provider": "greenhouse" if _is_greenhouse(job_url) else "lever" if "lever.co" in job_url else "ashby" if "ashbyhq" in job_url else "workable" if "workable.com" in job_url else "generic",
        "covered": len(missing) == 0,
        "verdict": "pass" if len(missing) == 0 else "fail",
        "required_total": len(required),
        "optional_total": len(optional),
        "missing": missing,
    }


def _is_greenhouse(job_url: str) -> bool:
    # gh_jid marks a Greenhouse board embedded on a custom domain.
    return "greenhouse.io" in job_url or "gh_jid=" in job_url
