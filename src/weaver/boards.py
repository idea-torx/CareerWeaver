"""The board registry — Lane 1's whole-web sweep (`weaver find --wide`).

Three layers, merged in this order and deduped on (kind, slug):
  1. the committed starter registry shipped next to this module (boards.json) —
     public ATS slugs for product companies and creative agencies; generic, no
     user data, safe to commit;
  2. `<data-dir>/boards.json` — the user's own additions (`weaver boards add`),
     local-only like the rest of the data dir;
  3. `find.orgs` / `find.boards` in config.json — whatever the user already
     configured for the narrow `weaver find`.

The sweep itself is sequential and polite: one board at a time, a small delay
between requests, and a hard cap (default 60 boards) so a growing registry can
never turn into a burst. Every request goes through `jobs.fetch_board_jobs`,
the single stdlib-urllib touchpoint — no new dependencies.

Slugs drift (a company moves boards, an agency closes one). A board that no
longer resolves is reported as a per-board error and the sweep carries on; the
fix is `weaver boards add <org> <board>`, not a code change.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import jobs as jobs_mod

#: Kinds the registry can record. Only FETCHABLE ones have a public feed
#: parser today; the rest are carried so the registry can be written down now
#: and swept later.
BOARD_KINDS = ("greenhouse", "lever", "ashby", "workable", "bamboohr", "other")
FETCHABLE = ("greenhouse", "lever", "ashby", "workable")

REGISTRY_NAME = "boards.json"
#: The committed starter registry (packaged with the module).
BUILTIN_REGISTRY = Path(__file__).with_name(REGISTRY_NAME)

#: Politeness: at most this many boards per sweep, this many seconds between.
DEFAULT_MAX_BOARDS = 60
DEFAULT_DELAY = 0.5


class BoardError(ValueError):
    """A malformed registry entry — a usage error, not a network error."""


# -------------------------------------------------------------------- entries


def normalize_entry(raw: Any) -> dict[str, Any]:
    """One registry entry -> {name, kind, slug, url, tags}.

    Accepts a dict ({"name": "Webflow", "greenhouse": "webflow"} or the
    explicit {"kind": ..., "slug": ...} form) or the shorthand string
    "slug:kind" / "slug".
    """
    if isinstance(raw, str):
        slug, sep, kind = raw.partition(":")
        raw = {"slug": slug.strip(), "kind": (kind.strip() if sep else "greenhouse")}
    if not isinstance(raw, dict):
        raise BoardError(f"bad board entry {raw!r} — expected an object or 'slug:kind'")

    kind = str(raw.get("kind") or "").strip().lower()
    slug = str(raw.get("slug") or raw.get("board_slug") or "").strip()
    if not kind:
        # the {"name": ..., "<kind>": "<slug>"} shorthand
        for candidate in BOARD_KINDS:
            if raw.get(candidate):
                kind, slug = candidate, str(raw[candidate]).strip()
                break
    url = str(raw.get("url") or "").strip()
    name = str(raw.get("name") or "").strip() or slug
    if not kind:
        raise BoardError(f"board {name or raw!r} has no kind (expected one of: {', '.join(BOARD_KINDS)})")
    if kind not in BOARD_KINDS:
        raise BoardError(f"unknown board kind {kind!r} for {name or slug!r} (expected one of: {', '.join(BOARD_KINDS)})")
    if not slug and not url:
        raise BoardError(f"board {name or raw!r} has neither a slug nor a url")
    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    return {"name": name, "kind": kind, "slug": slug.lower(), "url": url, "tags": tags}


def entry_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (entry["kind"], entry["slug"] or entry["url"].lower())


def merge(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Concatenate entry groups, first occurrence of a (kind, slug) wins."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for group in groups:
        for entry in group:
            key = entry_key(entry)
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    return out


def _normalize_all(raw_entries: Any, source: str, problems: list[str] | None) -> list[dict[str, Any]]:
    """Normalize a list of raw entries; bad ones are reported, never silent."""
    out: list[dict[str, Any]] = []
    if isinstance(raw_entries, dict):  # {"webflow": "greenhouse"} mapping form
        raw_entries = [{"slug": slug, "kind": kind} for slug, kind in raw_entries.items()]
    for raw in raw_entries or []:
        try:
            out.append(normalize_entry(raw))
        except BoardError as exc:
            if problems is not None:
                problems.append(f"{source}: {exc}")
    return out


def read_registry_file(path: Path, problems: list[str] | None = None) -> list[dict[str, Any]]:
    """Entries from a boards.json file. A missing/broken file reads as empty."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        if problems is not None:
            problems.append(f"{path}: {exc}")
        return []
    raw = payload.get("boards") if isinstance(payload, dict) else payload
    return _normalize_all(raw, str(path), problems)


