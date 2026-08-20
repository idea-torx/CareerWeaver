"""SQLite data model: facts, skills, domains, lenses, jobs, resumes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config as cfg
from .domains import DOMAINS

FACT_KINDS = (
    "role",
    "project",
    "metric",
    "tool",
    "award",
    "client",
    "education",
    "skill",
    "domain",
    # extension (documented): seed profile/summary paragraphs, kept so a lens can
    # re-use Leo's own voice instead of synthesising one.
    "summary",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    title       TEXT,
    org         TEXT,
    start       TEXT,
    end         TEXT,
    location    TEXT,
    bullets     TEXT NOT NULL DEFAULT '[]',
    metrics     TEXT NOT NULL DEFAULT '[]',
    tags        TEXT NOT NULL DEFAULT '[]',
    source      TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    aliases     TEXT NOT NULL DEFAULT '[]',
    sort_key    TEXT,
    fingerprint TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS skills (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    domains TEXT NOT NULL DEFAULT '[]',
    key     TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS domains (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE,
    label TEXT
);

CREATE TABLE IF NOT EXISTS lenses (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,
    target_titles     TEXT NOT NULL DEFAULT '[]',
    lead_domains      TEXT NOT NULL DEFAULT '[]',
    compress_domains  TEXT NOT NULL DEFAULT '[]',
    summary_tone      TEXT,
    skills_order      TEXT NOT NULL DEFAULT '[]',
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT,
    title           TEXT,
    company         TEXT,
    raw_text        TEXT,
    skills_required TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resumes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lens         TEXT,
    job_id       INTEGER,
    status       TEXT NOT NULL DEFAULT 'draft',
    format       TEXT NOT NULL DEFAULT 'md',
    path         TEXT,
    created_at   TEXT NOT NULL,
    prompt_used  TEXT,
    source_facts TEXT NOT NULL DEFAULT '[]',
    structure    TEXT,
    provider     TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS applications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id  INTEGER,
    job_id     INTEGER,
    status     TEXT NOT NULL DEFAULT 'dry_run',
    url        TEXT,
    company    TEXT,
    title      TEXT,
    payload    TEXT,
    response   TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (resume_id) REFERENCES resumes(id),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(kind);
CREATE INDEX IF NOT EXISTS idx_applications_resume ON applications(resume_id);
CREATE INDEX IF NOT EXISTS idx_facts_org ON facts(org);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path(data_dir))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(data_dir: Path) -> sqlite3.Connection:
    """Create the schema and seed the domain taxonomy. Idempotent."""
    conn = connect(data_dir)
    conn.executescript(SCHEMA)
    for name, label, _kw in DOMAINS:
        conn.execute(
            "INSERT INTO domains (name, label) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET label = excluded.label",
            (name, label),
        )
    conn.commit()
    return conn


def loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return [] if default is None else default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return [] if default is None else default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def row_to_fact(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for field in ("bullets", "metrics", "tags", "aliases"):
        d[field] = loads(d.get(field))
    return d


def upsert_fact(conn: sqlite3.Connection, fact: dict[str, Any]) -> int:
    """Insert a fact, or merge into the existing row with the same fingerprint.

    Merge semantics: bullets/metrics/tags/aliases become the union (order
    preserved, near-duplicate bullets collapsed by the caller), dates and
    location fill in if missing.
    """
    fp = fact["fingerprint"]
    cur = conn.execute("SELECT * FROM facts WHERE fingerprint = ?", (fp,))
    existing = cur.fetchone()
    if existing is None:
        cur = conn.execute(
            """INSERT INTO facts
               (kind, title, org, start, end, location, bullets, metrics, tags,
                source, verified, aliases, sort_key, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact["kind"],
                fact.get("title"),
                fact.get("org"),
                fact.get("start"),
                fact.get("end"),
                fact.get("location"),
                dumps(fact.get("bullets", [])),
                dumps(fact.get("metrics", [])),
                dumps(fact.get("tags", [])),
                fact.get("source"),
                int(fact.get("verified", 0)),
                dumps(fact.get("aliases", [])),
                fact.get("sort_key"),
                fp,
            ),
        )
        return int(cur.lastrowid or 0)

    old = row_to_fact(existing)
    merged_bullets = _merge_bullets(old["bullets"], fact.get("bullets", []))
    merged_metrics = _merge_unique(old["metrics"], fact.get("metrics", []))
    merged_tags = _merge_unique(old["tags"], fact.get("tags", []))
    aliases = _merge_unique(
        old["aliases"] + ([old["title"]] if old.get("title") else []),
        fact.get("aliases", []) + ([fact.get("title")] if fact.get("title") else []),
    )
    title = old.get("title") or fact.get("title")
    aliases = [a for a in aliases if a and a != title]
    sources = _merge_unique(
        (old.get("source") or "").split(",") if old.get("source") else [],
        [fact.get("source")] if fact.get("source") else [],
    )
    conn.execute(
        """UPDATE facts SET title = ?, org = ?, start = ?, end = ?, location = ?,
               bullets = ?, metrics = ?, tags = ?, source = ?, aliases = ?, sort_key = ?
           WHERE id = ?""",
        (
            title,
            old.get("org") or fact.get("org"),
            _prefer_date(old.get("start"), fact.get("start")),
            _prefer_end(old.get("end"), fact.get("end")),
            old.get("location") or fact.get("location"),
            dumps(merged_bullets),
            dumps(merged_metrics),
            dumps(merged_tags),
            ",".join(s for s in sources if s),
            dumps(aliases),
            old.get("sort_key") or fact.get("sort_key"),
            existing["id"],
        ),
    )
    return int(existing["id"])


