"""Persona lenses — the same facts, told six different ways."""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from . import db
from .domains import DOMAIN_NAMES

SEED_LENSES: list[dict[str, Any]] = [
    {
        "name": "fde",
        "target_titles": [
            "Forward Deployed AI Engineer",
            "Forward Deployed Engineer",
            "AI Engineer",
        ],
        "lead_domains": [
            "agentic_engineering",
            "ai_expertise",
            "fullstack_engineering",
            "sre_cloud",
        ],
        "compress_domains": ["cgi_motion", "graphics_brand", "video_multimedia"],
        "summary_tone": (
            "Technical and delivery-forward. Lead with shipping production software end to end "
            "by orchestrating AI coding agents, embedding with client teams, and owning the "
            "full stack. Creative background is context, not the headline."
        ),
        "skills_order": [
            "ai_expertise",
            "agentic_engineering",
            "fullstack_engineering",
            "sre_cloud",
            "design_engineering",
            "direction_pm",
        ],
        "notes": "Forward-deployed engineering roles (Anthropic, Palantir, BCG X, OpenAI FDE).",
    },
    {
        "name": "fdc",
        "target_titles": [
            "Forward Deployed Creative",
            "Forward Deployed Creative Engineer",
            "AI Creative Technologist",
        ],
        "lead_domains": ["ai_expertise", "cgi_motion", "video_multimedia", "direction_pm"],
        "compress_domains": ["sre_cloud", "fullstack_engineering"],
        "summary_tone": (
            "Creative firepower with an engineering spine. Lead with generative-AI craft, CGI "
            "and video output, and embedding with client creative teams to get AI adopted."
        ),
        "skills_order": [
            "ai_expertise",
            "cgi_motion",
            "video_multimedia",
            "direction_pm",
            "graphics_brand",
            "agentic_engineering",
        ],
        "notes": "Client-facing creative + AI hybrid roles.",
    },
    {
        "name": "design-engineer",
        "target_titles": ["Design Engineer", "Product Design Engineer", "Full Stack Design Engineer"],
        "lead_domains": [
            "design_engineering",
            "graphics_brand",
            "fullstack_engineering",
            "ai_expertise",
        ],
        "compress_domains": ["sre_cloud", "video_multimedia"],
        "summary_tone": (
            "Interface-first. Lead with product surfaces designed and shipped by one person: "
            "UI/UX direction, design systems, and the front-end and back-end behind them."
        ),
        "skills_order": [
            "design_engineering",
            "graphics_brand",
            "fullstack_engineering",
            "ai_expertise",
            "cgi_motion",
            "direction_pm",
        ],
        "notes": "Design-engineering roles at product companies.",
    },
    {
        "name": "multimedia",
        "target_titles": [
            "Full Stack Multi-Media",
            "Multimedia Design Engineer",
            "Multimedia Production Lead",
        ],
        "lead_domains": ["video_multimedia", "cgi_motion", "graphics_brand", "direction_pm"],
        "compress_domains": ["sre_cloud", "fullstack_engineering", "agentic_engineering"],
        "summary_tone": (
            "Production framing. Lead with campaigns taken from concept board to delivered cut: "
            "storyboards, shot design, 3D/CGI, animation, editing, sound, and the web the work "
            "lands on. Engineering shows up as capability, not as the story. Owner-operator voice: "
            "direct teams AND cut the work yourself. The most recognizable named work in the fact "
            "graph leads; every other named campaign keeps its own bullet rather than being merged "
            "into a summary line. Surface portfolio proof (audience/reach metrics) and the "
            "Notable Clients roster when the facts have them."
        ),
        "notes": (
            "Key points this lens MUST surface, in order: (1) profile = craft voice + 2-3 named "
            "flagships (a named video/campaign/product) drawn from the fact graph; (2) the most "
            "senior role leads with its highest-profile named campaign, then catalog-scale "
            "numbers, then each named campaign as its own bullet; (3) any flagship owned end to "
            "end is called out as such; (4) portfolio metrics when present; (5) the Notable "
            "Clients roster always included when present; (6) named collaborators are optional "
            "but named campaigns are never merged."
        ),
        "skills_order": [
            "video_multimedia",
            "cgi_motion",
            "ai_expertise",
            "graphics_brand",
            "direction_pm",
            "design_engineering",
        ],
    },
    {
        "name": "creative",
        "target_titles": [
            "Creative Director · CGI & Generative AI · Brand & Marketing",
            "Creative Director",
            "Art Director",
        ],
        "lead_domains": ["graphics_brand", "cgi_motion", "direction_pm"],
        "compress_domains": [
            "sre_cloud",
            "fullstack_engineering",
            "agentic_engineering",
            "design_engineering",
        ],
        "summary_tone": (
            "Brand-led. Lead with creative direction, visual identity, and campaign results; "
            "CGI and generative AI are the craft underneath."
        ),
        "skills_order": [
            "cgi_motion",
            "graphics_brand",
            "ai_expertise",
            "direction_pm",
            "video_multimedia",
            "design_engineering",
        ],
        "notes": "Agency and in-house creative-director roles.",
    },
    {
        "name": "sre",
        "target_titles": ["Site Reliability Engineer", "Platform Engineer", "Infrastructure Engineer"],
        "lead_domains": ["sre_cloud", "fullstack_engineering", "agentic_engineering"],
        "compress_domains": ["cgi_motion", "graphics_brand", "video_multimedia"],
        "summary_tone": (
            "Reliability framing. Lead with production environments, deployment, async pipelines, "
            "race conditions resolved, and systems that stayed up under real users."
        ),
        "skills_order": [
            "sre_cloud",
            "fullstack_engineering",
            "agentic_engineering",
            "ai_expertise",
            "design_engineering",
            "direction_pm",
        ],
        "notes": "Reliability / platform roles.",
    },
]


