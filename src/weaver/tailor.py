"""The tailor engine: fact graph + lens (+ job posting) -> structured resume.

Same facts, different emphasis. The LLM may re-word, re-order, re-group,
emphasize and compress; it may never add experience. Without a key the
deterministic path produces the same structure by scoring every fact against
the lens's lead domains.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Sequence

from . import db, guardrail, llm, textutil
from .domains import DOMAIN_NAMES, canonical_domain, label, score_domains

MAX_ROLES = 16  # full career history — never drop a role
LEAD_ROLE_BULLETS = 8
MID_ROLE_BULLETS = 7
TAIL_ROLE_BULLETS = 6
MAX_SKILL_ITEMS = 14

TAILOR_SYSTEM = """You are CareerWeaver's tailoring engine.

LENS SPEC
{lens_spec}

FACT GRAPH (ground truth — the ONLY permissible source of experience)
{fact_graph}

CONTACT
{contact}

{constraints}
RULES
1. The fact graph is ground truth. NEVER invent an organization, role, date,
   metric, client, award, or accomplishment that is not in it.
2. You MAY re-word, re-order, re-group, emphasize, and compress facts to fit
   the lens and the job posting.
3. Lead with the lens's lead domains; compress the lens's compress domains.
4. Pick each role's title from its `title` or `aliases` — do not invent titles.
5. BREVITY (non-negotiable): each bullet ≤ 16 words, one claim per bullet. Cut filler
   ("was responsible for", "helped", "worked on", "led efforts"). Profile: 2-3 sentences.
   Skills: tight keyword groups, no sentences.
6. PERSUASION: open bullets with a strong verb; lead with the OUTCOME/impact, then the
   mechanism. Use the fact's own numbers whenever they exist. Active voice, zero hedging,
   confident and specific. Mirror the job posting's vocabulary where your facts support it.
7. COMPLETENESS: keep EVERY role from the fact graph and every real bullet/accomplishment —
   brevity tightens the phrasing, it never removes experience. Order roles by relevance to
   the lens/job, but all roles appear.
8. FLAGSHIPS: lead the profile with 2-3 NAMED flagship accomplishments (a named video,
   campaign, product, or client win) — proof beats adjectives.
9. NAMED WORK: every named campaign/launch/project keeps its OWN bullet — never merge named
   work into a laundry list.
10. METRICS: surface portfolio/social proof metrics (views, followers, fidelity, assets)
    whenever the facts contain them.
11. VOICE: adopt the lens's summary_tone — including owner-operator lines ("I cut the work
    myself when the deadline needs it") when the facts support the framing.
12. BULLET ORDER: inside EVERY role, order bullets by relevance to the lens and the job
    posting — the strongest, most on-target bullet FIRST. Recruiters skim the first bullet
    of each role; it must be the best match, not the chronological first.
13. TITLES FLEX: flex EVERY role title toward the lens — when the role's aliases include a
    variant that fits the lens better than the raw title, use that alias (e.g. under a
    creative lens, "Design Engineer & SRE" or "Creative Technologist" beats the engineering
    title). Never invent a title.
14. Return ONLY this JSON object:
{{"title": str,
  "summary": str,
  "experience": [{{"role": str, "org": str, "dates": str, "location": str,
                  "bullets": [str], "fact_id": int}}],
  "skills": [{{"domain": str, "items": [str]}}],
  "education": [{{"degree": str, "school": str, "year": str}}],
  "awards": [str],
  "clients": [str]}}
