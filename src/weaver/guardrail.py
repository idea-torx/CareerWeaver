"""Guardrail: every factual claim in a generated resume must trace to the graph.

The model may re-word, re-order, re-group, emphasize or compress. It may not
introduce an organization, a date, or a metric that is not already a fact. This
scan is enforced in code, after generation, and its findings land in the
`unverified_mentions` field of `--json` output.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Sequence

from .domains import DOMAIN_LABELS, DOMAIN_NAMES, TOOLS, normalize

ENTITY_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.'’\-]*"
    r"(?:\s+(?:of|the|and|for|de|x|&)\s+[A-Z][A-Za-z0-9&.'’\-]*|\s+[A-Z][A-Za-z0-9&.'’\-]*)*)"
)

# Resume bullets open with a capitalized action verb — the org scanner must not
# mistake "Owned AI-disclosure" or "Shipped CGI" for companies.
RESUME_VERBS: set[str] = {
    "architected", "built", "completed", "contributed", "created", "delivered",
    "deployed", "designed", "developed", "directed", "drove", "engineered",
    "established", "founded", "handled", "improved", "increased", "initiated",
    "implemented", "introduced", "launched", "led", "managed", "negotiated",
    "orchestrated", "owned", "partnered", "performed", "planned", "prepared",
    "produced", "promoted", "provided", "published", "ran", "reduced", "released",
    "researched", "resolved", "scaled", "served", "shipped", "spearheaded",
    "streamlined", "supervised", "supported", "transformed", "trained", "wrote",
    "currently", "present", "previously", "rebranded", "redesigned", "rebuilt",
    "relaunched", "grew", "co-founded", "co-developed", "co-led", "hired", "mentored",
    "taught", "automated", "optimized", "reduced", "secured", "won", "featured",
}

# Decorative prefixes agents prepend to real names ("Emmy-winning Rowdy Studio").
DECORATIVE_PREFIXES: tuple[str, ...] = (
    "emmy-winning", "award-winning", "venture-backed", "yc-backed", "forbes-listed",
    "forbes-30", "y-combinator", "vc-backed",
)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
MONTH_YEAR_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(?:19|20)\d{2}\b"
)
METRIC_RE = re.compile(
    r"(~?\$\s?\d[\d,.]*\s*[KMB]?\b|\b\d[\d,.]*\s?%|\b\d[\d,.]*\s?[KMB]?\+|\b\d+x\b)",
    re.IGNORECASE,
)

# Ordinary English / resume vocabulary that is not a claim about the world.
COMMON_WORDS: set[str] = set(
    """
    a about above across after against all also an and any are as at be because been before
    beyond both build built but by can client clients collaborated concept content core create
    created creative cross current customer defined delivered deployed design designed direct
    directed director drove during each embedded enabling end engineer engineered engineering
    every experience first for founded from full grew had has have he her his how i if in
    including into is it its junior key launch launched lead leader leadership led level
    lifecycle major manage managed managing may minor more most my new no not of on one only
    or other our over own owned partner partnered per platform present principal produced
    product production program project projects ran reached remote research resolved role roles
    ran same scale senior served shipped since so software sole some staff summary team teams
    that the their them then there these they this through time to today took total under up
    us use used using various via was we were what when where which while who why will with
    within without work worked working would year years you your
    education experience skills awards recognition profile clients notable summary
    january february march april june july august september october november december
    jan feb mar apr jun jul aug sep sept oct nov dec present current now
    """.split()
)


# Vocabulary the renderer itself introduces — section headings, domain labels,
# lens/tool names. These are structure, not claims about the world, so they are
# whitelisted before the org scan rather than reported as unknown companies.
STRUCTURAL_VOCABULARY: list[str] = (
    list(DOMAIN_LABELS.values())
    + [name.replace("_", " ") for name in DOMAIN_NAMES]
    + list(TOOLS.values())
    + [
        "Profile",
        "Summary",
        "Experience",
        "Skills",
        "Education",
        "Awards & Recognition",
        "Notable Clients",
        "Projects",
        "Present",
        "Remote",
        "Resume",
        "Full-Stack Engineering",
        "Full Stack Engineering",
        "Direction & Delivery",
        "Cloud & Reliability",
        "Graphics & Brand",
        "CGI & Motion",
        "Video & Multimedia",
        "AI & Machine Learning",
        "Design Engineering",
        "Agentic Engineering",
    ]
)


def build_corpus(conn: sqlite3.Connection, extra: Iterable[str] = ()) -> str:
    """Normalized text of every fact, plus profile/contact and structural vocabulary."""
    chunks: list[str] = list(STRUCTURAL_VOCABULARY)
    for row in conn.execute(
        "SELECT title, org, start, end, location, bullets, metrics, tags, aliases FROM facts"
    ):
        chunks.extend(str(v) for v in tuple(row) if v)
    for row in conn.execute("SELECT name, domains FROM skills"):
        chunks.extend(str(v) for v in tuple(row) if v)
    for row in conn.execute("SELECT name, target_titles, notes FROM lenses"):
        chunks.extend(str(v) for v in tuple(row) if v)
    for row in conn.execute("SELECT title, company, raw_text, skills_required FROM jobs"):
        chunks.extend(str(v) for v in tuple(row) if v)
    chunks.extend(str(e) for e in extra if e)
    return normalize(" ".join(chunks))


def walk_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Flatten a structure into (json-path, string) pairs."""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        if value.strip():
            out.append((path or "$", value))
    elif isinstance(value, dict):
        for key, item in value.items():
            out.extend(walk_strings(item, f"{path}.{key}" if path else key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            out.extend(walk_strings(item, f"{path}[{index}]"))
    return out


def _known(candidate: str, corpus: str) -> bool:
    value = normalize(candidate)
    if not value:
        return True
    if value in corpus:
        return True
    # Possessive / trailing-s variance: "Caviar's" → "Caviar".
    if value.endswith(("'s", "’s")):
        value = value[:-2]
        if value in corpus:
            return True
    # Strip decorative prefixes and re-check ("Emmy-winning Rowdy Studio" → "Rowdy Studio").
    lowered = value
    for prefix in DECORATIVE_PREFIXES:
        if lowered.startswith(prefix):
            rest = lowered[len(prefix):].strip()
            if rest and rest in corpus:
                return True
    return False


def _is_boring(candidate: str) -> bool:
    words = [w for w in normalize(candidate).split() if w]
    if not words:
        return True
    if len("".join(words)) < 3:
        return True
    if words[0] in RESUME_VERBS:
        return True  # bullet-start verb phrase, not an organization
    return all(word in COMMON_WORDS for word in words)


def scan(structure: Any, corpus: str, skip_paths: Sequence[str] = ()) -> list[dict[str, str]]:
    """Return claims in `structure` that do not trace back to the fact graph."""
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for path, text in walk_strings(structure):
        if any(path.startswith(skip) for skip in skip_paths):
            continue
        for match in ENTITY_RE.finditer(text):
            candidate = match.group(1).strip(" .,;:")
            if _is_boring(candidate) or _known(candidate, corpus):
                continue
            _add(findings, seen, "org", candidate, path)
        for match in MONTH_YEAR_RE.finditer(text):
            if not _known(match.group(0), corpus):
                _add(findings, seen, "date", match.group(0), path)
        for match in YEAR_RE.finditer(text):
            if not _known(match.group(0), corpus):
                _add(findings, seen, "date", match.group(0), path)
        for match in METRIC_RE.finditer(text):
            candidate = match.group(0).strip()
            if not _known(candidate, corpus):
                _add(findings, seen, "metric", candidate, path)
    return findings


def _add(
    findings: list[dict[str, str]], seen: set[tuple[str, str]], kind: str, text: str, path: str
) -> None:
    key = (kind, normalize(text))
    if key in seen:
        return
    seen.add(key)
    findings.append({"type": kind, "text": text, "where": path})


# --------------------------------------------------------------- avoid list
#
# `profile.avoid` is the user's do-not-mention list: topics, employers or
# keywords that must never appear in anything the model writes for them (resume
# remixes, essays, "why this role", cover letters, open fields). The prompts ask
# for it; this enforces it in code, the same way the PII rule is enforced — a
# proposed text that mentions an entry is refused, not quietly shipped.
#
# Entries can be short ("Dreamcast"), so the match is whole-word: "dream" in a
# summary must not trip an entry the user spelled as "Dreamcast".


def avoid_terms(avoid: Iterable[str] | None) -> list[str]:
    """The non-empty, de-duplicated entries of an avoid list."""
    out: list[str] = []
    seen: set[str] = set()
    for entry in avoid or ():
        term = str(entry or "").strip()
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out


def _avoid_pattern(term: str) -> re.Pattern[str] | None:
    """Whole-word, case-insensitive, whitespace-tolerant matcher for one entry.

    A trailing possessive/plural still counts ("Dreamcast's", "Dreamcasts"); a
    longer word that merely STARTS with the entry does not ("dream" is not a
    mention of "Dreamcast", and "Dreamcast" is not a mention of "dream").
    """
    term = term.strip()
    if not term:
        return None
    body = r"\s+".join(re.escape(part) for part in term.split())
    prefix = r"(?<!\w)" if (term[0].isalnum() or term[0] == "_") else ""
    suffix = r"(?:['’]s|s)?(?!\w)" if (term[-1].isalnum() or term[-1] == "_") else ""
    return re.compile(prefix + body + suffix, re.IGNORECASE)


def avoid_hits(text: str, avoid: Iterable[str] | None) -> list[str]:
    """Which avoid-list entries this text mentions (in list order)."""
    hits: list[str] = []
    haystack = text or ""
    if not haystack.strip():
        return hits
    for term in avoid_terms(avoid):
        pattern = _avoid_pattern(term)
        if pattern and pattern.search(haystack):
            hits.append(term)
    return hits


def mentions_avoided(text: str, avoid: Iterable[str] | None) -> bool:
    return bool(avoid_hits(text, avoid))


def scan_avoid(
    structure: Any, avoid: Iterable[str] | None, skip_paths: Sequence[str] = ()
) -> list[dict[str, str]]:
    """Every avoid-list mention inside a structure, as guardrail findings."""
    terms = avoid_terms(avoid)
    if not terms:
        return []
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path, text in walk_strings(structure):
        if any(path.startswith(skip) for skip in skip_paths):
            continue
        for hit in avoid_hits(text, terms):
            key = ("avoid", f"{normalize(hit)}@{path}")
            if key in seen:
                continue
            seen.add(key)
            findings.append({"type": "avoid", "text": hit, "where": path})
    return findings


_SENTENCE_RE = re.compile(r"[^.!?]*[.!?]|[^.!?]+")


def _prune_text(text: str, terms: Sequence[str]) -> str:
    """Drop the sentences that mention an avoid topic; keep the rest."""
    if not avoid_hits(text, terms):
        return text
    kept = [
        part.strip()
        for part in _SENTENCE_RE.findall(text)
        if part.strip() and not avoid_hits(part, terms)
    ]
    return " ".join(kept).strip()


def _entry_is_avoided(entry: dict[str, Any], terms: Sequence[str]) -> bool:
    """A role/education entry whose own identity (org, title, dates…) is avoided."""
    return any(
        isinstance(value, str) and avoid_hits(value, terms) for value in entry.values()
    )


def _prune(value: Any, terms: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _prune_text(value, terms)
    if isinstance(value, dict):
        pruned = {key: _prune(item, terms) for key, item in value.items()}
        return pruned
    if isinstance(value, (list, tuple)):
        kept: list[Any] = []
        for item in value:
            if isinstance(item, str):
                if avoid_hits(item, terms):
                    continue
                kept.append(item)
            elif isinstance(item, dict):
                if _entry_is_avoided(item, terms):
                    continue
                pruned = _prune(item, terms)
                # A skills group whose every item was avoided is an empty heading.
                if "items" in pruned and not pruned["items"]:
                    continue
                kept.append(pruned)
            else:
                kept.append(_prune(item, terms))
        return kept
    return value


#: Structure keys that are the applicant's own identity, never a topic choice.
AVOID_SKIP_KEYS: tuple[str, ...] = ("name", "contact")


def redact_avoided(
    structure: dict[str, Any], avoid: Iterable[str] | None
) -> dict[str, Any]:
    """A copy of a tailored structure with every avoid-list mention removed.

    Roles/education whose org, title, dates or location name an avoid topic are
    dropped whole; bullets, awards, clients and skill items that mention one are
    dropped individually; free prose (summary, title) loses the sentence.
    """
    terms = avoid_terms(avoid)
    if not terms or not isinstance(structure, dict):
        return structure
    return {
        key: (value if key in AVOID_SKIP_KEYS else _prune(value, terms))
        for key, value in structure.items()
    }


def avoid_clause(avoid: Iterable[str] | None) -> str:
    """The hard constraint the tailor and apply prompts both carry."""
    terms = avoid_terms(avoid)
    if not terms:
        return ""
    return (
        "DO NOT MENTION (hard constraint — it overrides every other instruction)\n"
        "NEVER mention or imply: " + ", ".join(terms) + ". If a fact or the "
        "posting pulls toward those topics, leave them out entirely."
    )


# ------------------------------------------------------- compensation numbers
#
# A live catch: the model wrote "so excited to make them $29K" into a Ramp essay
# — unremarkable as an achievement and an unforced anchor on the applicant's own
# pay. Compensation numbers are never volunteered; they belong only in a field
# that explicitly asks for them.
#
# The pattern is deliberately salary-SHAPED, not "any dollar sign": "costs $10
# to run" is a cost, not an offer, and refusing it would gut ordinary prose.

#: Salary-shaped money: a K/M suffix, a thousands-separated figure, four+ digits,
#: or a per-hour/per-year rate. A bare "$10" is none of those.
MONEY_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s*[KMB]\b"
    r"|\$\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\$\s?\d{4,}(?:\.\d+)?"
    r"|\$\s?\d[\d,]*(?:\.\d+)?\s*(?:/|\s+per\s+)\s*(?:hr|hour|yr|year|annum|month|mo|week|wk|day)\b",
    re.IGNORECASE,
)

#: "29k" with no currency symbol — only compensation when a pay word sits in the
#: same text ("100k users" is a metric, "paid 100k" is a salary).
BARE_K_RE = re.compile(r"\b\d{2,3}\s?k\b", re.IGNORECASE)

COMPENSATION_WORD_RE = re.compile(
    r"salary|salaries|compensation|\bcomp\b|\bpay\b|\bpaid\b|\bpays\b|"
    r"\brate\b|\bwage|\bearn|\bstipend\b|\bbonus\b|\bremuneration\b",
    re.IGNORECASE,
)

#: A field label that is ALLOWED to carry a number: it asked for one.
COMPENSATION_LABEL_RE = re.compile(
    r"salary|compensation|\bcomp\b|total comp|\bpay\b|pay range|\bwage|"
    r"hourly rate|\brate\b|desired earnings|expected earnings|\bctc\b",
    re.IGNORECASE,
)


#: Applicant facts that exist ONLY to answer a field asking for compensation.
#: They are declared (so "what is your desired salary?" is answerable at all),
#: but they are NOT ordinary quotable data: the declared-datum carve-out in
#: `local_agent.volunteered_compensation` skips them, so the figure can never
#: be volunteered into an essay, a cover letter, or any field that never asked.
COMPENSATION_ONLY_KEYS = frozenset({"salary_expectation"})


def compensation_amounts(text: str) -> list[str]:
    """Salary-shaped figures in this text, in order, de-duplicated."""
    haystack = text or ""
    found = [m.group(0).strip() for m in MONEY_RE.finditer(haystack)]
    if COMPENSATION_WORD_RE.search(haystack):
        found.extend(m.group(0).strip() for m in BARE_K_RE.finditer(haystack))
    out: list[str] = []
    seen: set[str] = set()
    for amount in found:
        key = normalize(amount)
        if key not in seen:
            seen.add(key)
            out.append(amount)
    return out


def mentions_compensation(text: str) -> bool:
    return bool(compensation_amounts(text))


def asks_compensation(*labels: str | None) -> bool:
    """True when a form field's own wording asks for a compensation number."""
    return any(COMPENSATION_LABEL_RE.search(label or "") for label in labels)


COMPENSATION_RULE = "\n".join(
    [
        "COMPENSATION (hard rule — it overrides every other instruction)",
        "NEVER volunteer a compensation number: current or past salary, a rate, a",
        'bonus, or a "$29K"-style figure, in ANY text you write (resume, essay,',
        '"why this role", cover letter, open field). A compensation number belongs',
        "ONLY in a field that explicitly asks for compensation. If a fact carries",
        "your pay, leave the number out and describe the work instead.",
    ]
)


def summarize(findings: Sequence[dict[str, str]]) -> str:
    if not findings:
        return "guardrail: clean — every claim traces to the fact graph"
    parts = [f"{f['type']}:{f['text']!r} @ {f['where']}" for f in findings[:8]]
    more = f" (+{len(findings) - 8} more)" if len(findings) > 8 else ""
    return "guardrail: " + str(len(findings)) + " unverified mention(s) — " + "; ".join(parts) + more