def registry_path(data_dir: Path | str) -> Path:
    """The user's own registry — lives in the (gitignored) data dir."""
    return Path(data_dir) / REGISTRY_NAME


def load_builtin(problems: list[str] | None = None) -> list[dict[str, Any]]:
    return read_registry_file(BUILTIN_REGISTRY, problems)


def load_registry(
    data_dir: Path | str | None = None,
    config: dict[str, Any] | None = None,
    problems: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Committed starter registry + the user's boards.json + config find.*."""
    user: list[dict[str, Any]] = []
    if data_dir is not None:
        user = read_registry_file(registry_path(data_dir), problems)
    find = (config or {}).get("find") or {}
    configured = _normalize_all(find.get("boards") or [], "config.json find.boards", problems)
    configured += _normalize_all(find.get("orgs") or {}, "config.json find.orgs", problems)
    return merge(user, configured, load_builtin(problems))


def add(data_dir: Path | str, name: str, kind: str = "greenhouse", slug: str = "") -> dict[str, Any]:
    """`weaver boards add <org> [board]` — append to the user registry.

    Idempotent: adding a board that is already known returns the existing entry
    and rewrites nothing.
    """
    entry = normalize_entry({"name": name, "kind": kind, "slug": slug or name})
    path = registry_path(data_dir)
    existing = read_registry_file(path)
    for known in existing:
        if entry_key(known) == entry_key(entry):
            return known
    existing.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "boards": existing}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return entry


# ---------------------------------------------------------------------- sweep


def sweep(
    entries: Sequence[dict[str, Any]],
    targets: dict[str, Any],
    threshold: float = 0.6,
    limit: int = 10,
    max_boards: int = DEFAULT_MAX_BOARDS,
    delay: float = DEFAULT_DELAY,
    timeout: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    fetch: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Sweep the registry sequentially, score every posting, rank -> shortlist.

    Same result shape as `jobs.find_jobs` (jobs / scanned / errors) plus what
    the sweep itself did: how many boards it hit, which it skipped for the cap,
    and which kinds it has no feed parser for. Nothing is dropped quietly.
    """
    fetcher = fetch or jobs_mod.fetch_board_jobs
    usable: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    for entry in entries:
        (usable if entry["kind"] in FETCHABLE and entry["slug"] else unsupported).append(entry)

    cap = max(0, int(max_boards))
    swept, over_cap = usable[:cap], usable[cap:]

    postings: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for index, entry in enumerate(swept):
        if index and delay > 0:
            sleep(delay)  # politeness: sequential, spaced, never bursty
        try:
            postings.extend(fetcher(entry["slug"], entry["kind"], timeout))
        except (RuntimeError, jobs_mod.FindError) as exc:
            errors.append({"org": entry["slug"], "board": entry["kind"], "error": str(exc)})

    scored: list[dict[str, Any]] = []
    for posting in postings:
        fit, reasons = jobs_mod.score_fit(posting, targets)
        row = {key: posting[key] for key in ("role", "org", "board", "url", "location")}
        row["fit"] = fit
        row["reasons"] = reasons
        scored.append(row)
    scored.sort(key=lambda job: (-job["fit"], job["role"].lower(), job["url"]))
    kept = [job for job in scored if job["fit"] >= threshold][: max(0, limit)]
    return {
        "jobs": kept,
        "scanned": len(scored),
        "errors": errors,
        "swept": [{"name": e["name"], "kind": e["kind"], "slug": e["slug"]} for e in swept],
        "boards_swept": len(swept),
        "boards_registered": len(entries),
        "boards_over_cap": [f"{e['slug']}:{e['kind']}" for e in over_cap],
        "boards_unsupported": [f"{e['name']}:{e['kind']}" for e in unsupported],
    }