def _prefer_date(old: str | None, new: str | None) -> str | None:
    """Keep the more specific start date ('May 2026' beats a bare '2026')."""
    if not old:
        return new
    if not new:
        return old
    return new if (len(new.split()) > len(old.split())) else old


def _prefer_end(old: str | None, new: str | None) -> str | None:
    """A concrete end date beats 'Present' — the newer seeds close out old roles."""
    if not old:
        return new
    if not new:
        return old
    old_open = old.strip().lower() in ("present", "current", "now", "ongoing")
    new_open = new.strip().lower() in ("present", "current", "now", "ongoing")
    if old_open and not new_open:
        return new
    if new_open and not old_open:
        return old
    return _prefer_date(old, new)


def _merge_unique(a: Iterable[Any], b: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for item in list(a) + list(b):
        if item in (None, ""):
            continue
        key = str(item).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _merge_bullets(a: Sequence[str], b: Sequence[str]) -> list[str]:
    """Union of bullets, collapsing near-duplicates (keep the longest wording)."""
    from difflib import SequenceMatcher

    from .domains import normalize

    out: list[str] = []
    norms: list[str] = []
    for bullet in list(a) + list(b):
        text = (bullet or "").strip()
        if not text:
            continue
        n = normalize(text)
        replaced = False
        for i, existing in enumerate(norms):
            if existing == n or SequenceMatcher(None, existing, n).ratio() > 0.88:
                if len(text) > len(out[i]):
                    out[i] = text
                    norms[i] = n
                replaced = True
                break
        if not replaced:
            out.append(text)
            norms.append(n)
    return out


def get_facts(
    conn: sqlite3.Connection, kinds: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        rows = conn.execute(
            f"SELECT * FROM facts WHERE kind IN ({placeholders}) ORDER BY id", tuple(kinds)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM facts ORDER BY id").fetchall()
    return [row_to_fact(r) for r in rows]


def upsert_skill(conn: sqlite3.Connection, name: str, domains: Sequence[str]) -> int:
    from .domains import normalize

    key = normalize(name)
    if not key:
        return 0
    row = conn.execute("SELECT * FROM skills WHERE key = ?", (key,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO skills (name, domains, key) VALUES (?, ?, ?)",
            (name.strip(), dumps(list(dict.fromkeys(domains))), key),
        )
        return int(cur.lastrowid or 0)
    merged = _merge_unique(loads(row["domains"]), domains)
    conn.execute("UPDATE skills SET domains = ? WHERE id = ?", (dumps(merged), row["id"]))
    return int(row["id"])


def get_skills(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM skills ORDER BY id").fetchall()
    return [{"id": r["id"], "name": r["name"], "domains": loads(r["domains"])} for r in rows]


def add_job(
    conn: sqlite3.Connection,
    url: str | None,
    title: str | None,
    company: str | None,
    raw_text: str,
    skills_required: Sequence[str],
) -> int:
    cur = conn.execute(
        """INSERT INTO jobs (url, title, company, raw_text, skills_required, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (url, title, company, raw_text, dumps(list(skills_required)), now()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["skills_required"] = loads(d["skills_required"])
    return d


def find_job_by_url(conn: sqlite3.Connection, url: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["skills_required"] = loads(d["skills_required"])
    return d


def add_resume(
    conn: sqlite3.Connection,
    lens: str | None,
    job_id: int | None,
    fmt: str,
    path: str,
    prompt_used: str,
    source_facts: Sequence[int],
    structure: dict[str, Any],
    provider: str,
    status: str = "draft",
) -> int:
    cur = conn.execute(
        """INSERT INTO resumes
           (lens, job_id, status, format, path, created_at, prompt_used, source_facts,
            structure, provider)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            lens,
            job_id,
            status,
            fmt,
            path,
            now(),
            prompt_used,
            dumps(list(source_facts)),
            dumps(structure),
            provider,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_resume(conn: sqlite3.Connection, resume_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["source_facts"] = loads(d["source_facts"])
    d["structure"] = loads(d["structure"], default={})
    return d


def add_application(
    conn: sqlite3.Connection,
    resume_id: int,
    job_id: int | None,
    status: str,
    payload: dict[str, Any],
    response: dict[str, Any] | None = None,
    url: str | None = None,
    company: str | None = None,
    title: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO applications
           (resume_id, job_id, status, url, company, title, payload, response, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            resume_id,
            job_id,
            status,
            url,
            company,
            title,
            dumps(payload),
            dumps(response) if response is not None else None,
            now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def set_application_response(
    conn: sqlite3.Connection, application_id: int, response: dict[str, Any]
) -> None:
    """Re-stamp a row's response — used to store the run's completion block,
    which cannot be written until the row (and so its id) exists."""
    conn.execute(
        "UPDATE applications SET response = ? WHERE id = ?",
        (dumps(response), application_id),
    )
    conn.commit()


def get_applications(conn: sqlite3.Connection, limit: int | None = None) -> list[dict[str, Any]]:
    sql = """SELECT a.*, r.lens AS lens, r.path AS resume_path, r.format AS resume_format
             FROM applications a LEFT JOIN resumes r ON r.id = a.resume_id
             ORDER BY a.id DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for row in conn.execute(sql):
        entry = dict(row)
        entry["payload"] = loads(entry.get("payload"), default={})
        entry["response"] = loads(entry.get("response"), default={})
        out.append(entry)
    return out


def get_application(conn: sqlite3.Connection, application_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
    if row is None:
        return None
    entry = dict(row)
    entry["payload"] = loads(entry.get("payload"), default={})
    entry["response"] = loads(entry.get("response"), default={})
    return entry


def get_resumes(conn: sqlite3.Connection, limit: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id, lens, job_id, status, format, path, created_at, provider FROM resumes ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(row) for row in conn.execute(sql)]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    kinds = {
        r["kind"]: r["n"]
        for r in conn.execute("SELECT kind, COUNT(*) n FROM facts GROUP BY kind ORDER BY kind")
    }
    total = sum(kinds.values())
    verified = conn.execute("SELECT COUNT(*) n FROM facts WHERE verified = 1").fetchone()["n"]
    sources = [
        r["source"]
        for r in conn.execute("SELECT DISTINCT source FROM facts WHERE source IS NOT NULL")
    ]
    seed_files = sorted({s for entry in sources for s in (entry or "").split(",") if s})
    return {
        "facts_total": total,
        "facts_by_kind": kinds,
        "verified": verified,
        "verified_pct": round(100.0 * verified / total, 1) if total else 0.0,
        "skills": conn.execute("SELECT COUNT(*) n FROM skills").fetchone()["n"],
        "domains": conn.execute("SELECT COUNT(*) n FROM domains").fetchone()["n"],
        "lenses": conn.execute("SELECT COUNT(*) n FROM lenses").fetchone()["n"],
        "jobs": conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"],
        "resumes": conn.execute("SELECT COUNT(*) n FROM resumes").fetchone()["n"],
        "applications": conn.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"],
        "seed_files": seed_files,
    }