"""


# ------------------------------------------------------------------ lens scoring


def lens_weights(lens: dict[str, Any]) -> dict[str, float]:
    """Lead domains pull hard, compressed domains push back, the rest is neutral."""
    weights: dict[str, float] = {}
    lead = [d for d in lens.get("lead_domains", []) if d in DOMAIN_NAMES]
    for index, domain in enumerate(lead):
        weights[domain] = 4.0 - 0.5 * index
    for domain in lens.get("compress_domains", []):
        if domain not in weights:
            weights[domain] = -1.0
    for domain in DOMAIN_NAMES:
        weights.setdefault(domain, 0.5)
    return weights


def text_score(text: str, weights: dict[str, float], cap: int = 4) -> float:
    scores = score_domains(text)
    return sum(weights.get(domain, 0.0) * min(hits, cap) for domain, hits in scores.items())


def _recency_bonus(sort_key: str | None) -> float:
    match = re.match(r"(\d{4})-(\d{2})", sort_key or "")
    if not match:
        return 0.0
    year, month = int(match.group(1)), int(match.group(2))
    if year == 0:
        return 0.0
    return max(0.0, (year + month / 12.0 - 2020.0)) * 0.45


def score_role(role: dict[str, Any], weights: dict[str, float]) -> float:
    bullets = role.get("bullets") or []
    bullet_scores = sorted((text_score(b, weights) for b in bullets), reverse=True)
    top = bullet_scores[:6]
    mean_bullets = sum(top) / len(top) if top else 0.0
    titles = [role.get("title") or ""] + list(role.get("aliases") or [])
    best_title = max((text_score(t, weights) for t in titles), default=0.0)
    return mean_bullets + 0.5 * best_title + _recency_bonus(role.get("sort_key"))


def pick_title(role: dict[str, Any], weights: dict[str, float]) -> str:
    """Choose the title variant that fits the lens ('AI Engineering Lead' vs
    'Multimedia Production, AI & CGI Lead' — same job, different story)."""
    candidates = [t for t in [role.get("title")] + list(role.get("aliases") or []) if t]
    if not candidates:
        return ""
    scored = [(text_score(t, weights), -len(t), t) for t in candidates]
    scored.sort(reverse=True)
    return scored[0][2]


def format_dates(role: dict[str, Any]) -> str:
    start, end = (role.get("start") or "").strip(), (role.get("end") or "").strip()
    if start and end:
        return f"{start} – {end}"
    return start or end or ""


# ----------------------------------------------------------- deterministic build


def build_structure(
    conn: sqlite3.Connection,
    lens: dict[str, Any],
    profile: dict[str, Any],
    job: dict[str, Any] | None = None,
    max_roles: int = MAX_ROLES,
) -> dict[str, Any]:
    """The deterministic fallback — re-emit facts grouped by the lens."""
    weights = lens_weights(lens)
    if job:
        weights = _bias_for_job(weights, job)

    roles = db.get_facts(conn, ["role"])
    ranked = sorted(roles, key=lambda r: (-score_role(r, weights), r.get("sort_key") or ""))
    ranked = ranked[:max_roles]

    experience: list[dict[str, Any]] = []
    used_ids: list[int] = []
    # One claim, one bullet — across the whole document, not just within a role.
    claimed_bullets: list[str] = []
    for index, role in enumerate(ranked):
        if index < 3:
            limit = LEAD_ROLE_BULLETS
        elif index < 5:
            limit = MID_ROLE_BULLETS
        else:
            limit = TAIL_ROLE_BULLETS
        bullets = _select_bullets(role.get("bullets") or [], weights, limit, claimed_bullets)
        experience.append(
            {
                "role": pick_title(role, weights),
                "org": role.get("org") or "",
                "dates": format_dates(role),
                "location": role.get("location") or "",
                "bullets": bullets,
                "fact_id": role.get("id"),
            }
        )
        used_ids.append(int(role["id"]))

    summary, summary_ids = _select_summary(conn, lens, weights, job)
    used_ids.extend(summary_ids)

    skills, skill_ids = _select_skills(conn, lens)
    used_ids.extend(skill_ids)

    education = []
    for fact in db.get_facts(conn, ["education"]):
        education.append(
            {
                "degree": fact.get("title") or "",
                "school": fact.get("org") or "",
                "year": (fact.get("end") or fact.get("start") or ""),
            }
        )
        used_ids.append(int(fact["id"]))

    award_facts = sorted(
        db.get_facts(conn, ["award"]), key=lambda f: -text_score(f.get("title") or "", weights)
    )
    awards = [f.get("title") or "" for f in textutil.dedupe(award_facts, key=lambda f: f.get("title") or "")]
    used_ids.extend(int(f["id"]) for f in award_facts)

    client_facts = db.get_facts(conn, ["client"])
    clients = textutil.dedupe_names(f.get("title") or "" for f in client_facts)
    used_ids.extend(int(f["id"]) for f in client_facts)

    structure = {
        "name": profile.get("name") or "",
        "title": (lens.get("target_titles") or [""])[0],
        "contact": _contact_block(profile),
        "summary": summary,
        "experience": experience,
        "skills": skills,
        "education": education,
        "awards": awards,
        "clients": clients,
    }
    structure["_source_facts"] = sorted(set(used_ids))
    return structure


def _bias_for_job(weights: dict[str, float], job: dict[str, Any]) -> dict[str, float]:
    """Nudge domain weights toward whatever the posting actually asks for."""
    text = " ".join(
        str(job.get(key) or "") for key in ("title", "company", "raw_text")
    ) + " " + " ".join(job.get("skills_required") or [])
    scores = score_domains(text)
    total = sum(scores.values()) or 1
    biased = dict(weights)
    for domain, hits in scores.items():
        biased[domain] = biased.get(domain, 0.5) + 2.0 * (hits / total)
    return biased


def _select_bullets(
    bullets: Sequence[str],
    weights: dict[str, float],
    limit: int,
    claimed: list[str] | None = None,
) -> list[str]:
    """Best-fitting bullets for the lens, with near-duplicates collapsed.

    Dedupe runs before the cap so a role that has the same claim written three
    ways still contributes `limit` distinct bullets.
    """
    scored = [(text_score(b, weights), index, b) for index, b in enumerate(bullets)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    ordered = [b for _score, _index, b in scored]
    unique = textutil.dedupe(ordered)
    if claimed is not None:
        unique = [
            b for b in unique if not any(textutil.is_near_duplicate(b, c) for c in claimed)
        ]
    chosen = unique[:limit]
    if claimed is not None:
        claimed.extend(chosen)
    return chosen


def _select_summary(
    conn: sqlite3.Connection,
    lens: dict[str, Any],
    weights: dict[str, float],
    job: dict[str, Any] | None,
) -> tuple[str, list[int]]:
    """One summary — the single paragraph that best fits the lens.

    Blending two seed summaries reads like two people wrote it, so the best
    match wins outright.
    """
    facts = db.get_facts(conn, ["summary"])
    scored = sorted(
        ((text_score(f.get("title") or "", weights), f) for f in facts),
        key=lambda pair: -pair[0],
    )
    if not scored:
        titles = lens.get("target_titles") or []
        return (
            f"{titles[0] if titles else 'Candidate'}. {lens.get('summary_tone') or ''}".strip(),
            [],
        )
    _score, best = scored[0]
    return (best.get("title") or "").strip(), [int(best["id"])]


def _select_skills(
    conn: sqlite3.Connection, lens: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[int]]:
    skills = db.get_skills(conn)
    order = [d for d in (lens.get("skills_order") or lens.get("lead_domains") or []) if d]
    for domain in lens.get("lead_domains", []):
        if domain not in order:
            order.append(domain)
    groups: list[dict[str, Any]] = []
    used: list[int] = []
    claimed: set[str] = set()
    compress = set(lens.get("compress_domains") or [])
    for domain in order:
        items: list[str] = []
        for skill in skills:
            if domain not in (skill.get("domains") or []):
                continue
            key = skill["name"].lower()
            if key in claimed:
                continue
            items.append(skill["name"])
            claimed.add(key)
            used.append(int(skill["id"]))
            if len(items) >= MAX_SKILL_ITEMS:
                break
        items = textutil.dedupe_terms(items)
        if len(items) >= (4 if domain in compress else 2):
            groups.append({"domain": label(domain), "domain_key": domain, "items": items})
    return groups, used


MAX_LINKS = 4


def _contact_block(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": profile.get("email") or "",
        "phone": profile.get("phone") or "",
        "location": profile.get("location") or "",
        "links": _dedupe_links(profile.get("links") or []),
    }


def _dedupe_links(links: Sequence[str]) -> list[str]:
    """One link per host — two LinkedIn URLs in a header reads like a mistake."""
    out: list[str] = []
    hosts: set[str] = set()
    for link in links:
        value = (link or "").strip()
        if not value:
            continue
        host = re.sub(r"^https?://", "", value).split("/")[0].lower()
        if host in hosts:
            continue
        hosts.add(host)
        out.append(value)
        if len(out) >= MAX_LINKS:
            break
    return out


# --------------------------------------------------------------------- llm build


def fact_graph_payload(conn: sqlite3.Connection, lens: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact, id-carrying view of the graph for the prompt (lead domains first)."""
    weights = lens_weights(lens)
    payload: list[dict[str, Any]] = []
    for fact in db.get_facts(conn):
        entry: dict[str, Any] = {
            "id": fact["id"],
            "kind": fact["kind"],
            "title": fact.get("title"),
        }
        for key in ("org", "start", "end", "location"):
            if fact.get(key):
                entry[key] = fact[key]
        if fact.get("bullets"):
            entry["bullets"] = fact["bullets"]
        if fact.get("aliases"):
            entry["aliases"] = fact["aliases"]
        if fact.get("tags"):
            entry["domains"] = fact["tags"]
        if fact.get("metrics"):
            entry["metrics"] = fact["metrics"]
        entry["verified"] = bool(fact.get("verified"))
        payload.append(entry)
    payload.sort(
        key=lambda e: -text_score(
            " ".join([str(e.get("title") or "")] + list(e.get("bullets") or [])), weights
        )
    )
    return payload


