"""The application ledger — what weaver already applied to, and what waits on you.

Three jobs, all read-only over `applications` except `hold_status`:
  * `rows()`     — the ledger table (`weaver ledger`).
  * `applied_urls()` / `dedupe()` — Lane 1 never re-surfaces a posting already
    in the ledger.
  * `hold_status()` — a `--hold` run parks at audit_pending with an audit of
    kind "hold"; the ledger calls that `held` so the user knows it is filled and
    waiting for their send, not stuck on a wall.

Nothing here invents applicant facts: every cell is a stored column, a note the
engine itself wrote, or the org slug the job URL already contains.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from . import db

#: Vocabulary the ledger renders in. The engine writes "applied"; a human reads
#: "submitted" — same row, one word.
STATUSES = ("submitted", "held", "audit_pending", "stopped", "failed", "dry_run")

_STATUS_DISPLAY = {"applied": "submitted"}

#: Path shapes that carry the org slug: greenhouse/lever/ashby all put it first.
_BOARD_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com")


def display_status(status: str | None) -> str:
    raw = (status or "").strip()
    return _STATUS_DISPLAY.get(raw, raw or "unknown")


def hold_status(response: dict[str, Any] | None, status: str) -> str:
    """`audit_pending` + an audit raised by --hold is recorded as `held`.

    The engine has one park state; the ledger distinguishes *why* it parked so
    "ready for your send" never reads like "hit a captcha".
    """
    if status != "audit_pending" or not isinstance(response, dict):
        return status
    audit = response.get("audit")
    if isinstance(audit, dict) and str(audit.get("kind") or "") == "hold":
        return "held"
    return status


# --------------------------------------------------------------------- job urls


def normalize_url(url: str | None) -> str:
    """Dedupe key for a posting URL: scheme/host case, no query, no trailing /.

    Query strings on ATS links are tracking (`?gh_src=`, `?utm_...`) — the same
    posting reached two ways is one application.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    if not parts.netloc:
        return raw.rstrip("/").lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return f"{host}{path}".lower()


def org_from_url(url: str | None) -> str:
    """The board slug embedded in an ATS URL (`.../webflow/jobs/1001` -> webflow)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = parts.netloc.lower()
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return ""
    if any(host.endswith(known) for known in _BOARD_HOSTS):
        head = segments[0]
        # boards.greenhouse.io/embed/job_app?... and /embed/<org> variants
        if head in ("embed", "jobs", "job") and len(segments) > 1:
            head = segments[1]
        return head
    return ""


# ------------------------------------------------------------------------ rows


def _applications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every ledger row, newest first. A db with no `applications` table is empty."""
    try:
        return db.get_applications(conn)
    except sqlite3.Error:
        return []


def _note(entry: dict[str, Any]) -> str:
    """The engine's own last word: reason, else error, else the last trace note."""
    response = entry.get("response")
    if not isinstance(response, dict):
        return ""
    for key in ("reason", "error"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    audit = response.get("audit")
    if isinstance(audit, dict) and str(audit.get("note") or "").strip():
        return str(audit["note"]).strip()
    trace = response.get("trace")
    if isinstance(trace, list):
        for step in reversed(trace):
            if isinstance(step, dict) and str(step.get("note") or "").strip():
                return str(step["note"]).strip()
    confirmation = response.get("confirmation_text")
    if isinstance(confirmation, str) and confirmation.strip():
        return confirmation.strip()
    return ""


def rows(conn: sqlite3.Connection, limit: int | None = None) -> list[dict[str, Any]]:
    """The ledger: id · date · org · role · status · note, newest first."""
    out: list[dict[str, Any]] = []
    for entry in _applications(conn):
        url = entry.get("url") or ""
        out.append(
            {
                "id": entry["id"],
                "date": (entry.get("created_at") or "")[:10],
                "created_at": entry.get("created_at"),
                "org": entry.get("company") or org_from_url(url) or "",
                "role": entry.get("title") or "",
                "status": display_status(entry.get("status")),
                "raw_status": entry.get("status"),
                "note": _note(entry),
                "job_url": url,
                "resume_id": entry.get("resume_id"),
                "job_id": entry.get("job_id"),
            }
        )
    if limit is not None:
        out = out[: max(0, limit)]
    return out


def counts(entries: Sequence[dict[str, Any]]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for entry in entries:
        tally[entry["status"]] = tally.get(entry["status"], 0) + 1
    return dict(sorted(tally.items()))


def format_table(entries: Sequence[dict[str, Any]]) -> list[str]:
    """Fixed-width human table. Empty ledger says so instead of printing a header."""
    if not entries:
        return ["no applications yet — run `weaver apply <resume_id>` to start the ledger"]
    lines = [
        f"{'id':>4}  {'date':<10}  {'org':<18}  {'role':<34}  {'status':<13}  note",
        f"{'-' * 4}  {'-' * 10}  {'-' * 18}  {'-' * 34}  {'-' * 13}  {'-' * 4}",
    ]
    for entry in entries:
        lines.append(
            f"{entry['id']:>4}  {entry['date']:<10}  {(entry['org'] or '-')[:18]:<18}  "
            f"{(entry['role'] or '-')[:34]:<34}  {entry['status']:<13}  "
            f"{(entry['note'] or '')[:60]}"
        )
    tally = counts(entries)
    lines.append(
        f"{len(entries)} application(s)  " + ", ".join(f"{k}={v}" for k, v in tally.items())
    )
    held = tally.get("held", 0)
    if held:
        lines.append(f"  {held} held — filled and waiting for your send")
    return lines


# ---------------------------------------------------------------------- dedupe


def applied_urls(conn: sqlite3.Connection) -> set[str]:
    """Normalized URLs of every posting already in the ledger."""
    return {
        key
        for entry in _applications(conn)
        if (key := normalize_url(entry.get("url")))
    }


def dedupe(
    postings: Iterable[dict[str, Any]], applied: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a shortlist into (fresh, already-applied). Order is preserved."""
    fresh: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for posting in postings:
        key = normalize_url(posting.get("url"))
        (excluded if key and key in applied else fresh).append(posting)
    return fresh, excluded