def seed(conn: sqlite3.Connection, overwrite: bool = False) -> list[str]:
    """Install the six shipped lenses. Idempotent; keeps user edits unless overwrite."""
    created: list[str] = []
    for spec in SEED_LENSES:
        existing = get(conn, spec["name"])
        if existing and not overwrite:
            continue
        upsert(conn, spec)
        created.append(spec["name"])
    conn.commit()
    return created


def upsert(conn: sqlite3.Connection, spec: dict[str, Any]) -> int:
    row = conn.execute("SELECT id FROM lenses WHERE name = ?", (spec["name"],)).fetchone()
    values = (
        db.dumps(spec.get("target_titles", [])),
        db.dumps(spec.get("lead_domains", [])),
        db.dumps(spec.get("compress_domains", [])),
        spec.get("summary_tone", ""),
        db.dumps(spec.get("skills_order", [])),
        spec.get("notes", ""),
    )
    if row is None:
        cur = conn.execute(
            """INSERT INTO lenses
               (target_titles, lead_domains, compress_domains, summary_tone, skills_order, notes, name)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            values + (spec["name"],),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    conn.execute(
        """UPDATE lenses SET target_titles = ?, lead_domains = ?, compress_domains = ?,
               summary_tone = ?, skills_order = ?, notes = ? WHERE name = ?""",
        values + (spec["name"],),
    )
    conn.commit()
    return int(row["id"])


def get(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM lenses WHERE name = ?", (name,)).fetchone()
    return _row_to_lens(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM lenses ORDER BY name").fetchall()
    return [_row_to_lens(r) for r in rows]


def create(
    conn: sqlite3.Connection,
    name: str,
    lead_domains: Sequence[str],
    target_titles: Sequence[str],
    compress_domains: Sequence[str] | None = None,
    summary_tone: str = "",
    skills_order: Sequence[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    lead = [d for d in lead_domains if d]
    unknown = [d for d in lead if d not in DOMAIN_NAMES]
    if unknown:
        raise ValueError(f"unknown domain(s): {', '.join(unknown)}. Known: {', '.join(DOMAIN_NAMES)}")
    compress = list(compress_domains or [d for d in DOMAIN_NAMES if d not in lead][-3:])
    order = list(skills_order or lead + [d for d in DOMAIN_NAMES if d not in lead])
    upsert(
        conn,
        {
            "name": name,
            "target_titles": list(target_titles),
            "lead_domains": lead,
            "compress_domains": compress,
            "summary_tone": summary_tone,
            "skills_order": order,
            "notes": notes,
        },
    )
    created = get(conn, name)
    assert created is not None
    return created


def _row_to_lens(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for field in ("target_titles", "lead_domains", "compress_domains", "skills_order"):
        d[field] = db.loads(d.get(field))
    return d