def build_prompt(
    conn: sqlite3.Connection, lens: dict[str, Any], profile: dict[str, Any]
) -> str:
    lens_spec = json.dumps(
        {
            "name": lens.get("name"),
            "target_titles": lens.get("target_titles"),
            "lead_domains": lens.get("lead_domains"),
            "compress_domains": lens.get("compress_domains"),
            "summary_tone": lens.get("summary_tone"),
            "skills_order": lens.get("skills_order"),
            "notes": lens.get("notes"),
        },
        ensure_ascii=False,
        indent=2,
    )
    graph = json.dumps(fact_graph_payload(conn, lens), ensure_ascii=False)
    contact = json.dumps(
        {"name": profile.get("name"), **_contact_block(profile)}, ensure_ascii=False
    )
    return TAILOR_SYSTEM.format(
        lens_spec=lens_spec,
        fact_graph=graph,
        contact=contact,
        constraints=hard_constraints(profile),
    )


def hard_constraints(profile: dict[str, Any]) -> str:
    """The do-not-mention list + the never-volunteer-compensation rule.

    Both are enforced in code afterwards (guardrail); the prompt carries them so
    the model does not have to be corrected by a refusal to get it right.
    """
    blocks = [
        guardrail.avoid_clause(profile.get("avoid")),
        guardrail.COMPENSATION_RULE,
    ]
    return "\n\n".join(block for block in blocks if block) + "\n"


