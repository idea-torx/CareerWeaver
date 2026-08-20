"""Job postings: store raw text, pull out title/company/required skills.

Deterministic only — `jobs add <url>` records the URL and does not reach out to
the network unless you pass --fetch (kept local by default).

Lane 1 (`weaver find`) lives here too: public ATS board feeds (Greenhouse /
Lever / Ashby / Workable, no auth), a deterministic fit scorer against the
saved profile, and the ranked shortlist written to jobs.json.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .config import DEFAULT_FIND
from .domains import TOOLS, normalize

URL_RE = re.compile(r"^https?://", re.I)

TITLE_RE = re.compile(r"^\s*(?:job\s+)?(?:title|role|position)\s*[:\-–]\s*(.+)$", re.I | re.M)
COMPANY_RE = re.compile(r"^\s*(?:company|organization|employer)\s*[:\-–]\s*(.+)$", re.I | re.M)
AT_COMPANY_RE = re.compile(r"^(.{3,70}?)\s+(?:at|@|—|-)\s+([A-Z][\w&.' ]{2,40})\s*$", re.M)
ROLE_WORDS = re.compile(
    r"\b(engineer|developer|designer|director|lead|manager|artist|architect|scientist|"
    r"specialist|analyst|consultant|technologist|producer|creative)\b",
    re.I,
)


def is_url(value: str) -> bool:
    return bool(URL_RE.match(value.strip()))


#: An Ashby job page: jobs.ashbyhq.com/<org>/<posting-uuid>[...]
ASHBY_JOB_RE = re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f][0-9a-f-]{7,})", re.I)

#: A Workable job page: apply.workable.com/<org>/j/<SHORTCODE> — or the
#: org-less shortlink apply.workable.com/j/<SHORTCODE>, which 301s to it.
WORKABLE_JOB_RE = re.compile(
    r"https?://apply\.workable\.com/(?:([^/?#]+)/)?j/([A-Z0-9]{6,})", re.I
)


def fetch(url: str, timeout: float = 20.0) -> str:
    """Fetch a posting. Only called when the user explicitly passes --fetch.

    Ashby job pages are JS-rendered shells over plain HTTP — scraping one
    stored ~137 chars of bootstrap script (job 6) and the lens auto-picker,
    starved of text, dressed a Principal Product Designer role as sre. Their
    public posting API carries the real text, so an ashby URL is answered from
    the same feed `weaver find` reads; the HTML scrape stays as the fallback.
    """
    ashby = ASHBY_JOB_RE.match(url.strip())
    if ashby:
        text = _fetch_ashby_posting(ashby.group(1), ashby.group(2), timeout=timeout)
        if text:
            return text
    workable = WORKABLE_JOB_RE.match(url.strip())
    if workable:
        text = _fetch_workable_posting(
            workable.group(1) or "", workable.group(2), url.strip(), timeout=timeout
        )
        if text:
            return text
    request = urllib.request.Request(url, headers={"User-Agent": "CareerWeaver/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"could not fetch {url}: {exc}") from exc
    return strip_html(raw)


def _fetch_ashby_posting(org: str, job_id: str, timeout: float = 20.0) -> str:
    """The posting's text from Ashby's public board API, "" when not found.

    Leads with "<role> at <Org>" and the location so `parse_posting` extracts
    the same title/company it would from a well-behaved HTML posting.
    """
    try:
        postings = fetch_board_jobs(org, "ashby", timeout=timeout)
    except (RuntimeError, FindError):
        return ""
    wanted = job_id.lower()
    for posting in postings:
        if wanted in (posting.get("url") or "").lower():
            role = (posting.get("role") or "").strip()
            lines: list[str] = []
            if role:
                lines.append(f"{role} at {org.strip().capitalize()}")
            location = (posting.get("location") or "").strip()
            if location:
                lines.append(location)
            lines += ["", posting.get("body") or ""]
            return "\n".join(lines).strip()
    return ""


def _fetch_workable_posting(org: str, shortcode: str, url: str, timeout: float = 20.0) -> str:
    """The posting's text from Workable's public job API, "" when not found.

    Workable job pages are JS shells like Ashby's (~48 chars of visible text),
    so scraping one starves the parser. The org-less shortlink form
    (apply.workable.com/j/<CODE>) 301s to the org path; one fetch of the shell
    recovers the org, then the v2 job endpoint carries the real text.
    """
    if not org:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "CareerWeaver/0.1"})
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                resolved = WORKABLE_JOB_RE.match(resp.geturl() or "")
                org = (resolved.group(1) or "") if resolved else ""
        except (urllib.error.URLError, OSError, ValueError):
            return ""
    if not org:
        return ""
    try:
        data = http_get_json(
            f"https://apply.workable.com/api/v2/accounts/{org.strip().lower()}/jobs/{shortcode}",
            timeout,
        )
    except RuntimeError:
        return ""
    if not isinstance(data, dict):
        return ""
    lines: list[str] = []
    role = (data.get("title") or "").strip()
    if role:
        lines.append(f"{role} at {org.strip().capitalize()}")
    location = _workable_location(data)
    if location:
        lines.append(location)
    body = " ".join(
        strip_html(html.unescape(data.get(part) or ""))
        for part in ("description", "requirements", "benefits")
    ).strip()
    lines += ["", body]
    return "\n".join(lines).strip()


def _workable_location(job: dict[str, Any]) -> str:
    """One display string from Workable's several location shapes."""
    loc = job.get("location")
    if isinstance(loc, dict):
        parts = [loc.get("display") or "", loc.get("city") or "", loc.get("country") or ""]
        display = next((p.strip() for p in parts if p and p.strip()), "")
    else:
        parts = [job.get("city") or "", job.get("state") or "", job.get("country") or ""]
        display = ", ".join(p.strip() for p in parts if p and p.strip())
    remote = bool(job.get("remote") or job.get("telecommuting")) or job.get("workplace") == "remote"
    if remote:
        display = f"{display} (Remote)" if display else "Remote"
    return display


def strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;?", "&", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def parse_posting(text: str) -> dict[str, Any]:
    body = (text or "").strip()
    title = ""
    company = ""

    match = TITLE_RE.search(body)
    if match:
        title = match.group(1).strip()
    match = COMPANY_RE.search(body)
    if match:
        company = match.group(1).strip()

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not title or not company:
        for line in lines[:8]:
            at = AT_COMPANY_RE.match(line)
            if at and ROLE_WORDS.search(at.group(1)):
                title = title or at.group(1).strip()
                company = company or at.group(2).strip()
                break
    if not title:
        for line in lines[:6]:
            if ROLE_WORDS.search(line) and len(line) <= 80:
                title = line
                break
    return {
        "title": title[:120],
        "company": company[:80],
        "skills_required": detect_skills(body),
    }


def detect_skills(text: str, conn: sqlite3.Connection | None = None) -> list[str]:
    """Skills the posting names, drawn from the tool vocabulary + known skills."""
    hay = normalize(text)
    found: list[str] = []
    vocabulary: list[tuple[str, str]] = [(key, name) for key, name in TOOLS.items()]
    if conn is not None:
        for skill in db.get_skills(conn):
            vocabulary.append((normalize(skill["name"]), skill["name"]))
    for key, name in vocabulary:
        if not key:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", hay):
            if name not in found:
                found.append(name)
    return found


def add(
    conn: sqlite3.Connection, value: str, fetch_url: bool = False, text: str | None = None
) -> dict[str, Any]:
    """Add a posting from a URL, a file path, or raw text."""
    url = None
    raw = text or ""
    if not raw:
        candidate = value.strip()
        if is_url(candidate):
            url = candidate
            raw = fetch(candidate) if fetch_url else ""
        else:
            from pathlib import Path

            path = Path(candidate).expanduser()
            if len(candidate) < 400 and path.exists() and path.is_file():
                raw = path.read_text(encoding="utf-8", errors="replace")
            else:
                raw = candidate

    parsed = parse_posting(raw)
    parsed["skills_required"] = detect_skills(raw, conn) if raw else []
    job_id = db.add_job(
        conn,
        url=url,
        title=parsed["title"] or (url or "")[:120] or "Untitled posting",
        company=parsed["company"],
        raw_text=raw,
        skills_required=parsed["skills_required"],
    )
    job = db.get_job(conn, job_id)
    assert job is not None
    return job


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
    out = []
    for row in rows:
        entry = dict(row)
        entry["skills_required"] = db.loads(entry["skills_required"])
        entry["raw_text_chars"] = len(entry.get("raw_text") or "")
        entry.pop("raw_text", None)
        out.append(entry)
    return out


