"""`weaver serve` — a local, read-only viewer for the graph and the ledger.

Stdlib only, binds to 127.0.0.1 by default, and never writes: it is a window
onto weaver.db, not an app. `/` renders a page, `/api` returns the same data as
JSON for agents.
"""

from __future__ import annotations

import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import config as cfg, db, lenses as lenses_mod


def snapshot(data_dir: Path) -> dict[str, Any]:
    conn: sqlite3.Connection = db.init_db(data_dir)
    try:
        config = cfg.load_config(data_dir)
        return {
            "data_dir": str(data_dir),
            "profile": config.get("profile") or {},
            "stats": db.stats(conn),
            "lenses": lenses_mod.list_all(conn),
            "resumes": db.get_resumes(conn, limit=25),
            "applications": db.get_applications(conn, limit=25),
            "facts": [
                {
                    "id": f["id"],
                    "kind": f["kind"],
                    "title": f["title"],
                    "org": f["org"],
                    "start": f["start"],
                    "end": f["end"],
                    "verified": f["verified"],
                    "source": f["source"],
                    "bullets": len(f.get("bullets") or []),
                }
                for f in db.get_facts(conn)
            ],
        }
    finally:
        conn.close()


PAGE_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
       margin: 0; padding: 32px; max-width: 1040px; }
h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
     margin: 32px 0 10px; opacity: 0.6; }
.sub { opacity: 0.6; margin: 0 0 24px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid rgba(128,128,128,0.25);
         vertical-align: top; }
th { font-weight: 600; opacity: 0.6; font-size: 11px; text-transform: uppercase;
     letter-spacing: 0.06em; }
.tiles { display: flex; flex-wrap: wrap; gap: 10px; }
.tile { border: 1px solid rgba(128,128,128,0.3); border-radius: 8px; padding: 10px 14px; min-width: 96px; }
.tile b { display: block; font-size: 20px; font-weight: 600; }
.tile span { opacity: 0.6; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; opacity: 0.75; }
.empty { opacity: 0.5; font-style: italic; }
"""


def _tile(value: Any, caption: str) -> str:
    return f'<div class="tile"><b>{html.escape(str(value))}</b><span>{html.escape(caption)}</span></div>'


def _rows(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{html.escape(empty)}</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_page(data: dict[str, Any]) -> str:
    stats = data["stats"]
    profile = data.get("profile") or {}
    tiles = "".join(
        [
            _tile(stats["facts_total"], "facts"),
            _tile(f"{stats['verified_pct']}%", "verified"),
            _tile(stats["skills"], "skills"),
            _tile(stats["lenses"], "lenses"),
            _tile(stats["jobs"], "jobs"),
            _tile(stats["resumes"], "resumes"),
            _tile(stats.get("applications", 0), "applications"),
        ]
    )
    kinds = _rows(
        ["kind", "count"],
        [[k, v] for k, v in sorted(stats["facts_by_kind"].items())],
        "no facts yet — run weaver seed-import",
    )
    lens_rows = _rows(
        ["lens", "lead domains", "target title"],
        [
            [l["name"], ", ".join(l["lead_domains"]), (l["target_titles"] or [""])[0]]
            for l in data["lenses"]
        ],
        "no lenses",
    )
    resume_rows = _rows(
        ["#", "lens", "format", "provider", "created", "path"],
        [
            [r["id"], r["lens"] or "-", r["format"], r["provider"] or "-", r["created_at"], r["path"]]
            for r in data["resumes"]
        ],
        "no resumes generated yet — run weaver tailor",
    )
    app_rows = _rows(
        ["#", "status", "resume", "lens", "job", "created"],
        [
            [
                a["id"],
                a["status"],
                f"#{a['resume_id']}",
                a.get("lens") or "-",
                a.get("title") or a.get("company") or "(none)",
                a["created_at"],
            ]
            for a in data["applications"]
        ],
        "no applications yet — run weaver apply <id> --dry-run",
    )
    name = html.escape(profile.get("name") or "CareerWeaver")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CareerWeaver — {name}</title><style>{PAGE_CSS}</style></head>
<body>
<h1>CareerWeaver</h1>
<p class="sub">{name} · <code>{html.escape(data['data_dir'])}</code> · read-only ·
<a href="/api">/api</a></p>
<div class="tiles">{tiles}</div>
<h2>Fact graph</h2>{kinds}
<h2>Lenses</h2>{lens_rows}
<h2>Resumes</h2>{resume_rows}
<h2>Applications</h2>{app_rows}
</body></html>
"""


class WeaverHandler(BaseHTTPRequestHandler):
    server_version = "CareerWeaver"
    data_dir: Path = Path("data")
    quiet: bool = True

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if not self.quiet:
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            data = snapshot(self.data_dir)
        except Exception as exc:  # a broken db should not kill the server
            self._send(
                500,
                json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        if path == "/":
            self._send(200, render_page(data).encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api":
            payload = {"ok": True, **data}
            self._send(
                200,
                json.dumps(payload, indent=2, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        section = path[len("/api/") :] if path.startswith("/api/") else ""
        if section in ("facts", "lenses", "resumes", "applications", "stats", "profile"):
            self._send(
                200,
                json.dumps({"ok": True, section: data[section]}, indent=2, default=str).encode(
                    "utf-8"
                ),
                "application/json; charset=utf-8",
            )
            return
        self._send(
            404,
            json.dumps({"ok": False, "error": f"not found: {path}"}).encode("utf-8"),
            "application/json; charset=utf-8",
        )


def make_server(
    data_dir: Path, host: str = "127.0.0.1", port: int = 8787, quiet: bool = True
) -> ThreadingHTTPServer:
    handler = type(
        "BoundWeaverHandler",
        (WeaverHandler,),
        {"data_dir": Path(data_dir), "quiet": quiet},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