def _coerce_llm_structure(
    data: dict[str, Any], fallback: dict[str, Any]
) -> dict[str, Any] | None:
    """Validate the model's JSON into our shape; None means 'use the fallback'."""
    if not isinstance(data, dict) or not isinstance(data.get("experience"), list):
        return None
    experience: list[dict[str, Any]] = []
    for entry in data["experience"]:
        if not isinstance(entry, dict):
            continue
        bullets = [str(b).strip() for b in (entry.get("bullets") or []) if str(b).strip()]
        role = {
            "role": str(entry.get("role") or "").strip(),
            "org": str(entry.get("org") or "").strip(),
            "dates": str(entry.get("dates") or "").strip(),
            "location": str(entry.get("location") or "").strip(),
            "bullets": bullets,
        }
        if entry.get("fact_id") is not None:
            try:
                role["fact_id"] = int(entry["fact_id"])
            except (TypeError, ValueError):
                pass
        if role["role"] or role["org"]:
            experience.append(role)
    if not experience:
        return None

    skills: list[dict[str, Any]] = []
    for entry in data.get("skills") or []:
        if isinstance(entry, dict) and entry.get("items"):
            skills.append(
                {
                    "domain": canonical_domain(str(entry.get("domain") or "Skills")),
                    "items": [str(i).strip() for i in entry["items"] if str(i).strip()],
                }
            )
    education: list[dict[str, Any]] = []
    for entry in data.get("education") or []:
        if isinstance(entry, dict):
            education.append(
                {
                    "degree": str(entry.get("degree") or ""),
                    "school": str(entry.get("school") or ""),
                    "year": str(entry.get("year") or ""),
                }
            )
        elif isinstance(entry, str):
            education.append({"degree": entry, "school": "", "year": ""})

    return {
        "name": fallback.get("name") or "",
        "title": str(data.get("title") or fallback.get("title") or ""),
        "contact": fallback.get("contact", {}),
        "summary": str(data.get("summary") or fallback.get("summary") or ""),
        "experience": experience,
        "skills": skills or fallback.get("skills", []),
        "education": education or fallback.get("education", []),
        "awards": [str(a) for a in (data.get("awards") or fallback.get("awards", []))],
        "clients": [str(c) for c in (data.get("clients") or fallback.get("clients", []))],
    }