def resolve(conn: sqlite3.Connection, value: str) -> dict[str, Any] | None:
    """--job accepts an id, a saved URL, a file path, or raw text."""
    candidate = (value or "").strip()
    if not candidate:
        return None
    if candidate.isdigit():
        return db.get_job(conn, int(candidate))
    if is_url(candidate):
        existing = db.find_job_by_url(conn, candidate)
        return existing or add(conn, candidate)
    return add(conn, candidate)


# --------------------------------------------------------------- lane 1: find

#: Supported public ATS boards -> feed URL templates (no auth keys).
BOARDS = ("greenhouse", "lever", "ashby", "workable")
DEFAULT_ORGS: dict[str, str] = dict(DEFAULT_FIND["orgs"])
DEFAULT_SENIORITY: list[str] = list(DEFAULT_FIND["seniority"])

#: Weights of the fit scorer; the four components sum to 1.0.
W_TITLE, W_SKILLS, W_SENIORITY, W_LOCATION = 0.45, 0.35, 0.10, 0.10

FIND_STOPWORDS = frozenset(
    "a an the and or of for to in with at our we you your".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class FindError(ValueError):
    """Bad org/board input — a usage error, not a network error."""


def board_url(board: str, org: str) -> str:
    """Public feed URL for one org's board."""
    slug = (org or "").strip().lower()
    if board == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    if board == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    if board == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    if board == "workable":
        # details=true inlines each posting's description HTML — one request
        # per org, same shape the fit scorer needs from the other boards.
        return f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    raise FindError(f"unknown board {board!r} (expected one of: {', '.join(BOARDS)})")


def http_get_json(url: str, timeout: float = 15.0) -> Any:
    """The one network touchpoint of `weaver find` (stdlib urllib, no deps)."""
    request = urllib.request.Request(url, headers={"User-Agent": "CareerWeaver/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"could not fetch {url}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from {url}: {exc}") from exc


def _posting(role: str, org: str, board: str, url: str, location: str, body: str) -> dict[str, Any]:
    return {
        "role": (role or "").strip()[:120],
        "org": org,
        "board": board,
        "url": (url or "").strip(),
        "location": (location or "").strip()[:120],
        "body": body or "",
    }


def parse_board_payload(board: str, org: str, payload: Any) -> list[dict[str, Any]]:
    """Normalize a raw feed payload into postings (pure — canned-JSON testable)."""
    jobs: list[dict[str, Any]] = []
    if board == "greenhouse":
        for job in (payload or {}).get("jobs") or []:
            jobs.append(
                _posting(
                    role=job.get("title") or "",
                    org=org,
                    board=board,
                    url=job.get("absolute_url") or "",
                    location=(job.get("location") or {}).get("name") or "",
                    body=strip_html(html.unescape(job.get("content") or "")),
                )
            )
    elif board == "lever":
        for job in payload or []:
            extra = " ".join(
                (item.get("text") or "")
                + " "
                + strip_html(html.unescape(item.get("content") or ""))
                for item in job.get("lists") or []
            )
            jobs.append(
                _posting(
                    role=job.get("text") or "",
                    org=org,
                    board=board,
                    url=job.get("hostedUrl") or "",
                    location=(job.get("categories") or {}).get("location") or "",
                    body=((job.get("descriptionPlain") or "") + " " + extra).strip(),
                )
            )
    elif board == "ashby":
        for job in (payload or {}).get("jobs") or []:
            description = job.get("descriptionHtml") or job.get("description") or ""
            jobs.append(
                _posting(
                    role=job.get("title") or "",
                    org=org,
                    board=board,
                    url=job.get("jobUrl") or job.get("applyUrl") or "",
                    location=job.get("location") or "",
                    body=strip_html(html.unescape(description)),
                )
            )
    elif board == "workable":
        for job in (payload or {}).get("jobs") or []:
            jobs.append(
                _posting(
                    role=job.get("title") or "",
                    org=org,
                    board=board,
                    # The feed's own url field is the 301-ing shortlink; the
                    # org path is built instead so saved URLs carry the org
                    # for later API-backed fetches.
                    url=(
                        f"https://apply.workable.com/{org}/j/{job['shortcode']}/"
                        if job.get("shortcode")
                        else (job.get("url") or job.get("shortlink") or "").strip()
                    ),
                    location=_workable_location(job),
                    body=strip_html(html.unescape(job.get("description") or "")),
                )
            )
    else:
        raise FindError(f"unknown board {board!r} (expected one of: {', '.join(BOARDS)})")
    return [job for job in jobs if job["role"]]


def fetch_board_jobs(org: str, board: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Fetch + normalize one org's board."""
    return parse_board_payload(board, org, http_get_json(board_url(board, org), timeout))


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens, minus filler — 'Staff Brand Designer' -> staff brand designer."""
    return [
        tok
        for tok in _TOKEN_RE.findall((text or "").lower())
        if tok not in FIND_STOPWORDS
    ]


def _flat(text: str) -> str:
    """Normalized single-string form used for word-boundary matching."""
    return " ".join(_TOKEN_RE.findall((text or "").lower()))


def _tok_present(flat_hay: str, token: str) -> bool:
    if not token or not flat_hay:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])"
    return re.search(pattern, flat_hay) is not None


def _phrase_tokens(phrase: str) -> list[str]:
    return [
        tok
        for tok in _TOKEN_RE.findall((phrase or "").lower())
        if tok not in FIND_STOPWORDS
    ]


def _phrase_present(flat_hay: str, phrase: str) -> bool:
    return all(_tok_present(flat_hay, tok) for tok in _phrase_tokens(phrase))


def score_fit(posting: dict[str, Any], targets: dict[str, Any]) -> tuple[float, list[str]]:
    """Deterministic 0..1 fit + human-readable reasons for one posting.

    Keyword overlap: target-title tokens and required-skill tokens matched on
    the posting's title+location+body; plus fixed bumps for a seniority word in
    the posting's own title and a target location matched in the location field
    (or body). No network, no LLM, same input -> same output.
    """
    titles = [t for t in (targets.get("target_titles") or []) if _phrase_tokens(t)]
    skills = [s for s in (targets.get("skills") or []) if _phrase_tokens(s)]
    seniority = [str(s) for s in targets.get("seniority") or [] if str(s).strip()]
    locations = [loc for loc in (targets.get("locations") or []) if _phrase_tokens(loc)]
    excludes = [e for e in (targets.get("exclude_titles") or []) if _phrase_tokens(e)]
    if not titles and not skills:
        return 0.0, ["no targets configured — set profile.target_roles/target_skills (config.json)"]

    title_flat = _flat(posting.get("role") or "")

    # QUALIFICATION VETO. Token overlap cannot tell a Product Designer from a
    # Product Engineer — the morning-of-Aug-16 run shortlisted Senior/Staff
    # Fullstack roles at 0.71 because "engineer" tokens and React/TypeScript
    # skills lit up the scorer. A title matching find.exclude_titles is out,
    # unless a target title matches the posting title IN FULL (an explicit
    # qualification always beats the exclusion list).
    if excludes:
        vetoed = next((e for e in excludes if _phrase_present(title_flat, e)), "")
        if vetoed:
            qualified = any(
                all(_tok_present(title_flat, tok) for tok in _phrase_tokens(t)) for t in titles
            )
            if not qualified:
                return 0.0, [f'excluded: "{vetoed.lower()}" in the title (find.exclude_titles)']
    hay_flat = _flat(
        f"{posting.get('role') or ''} {posting.get('location') or ''} {posting.get('body') or ''}"
    )

    best_frac, best_title, best_hit = 0.0, "", 0
    for phrase in titles:
        toks = _phrase_tokens(phrase)
        # Matched against the posting's TITLE only. Body matching let any
        # posting whose description mentions "product design" collect the
        # full title weight — "Senior Applied Scientist, Credit Risk" scored
        # 0.68 for a designer profile (morning of Aug 16). The body still
        # feeds the skills component; the title weight is about the role.
        hit = sum(1 for tok in toks if _tok_present(title_flat, tok))
        frac = hit / len(toks)
        if frac > best_frac or (frac == best_frac and hit > best_hit):
            best_frac, best_title, best_hit = frac, phrase, hit
    reasons: list[str] = []
    if best_frac > 0:
        reasons.append(f"title {best_hit}/{len(_phrase_tokens(best_title))} ({best_title.lower()})")

    matched_skills = [s for s in skills if _phrase_present(hay_flat, s)]
    if skills:
        detail = f" ({', '.join(s.lower() for s in matched_skills)})" if matched_skills else ""
        reasons.append(f"skills {len(matched_skills)}/{len(skills)}{detail}")

    sen_word = next((w for w in seniority if _tok_present(title_flat, w.lower())), "")
    if sen_word:
        reasons.append(f"seniority: {sen_word.lower()}")

    loc_flat = _flat(posting.get("location") or "")
    loc_match = next(
        (loc for loc in locations if _phrase_present(loc_flat, loc) or _phrase_present(hay_flat, loc)),
        "",
    )
    if loc_match:
        reasons.append(f"location: {loc_match.lower()}")

    fit = W_TITLE * best_frac
    fit += W_SKILLS * (len(matched_skills) / len(skills) if skills else 0.0)
    fit += W_SENIORITY if sen_word else 0.0
    fit += W_LOCATION if loc_match else 0.0
    return round(min(1.0, max(0.0, fit)), 2), reasons


def load_targets(config: dict[str, Any]) -> dict[str, Any]:
    """Profile roles/skills (or find-section overrides) + scoring knobs."""
    find = config.get("find") or {}
    profile = config.get("profile") or {}
    return {
        "target_titles": [str(t) for t in find.get("target_titles") or profile.get("target_roles") or []],
        "skills": [str(s) for s in find.get("skills") or profile.get("target_skills") or []],
        "seniority": [str(s) for s in find.get("seniority") or DEFAULT_SENIORITY],
        "locations": [str(loc) for loc in find.get("locations") or []],
        # Hard veto on titles the applicant is NOT qualified for — token
        # overlap alone kept shortlisting developer roles for a designer.
        "exclude_titles": [str(e) for e in find.get("exclude_titles") or []],
    }


def resolve_orgs(requested: list[str], configured: dict[str, str]) -> dict[str, str]:
    """Positional org args -> {org: board}. Accepts 'name' (from config) or
    'name:board' (greenhouse|lever|ashby|workable). No args -> the configured set."""
    if not requested:
        return dict(configured or DEFAULT_ORGS)
    boards: dict[str, str] = {}
    for item in requested:
        name, sep, board = item.partition(":")
        name, board = name.strip().lower(), board.strip().lower()
        if not name:
            raise FindError(f"bad org {item!r} — pass org or org:board")
        if sep:
            if board not in BOARDS:
                raise FindError(f"unknown board {board!r} for {name} (expected one of: {', '.join(BOARDS)})")
        elif name in (configured or {}):
            board = configured[name]
        else:
            raise FindError(
                f"unknown org {name!r} — pass {name}:greenhouse|lever|ashby|workable "
                "or add it to find.orgs in config.json"
            )
        boards[name] = board
    return boards


def find_jobs(
    boards: dict[str, str],
    targets: dict[str, Any],
    threshold: float = 0.6,
    limit: int = 10,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch every board, score every posting, rank, filter -> shortlist."""
    postings: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for org, board in sorted(boards.items()):
        try:
            postings.extend(fetch_board_jobs(org, board, timeout))
        except (RuntimeError, FindError) as exc:
            errors.append({"org": org, "board": board, "error": str(exc)})
    scored: list[dict[str, Any]] = []
    for posting in postings:
        fit, reasons = score_fit(posting, targets)
        entry = {key: posting[key] for key in ("role", "org", "board", "url", "location")}
        entry["fit"] = fit
        entry["reasons"] = reasons
        scored.append(entry)
    scored.sort(key=lambda job: (-job["fit"], job["role"].lower(), job["url"]))
    kept = [job for job in scored if job["fit"] >= threshold][: max(0, limit)]
    return {"jobs": kept, "scanned": len(scored), "errors": errors}


def write_shortlist(
    data_dir: Path | str, result: dict[str, Any], targets: dict[str, Any] | None = None
) -> Path:
    """Persist the ranked shortlist to <data-dir>/jobs.json for Lane 3."""
    out_dir = Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "jobs.json"
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "targets": targets or {},
        "jobs": result.get("jobs") or [],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