# ------------------------------------------------------------------------ entry


def tailor(
    conn: sqlite3.Connection,
    lens: dict[str, Any],
    profile: dict[str, Any],
    job: dict[str, Any] | None = None,
    use_llm: bool = True,
    max_roles: int = MAX_ROLES,
) -> dict[str, Any]:
    """Produce the tailored structure plus its audit trail. Never raises on LLM issues."""
    fallback = build_structure(conn, lens, profile, job=job, max_roles=max_roles)
    source_facts = fallback.pop("_source_facts", [])
    structure = fallback
    provider = "deterministic"
    fallback_reason: str | None = None
    prompt = build_prompt(conn, lens, profile)

    if use_llm and llm.available():
        user = _user_message(lens, job)
        data = llm.complete_json(prompt, user)
        if data.get("_fallback"):
            fallback_reason = str(data.get("_reason") or "provider unavailable")
        else:
            coerced = _coerce_llm_structure(data, fallback)
            if coerced is None:
                fallback_reason = "model output failed validation; used deterministic build"
            else:
                structure = coerced
                provider = "openai"
                source_facts = sorted(
                    set(source_facts)
                    | {int(e["fact_id"]) for e in coerced["experience"] if e.get("fact_id")}
                )
    elif use_llm:
        fallback_reason = "no api key (OPENAI_API_KEY unset)"

    # The do-not-mention list is enforced in code, on both paths: the
    # deterministic build re-emits the facts verbatim (an avoided employer is
    # still IN the graph), and a model that ignored the prompt cannot ship it
    # either. Whatever names an avoid topic is dropped before anything renders.
    avoided = guardrail.avoid_hits(
        " ".join(text for _path, text in guardrail.walk_strings(structure)),
        profile.get("avoid"),
    )
    if avoided:
        structure = guardrail.redact_avoided(structure, profile.get("avoid"))

    corpus = guardrail.build_corpus(
        conn,
        extra=[
            profile.get("name") or "",
            profile.get("title") or "",
            profile.get("location") or "",
            profile.get("email") or "",
            profile.get("phone") or "",
            " ".join(profile.get("links") or []),
        ],
    )
    unverified = guardrail.scan(structure, corpus, skip_paths=("contact", "name"))

    return {
        "lens": lens.get("name"),
        "provider": provider,
        "fallback_reason": fallback_reason,
        "job_id": (job or {}).get("id"),
        "structure": structure,
        "source_facts": source_facts,
        "unverified_mentions": unverified,
        "avoided": avoided,
        "prompt_used": prompt,
    }


def _user_message(lens: dict[str, Any], job: dict[str, Any] | None) -> str:
    if job and (job.get("raw_text") or job.get("title")):
        parts = [
            "JOB POSTING",
            f"Title: {job.get('title') or 'unknown'}",
            f"Company: {job.get('company') or 'unknown'}",
            f"URL: {job.get('url') or 'n/a'}",
            "",
            (job.get("raw_text") or "")[:20000],
        ]
        return "\n".join(parts) + "\n\nTailor the resume to this posting through the lens."
    titles = lens.get("target_titles") or []
    return (
        "No job posting. Tailor the resume to the lens itself; target title: "
        f"{titles[0] if titles else lens.get('name')}."
    )
