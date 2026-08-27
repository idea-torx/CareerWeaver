"""`weaver` — agent-first CLI. Human text on a tty, clean JSON when piped."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import __version__, boards as boards_mod, completion, config as cfg, db, extract, guardrail
from . import jobs as jobs_mod
from . import ledger as ledger_mod
from . import lenses as lenses_mod
from . import llm, local_agent, preflight, render, tailor as tailor_mod
from . import payload as payload_lib
from .domains import DOMAIN_NAMES, score_domains

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
#: Terminal codes of a real `apply` run (see weaver.completion): a run that
#: reached its end is 0, one that died is 2, one waiting on a human is 3.
EXIT_FAILED = completion.EXIT_FAILED
EXIT_AUDIT_PENDING = completion.EXIT_AUDIT_PENDING


# ------------------------------------------------------------------------ output


def wants_json(args: argparse.Namespace) -> bool:
    # force_human wins over the piped-stdout heuristic: batch's inner runs set
    # it so a redirected cycle emits ONE JSON document, not N per-run blobs.
    if getattr(args, "force_human", False):
        return False
    if getattr(args, "json", False):
        return True
    return not sys.stdout.isatty()


def emit(args: argparse.Namespace, data: Any, human: Sequence[str]) -> int:
    if wants_json(args):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        for line in human:
            print(line)
    return EXIT_OK


def fail(args: argparse.Namespace, message: str, code: int = EXIT_ERROR, **extra: Any) -> int:
    payload = {"ok": False, "error": message, **extra}
    if wants_json(args):
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def open_db(args: argparse.Namespace) -> tuple[Path, sqlite3.Connection, dict[str, Any]]:
    data_dir = cfg.resolve_data_dir(getattr(args, "data_dir", None))
    conn = db.init_db(data_dir)
    lenses_mod.seed(conn)
    return data_dir, conn, cfg.load_config(data_dir)


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------- commands


def profile_env(key: str) -> str | None:
    """`WEAVER_PROFILE_EMAIL` etc. — set any profile field without a flag."""
    value = os.environ.get(f"WEAVER_PROFILE_{key.upper()}")
    return value.strip() if value and value.strip() else None


def collect_profile(args: argparse.Namespace, existing: dict[str, Any]) -> dict[str, Any]:
    """Blank template < existing config < environment < CLI flags < prompts.

    Nothing is invented and nothing is overwritten with a blank: an untouched
    field keeps whatever the config already had (empty, on a fresh init).
    """
    profile = dict(existing)
    for key, _label in cfg.PROFILE_PROMPTS:
        from_env = profile_env(key)
        if from_env is not None:
            profile[key] = from_env
        from_arg = getattr(args, key, None)
        if from_arg:
            profile[key] = from_arg

    if getattr(args, "interactive", False):
        # Prompts go to stderr so `weaver init --interactive --json | jq` works.
        print(
            "weaver init — press Enter to leave a field blank (edit config.json later).",
            file=sys.stderr,
        )
        for key, label in cfg.PROFILE_PROMPTS:
            current = profile.get(key)
            shown = ", ".join(current) if isinstance(current, list) else (current or "")
            suffix = f" [{shown}]" if shown else ""
            print(f"  {label}{suffix}: ", end="", file=sys.stderr, flush=True)
            try:
                answer = input().strip()
            except EOFError:
                break
            if answer:
                profile[key] = answer
    return cfg.normalize_profile(profile)


def cmd_init(args: argparse.Namespace) -> int:
    data_dir = cfg.resolve_data_dir(getattr(args, "data_dir", None))
    existed = cfg.config_path(data_dir).exists()
    conn = db.init_db(data_dir)
    seeded = lenses_mod.seed(conn, overwrite=bool(getattr(args, "reset_lenses", False)))
    config = cfg.load_config(data_dir)
    config["profile"] = collect_profile(args, config.get("profile") or {})
    cfg.save_config(data_dir, config)
    cfg.out_dir(data_dir)
    filled = [k for k, v in config["profile"].items() if v]
    data = {
        "ok": True,
        "data_dir": str(data_dir),
        "db": str(cfg.db_path(data_dir)),
        "config": str(cfg.config_path(data_dir)),
        "out_dir": str(data_dir / "out"),
        "already_existed": existed,
        "lenses_seeded": seeded,
        "lenses": [l["name"] for l in lenses_mod.list_all(conn)],
        "domains": DOMAIN_NAMES,
        "profile": config["profile"],
        "profile_fields_set": filled,
        "llm": llm.describe(),
    }
    human = [
        f"initialized {data_dir}",
        f"  db      {cfg.db_path(data_dir)}",
        f"  config  {cfg.config_path(data_dir)}",
        f"  lenses  {', '.join(data['lenses'])}",
        f"  llm     {data['llm']['provider']}",
        f"  profile {len(filled)}/{len(config['profile'])} field(s) set"
        + ("" if filled else " — run `weaver init --interactive` or edit config.json"),
    ]
    if data["llm"]["config_error"]:
        human.append(f"  ! llm   {data['llm']['config_error']}")
    return emit(args, data, human)


def cmd_seed_import(args: argparse.Namespace) -> int:
    data_dir, conn, config = open_db(args)
    directory = Path(args.dir).expanduser()
    if not directory.exists() or not directory.is_dir():
        return fail(args, f"seed directory not found: {directory}", EXIT_USAGE)

    result = extract.import_directory(conn, directory, use_llm=not args.no_llm)
    profile = extract.profile_from_imports(result["imported"])
    merged = dict(config.get("profile") or {})
    for key, value in profile.items():
        if value and (args.overwrite_profile or not merged.get(key)):
            merged[key] = value
    config["profile"] = merged
    cfg.save_config(data_dir, config)

    totals: dict[str, int] = {}
    for entry in result["imported"]:
        for key, value in entry.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    graph = db.stats(conn)
    data = {
        "ok": True,
        "dir": str(directory),
        "files": result["files"],
        "parsed": [
            {"file": e["source"], "provider": e["provider"], **{k: v for k, v in e.items() if isinstance(v, int)}}
            for e in result["imported"]
        ],
        "errors": result["errors"],
        "warnings": result.get("warnings", []),
        "skipped": result.get("skipped", []),
        "totals_parsed": totals,
        "graph": graph,
        "profile": merged,
    }
    human = [f"imported {len(result['files'])} file(s) from {directory}"]
    for entry in data["parsed"]:
        human.append(
            f"  {entry['file']:48s} roles={entry.get('role', 0):2d} "
            f"skills={entry.get('skill', 0):2d} awards={entry.get('award', 0):2d} "
            f"({entry['provider']})"
        )
    for entry in result.get("skipped", []):
        human.append(f"  - {entry['file']}: skipped ({entry['reason']})")
    for warning in result.get("warnings", []):
        human.append(f"  ~ {warning['file']}: {warning['warning']}")
    for err in result["errors"]:
        human.append(f"  ! {err['file']}: {err['error']}")
    human += [
        f"fact graph: {graph['facts_total']} facts "
        f"({', '.join(f'{k}={v}' for k, v in graph['facts_by_kind'].items())})",
        f"verified (corroborated by 2+ seeds): {graph['verified']} ({graph['verified_pct']}%)",
        f"skills: {graph['skills']}  lenses: {graph['lenses']}",
    ]
    return emit(args, data, human)


def cmd_lens_list(args: argparse.Namespace) -> int:
    _data_dir, conn, _config = open_db(args)
    all_lenses = lenses_mod.list_all(conn)
    data = {"ok": True, "count": len(all_lenses), "lenses": all_lenses}
    human = [f"{len(all_lenses)} lenses"]
    for lens in all_lenses:
        human.append(
            f"  {lens['name']:16s} {', '.join(lens['lead_domains'])}\n"
            f"  {'':16s} → {(lens['target_titles'] or [''])[0]}"
        )
    return emit(args, data, human)


def cmd_lens_show(args: argparse.Namespace) -> int:
    _data_dir, conn, _config = open_db(args)
    lens = lenses_mod.get(conn, args.name)
    if lens is None:
        return fail(args, f"no such lens: {args.name}", EXIT_USAGE)
    human = [
        f"lens {lens['name']}",
        f"  titles    {'; '.join(lens['target_titles'])}",
        f"  lead      {', '.join(lens['lead_domains'])}",
        f"  compress  {', '.join(lens['compress_domains'])}",
        f"  skills    {', '.join(lens['skills_order'])}",
        f"  tone      {lens['summary_tone']}",
        f"  notes     {lens['notes']}",
    ]
    return emit(args, {"ok": True, "lens": lens}, human)


def cmd_lens_create(args: argparse.Namespace) -> int:
    _data_dir, conn, _config = open_db(args)
    lead = [d.strip() for d in (args.lead_domains or "").split(",") if d.strip()]
    titles = [t.strip() for t in (args.titles or "").split(";") if t.strip()]
    if not lead:
        return fail(args, "--lead-domains is required (comma separated)", EXIT_USAGE)
    compress = [d.strip() for d in (args.compress_domains or "").split(",") if d.strip()]
    try:
        lens = lenses_mod.create(
            conn,
            name=args.name,
            lead_domains=lead,
            target_titles=titles or [args.name],
            compress_domains=compress or None,
            summary_tone=args.tone or "",
            notes=args.notes or "",
        )
    except ValueError as exc:
        return fail(args, str(exc), EXIT_USAGE)
    return emit(args, {"ok": True, "lens": lens}, [f"created lens {lens['name']}"])


def _auto_lens(conn: sqlite3.Connection, job: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the lens whose lead domains best match a posting."""
    text = " ".join(
        str(job.get(k) or "") for k in ("title", "company", "raw_text")
    ) + " " + " ".join(job.get("skills_required") or [])
    scores = score_domains(text)
    best, best_score = None, -1.0
    for lens in lenses_mod.list_all(conn):
        total = 0.0
        for index, domain in enumerate(lens["lead_domains"]):
            total += scores.get(domain, 0) * (4.0 - 0.5 * index)
        if total > best_score:
            best, best_score = lens, total
    return best


#: What each `--format` is allowed to leave on disk. A row that claims `pdf`
#: while holding markdown is a mislabelled upload waiting to happen.
FORMAT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "md": (".md", ".markdown", ".txt"),
    "docx": (".docx",),
    "pdf": (".pdf",),
}


def _artifact_problem(path: Path, fmt: str) -> str | None:
    """Why `path` is not a usable `fmt` artifact — None when it is one."""
    try:
        if not path.exists():
            return "does not exist"
        if not path.is_file():
            return "is not a file"
        if path.stat().st_size == 0:
            return "is empty"
    except OSError as exc:
        return f"could not be read ({exc})"
    allowed = FORMAT_SUFFIXES.get((fmt or "md").lower())
    if allowed and path.suffix.lower() not in allowed:
        return f"is not a {fmt} file (suffix {path.suffix or 'none'!r}; expected {' or '.join(allowed)})"
    return None


def _resume_file_problem(resume_url: str) -> str | None:
    """Why the resume `apply` is about to upload is unusable — None when it is.

    Hosted resumes are fetched at run time, so only local paths are checked.
    """
    if local_agent.is_hosted(resume_url):
        return None
    if not str(resume_url or "").strip():
        return "the resume row has no path on record"
    path = Path(resume_url).expanduser()
    try:
        if not path.exists():
            return f"the resume file {path} does not exist"
        if not path.is_file():
            return f"the resume path {path} is not a file"
        if path.stat().st_size == 0:
            return f"the resume file {path} is empty"
    except OSError as exc:
        return f"the resume file {path} could not be read ({exc})"
    return None


def cmd_tailor(args: argparse.Namespace) -> int:
    data_dir, conn, config = open_db(args)

    source_note = args.source or "fact-graph"
    imported: dict[str, Any] | None = None
    if args.source:
        path = Path(args.source).expanduser()
        if path.exists() and path.is_file():
            known = conn.execute(
                "SELECT COUNT(*) n FROM facts WHERE source LIKE ?", (f"%{path.name}%",)
            ).fetchone()["n"]
            if not known:
                imported = extract.import_document(conn, path, use_llm=not args.no_llm)
                extract.mark_corroborated(conn)
            source_note = path.name
        elif args.source not in ("graph", "facts", "all"):
            return fail(args, f"source not found: {args.source}", EXIT_USAGE)

    job = None
    if args.job:
        try:
            job = jobs_mod.resolve(conn, args.job)
        except RuntimeError as exc:
            return fail(args, str(exc))
        # `--job 12` with no job #12 used to resolve to None and the resume was
        # written unlinked (or, once applied, read as some other job's) — the
        # Aug-16 loom sent three resumes at the wrong postings that way. An id
        # the jobs table does not hold is a usage error, never a silent skip.
        if job is None:
            return fail(
                args,
                f"no job matches --job {args.job!r} — `weaver jobs list` shows the saved ids",
                EXIT_USAGE,
            )

    lens = None
    if args.lens:
        lens = lenses_mod.get(conn, args.lens)
        if lens is None:
            return fail(args, f"no such lens: {args.lens}", EXIT_USAGE)
    elif job:
        # A textless job gives the lens picker NOTHING — the morning-of-Aug-16
        # run silently dressed engineering applications in the creative lens
        # this way. Refusing loudly beats a wrong resume quietly.
        if len(str(job.get("raw_text") or "").strip()) < 200:
            return fail(
                args,
                f"job #{job.get('id')} has no posting text — the lens auto-pick would "
                "be a blind guess. Re-add it with `weaver jobs add <url> --fetch`, "
                "or pass --lens <name> explicitly.",
                EXIT_USAGE,
            )
        lens = _auto_lens(conn, job)
    if lens is None:
        return fail(args, "provide --lens <name> or --job <url|text>", EXIT_USAGE)

    if not db.get_facts(conn, ["role"]):
        return fail(args, "fact graph is empty — run `weaver seed-import --dir seed_resumes` first")

    profile = config.get("profile") or {}
    result = tailor_mod.tailor(
        conn,
        lens,
        profile,
        job=job,
        use_llm=not args.no_llm,
        max_roles=args.max_roles,
    )

    fmt = (args.format or "md").lower()
    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        out_path = cfg.out_dir(data_dir) / render.default_filename(
            profile.get("name") or "resume", lens["name"], fmt, stamp(), job=job
        )
    try:
        written = Path(render.render(result["structure"], out_path, fmt))
    except ValueError as exc:
        return fail(args, str(exc), EXIT_USAGE)

    # The resume row's `path` IS the file `apply` uploads. On Aug-16 three rows
    # still pointed at long-dead /tmp files while adapt rendered fresh
    # artifacts into data/out — three companies got the wrong resume. The row
    # now records THIS run's artifact, absolute, and only after it is proven to
    # be a real, non-empty file of the format that was asked for.
    written = written.expanduser().resolve()
    problem = _artifact_problem(written, fmt)
    if problem:
        return fail(
            args,
            f"refusing to record resume row: rendered artifact {written} {problem}",
            EXIT_ERROR,
        )

    resume_id = db.add_resume(
        conn,
        lens=lens["name"],
        job_id=(job or {}).get("id"),
        fmt=fmt,
        path=str(written),
        prompt_used=result["prompt_used"],
        source_facts=result["source_facts"],
        structure=result["structure"],
        provider=result["provider"],
        status=args.status,
    )
    # Read the row back: a path/linkage that was never asked about is exactly
    # how the stale rows survived. Verify, don't assume.
    stored = db.get_resume(conn, resume_id)
    expected_job_id = (job or {}).get("id")
    if stored is None or stored.get("path") != str(written) or stored.get("job_id") != expected_job_id:
        return fail(
            args,
            f"resume row #{resume_id} did not land as rendered "
            f"(path={(stored or {}).get('path')!r}, job_id={(stored or {}).get('job_id')!r}; "
            f"expected path={str(written)!r}, job_id={expected_job_id!r})",
            EXIT_ERROR,
        )

    data = {
        "ok": True,
        "resume_id": resume_id,
        "lens": lens["name"],
        "source": source_note,
        "job_id": (job or {}).get("id"),
        "format": fmt,
        "path": str(written),
        "provider": result["provider"],
        "fallback_reason": result["fallback_reason"],
        "structure": result["structure"],
        "source_facts": result["source_facts"],
        "unverified_mentions": result["unverified_mentions"],
        "avoided": result["avoided"],
        "imported_source": imported["source"] if imported else None,
    }
    structure = result["structure"]
    human = [
        f"resume #{resume_id} → {written}",
        f"  lens      {lens['name']} ({', '.join(lens['lead_domains'])})",
        f"  title     {structure.get('title')}",
        f"  provider  {result['provider']}"
        + (f" ({result['fallback_reason']})" if result["fallback_reason"] else ""),
        f"  roles     {len(structure.get('experience') or [])}"
        f"  facts used {len(result['source_facts'])}",
        "  " + guardrail.summarize(result["unverified_mentions"]),
    ]
    if result["avoided"]:
        human.append(
            "  do-not-mention: removed every mention of "
            + ", ".join(result["avoided"])
        )
    human.append("")
    human += ["  " + line for line in render.preview(structure, limit=14)]
    return emit(args, data, human)


def cmd_jobs_add(args: argparse.Namespace) -> int:
    _data_dir, conn, _config = open_db(args)
    value = args.value
    if value == "-":
        value = sys.stdin.read()
    try:
        job = jobs_mod.add(conn, value, fetch_url=args.fetch)
    except RuntimeError as exc:
        return fail(args, str(exc))
    human = [
        f"job #{job['id']} {job.get('title') or '(untitled)'}"
        + (f" @ {job['company']}" if job.get("company") else ""),
        f"  url    {job.get('url') or '-'}",
        f"  text   {len(job.get('raw_text') or '')} chars",
        f"  skills {', '.join(job.get('skills_required') or []) or '-'}",
    ]
    warning = ""
    if job.get("url") and len(str(job.get("raw_text") or "").strip()) < 200:
        warning = (
            "no posting text stored — lens auto-pick, preflight and the fit "
            "scorer are blind for this job. Re-add with --fetch."
        )
        human.append(f"  ! {warning}")
    job_out = dict(job)
    job_out["raw_text_chars"] = len(job_out.pop("raw_text", "") or "")
    data = {"ok": True, "job": job_out}
    if warning:
        data["warning"] = warning
    return emit(args, data, human)


def cmd_jobs_list(args: argparse.Namespace) -> int:
    _data_dir, conn, _config = open_db(args)
    all_jobs = jobs_mod.list_all(conn)
    human = [f"{len(all_jobs)} job(s)"]
    for job in all_jobs:
        human.append(
            f"  #{job['id']:<3d} {(job.get('title') or '(untitled)')[:48]:48s} "
            f"{job.get('company') or '':20s} {job.get('url') or ''}"
        )
    return emit(args, {"ok": True, "count": len(all_jobs), "jobs": all_jobs}, human)


def cmd_preflight(args: argparse.Namespace) -> int:
    """Check a saved job's form questions against the applicant facts (no browser)."""
    _data_dir, conn, config = open_db(args)
    job = db.get_job(conn, args.job_id)
    if job is None:
        return fail(args, f"no such job #{args.job_id}", EXIT_USAGE)
    applicant = payload_lib.applicant_from_profile(
        config.get("profile") or {},
        (config.get("profile") or {}).get("links") or [],
    )
    # Parity with the apply/batch gates: those runs always attach a rendered
    # resume, so `Resume/CV` is never a real gap — without this shim the
    # standalone verdict read `fail (needs fact: resume)` on every Greenhouse
    # job while the cycle's own gate passed it.
    applicant.setdefault("resume", "(attached at apply time)")
    report = preflight.preflight(job.get("url") or "", applicant)
    data = {"ok": True, "job_id": args.job_id, **report}
    human = [f"preflight for job #{args.job_id}"]
    human.append(
        f"required: {report['required_total']} | optional: {report['optional_total']} | verdict: {report['verdict']}"
    )
    if report["missing"]:
        human.append(f"{len(report['missing'])} uncovered required question(s):")
        for m in report["missing"]:
            human.append(f"  - {m['question']}  (needs fact: {m['needs_fact']})")
    else:
        human.append("all required questions covered by facts — browser-safe to apply")
    return emit(args, data, human)


def cmd_tab_host(args: argparse.Namespace) -> int:
    """The shared headed browser `apply --tab` runs open their tabs in."""
    data_dir = cfg.resolve_data_dir(getattr(args, "data_dir", None))
    try:
        local_agent.local_driver.run_tab_host(data_dir, getattr(args, "port", None))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        return fail(args, str(exc))
    return EXIT_OK


def cmd_apply(args: argparse.Namespace) -> int:
    _data_dir, conn, config = open_db(args)
    resume = db.get_resume(conn, args.resume_id)
    if resume is None:
        return fail(args, f"no such resume: {args.resume_id}", EXIT_USAGE)
    job = db.get_job(conn, resume["job_id"]) if resume.get("job_id") else None
    profile = config.get("profile") or {}
    request_payload = payload_lib.build_payload(conn, resume, profile, job)
    resume_url = (
        getattr(args, "resume_url", None)
        or resume.get("path")
        or os.environ.get("WEAVER_RESUME_URL", "")
    )
    max_actions = getattr(args, "max_actions", None) or local_agent.DEFAULT_MAX_ACTIONS

    # A missing resume file used to cost nothing: the driver simply never
    # uploaded one and the run still reported a filled form (Aug-16 — two of
    # three applications went out with no resume attached at all). Refuse the
    # run instead; a resume-less application is worse than no application.
    problem = _resume_file_problem(resume_url)
    if problem:
        return fail(
            args,
            f"resume #{resume['id']}: {problem}. Re-render it with "
            f"`weaver adapt graph --job {resume.get('job_id') or '<job-id>'} "
            f"--format {resume.get('format') or 'pdf'}` and apply the new resume id, "
            "or pass --resume-url <public url>.",
            EXIT_USAGE,
        )

    if args.dry_run:
        data = {"ok": True, "dry_run": True, **local_agent.dry_run(request_payload, resume_url=resume_url, max_actions=max_actions)}
        request = data["request"]
        application_id = db.add_application(
            conn,
            resume_id=int(resume["id"]),
            job_id=(job or {}).get("id"),
            status="dry_run",
            payload=request,
            url=(job or {}).get("url"),
            company=(job or {}).get("company"),
            title=(job or {}).get("title"),
        )
        data["application_id"] = application_id
        human = [
            f"local dry-run for resume #{resume['id']} ({resume['path']})",
            f"  application     #{application_id} (status dry_run, logged to applications)",
            f"  provider        local ({data['engine']})",
            f"  job url         {request.get('job_url') or '(none — job not linked)'}",
            f"  resume file     {request.get('resume_url') or '(none)'} ({data['resume_source']})",
            f"  would open      {data['would_open']} (no network call, no browser)",
            f"  llm key         {data['llm_key_present']}",
            "",
            json.dumps(data["request"], indent=2, ensure_ascii=False)[:4000],
        ]
        return emit(args, data, human)

    if not llm.available():
        return fail(
            args,
            "no model configured — set WEAVER_LLM_CMD (a local CLI) or "
            "WEAVER_API_KEY (an HTTP endpoint). Re-run with --dry-run.",
            EXIT_USAGE,
        )

    # Preflight gate: never burn a browser session on a form we can't answer.
    # (Greenhouse/Lever/Ashby expose questions publicly; generic fetch is best effort.)
    if not args.force and job:
        applicant = payload_lib.applicant_from_profile(
            config.get("profile") or {},
            (config.get("profile") or {}).get("links") or [],
        )
        applicant["resume"] = resume_url  # the hosted resume answers Resume/CV questions
        report = preflight.preflight(job.get("url") or "", applicant)
        if report["verdict"] == "fail":
            missing = "; ".join(
                f"{m['question']} (needs fact: {m['needs_fact']})" for m in report["missing"]
            )
            return fail(
                args,
                f"preflight FAIL — {len(report['missing'])} required question(s) with no supporting fact: {missing}. "
                "Add the facts to the profile (data/config.json) and re-run, or --force to proceed anyway.",
                EXIT_USAGE,
            )
    cdp_url = None
    real_chrome_host = False
    if getattr(args, "tab", False):
        try:
            if _needs_real_chrome((job or {}).get("url") or ""):
                # Bot-walled ATS (workable, lever): the anti-bot check fails
                # even for a human inside the Playwright tab-host — it must run
                # in a real Chrome over CDP (see _ensure_real_chrome).
                real_chrome_host = True
                cdp_url = _ensure_real_chrome(_data_dir)
            else:
                cdp_url = local_agent.local_driver.ensure_tab_host(_data_dir)
        except RuntimeError as exc:
            return fail(args, str(exc))
    # Every real run leaves a durable trace: two killed windows lost two full
    # traces before this default existed. WEAVER_TRACE_FILE still overrides.
    traces_dir = _data_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    trace_file = os.environ.get("WEAVER_TRACE_FILE") or str(traces_dir / f"apply-{stamp()}.jsonl")

    # Where the filled form actually lives, for the completion block.
    if getattr(args, "tab", False):
        window = f"tab of the shared tab-host window ({cdp_url})"
    elif getattr(args, "visible", False):
        window = "the browser window this run opened"
    else:
        window = "headless — no window was opened"

    def end_run(status: str, lean: dict[str, Any], screenshot: str | None = None) -> int:
        """The ONE exit of a started run: ledger row, then the loud block.

        Clean hold, raised RuntimeError, Ctrl-C — all land here, and the row is
        written BEFORE anything is printed. A run that reached the browser can
        no longer fizzle out with nothing in the ledger.
        """
        audit = lean.get("audit") if isinstance(lean.get("audit"), dict) else None
        application_id = db.add_application(
            conn,
            resume_id=int(resume["id"]),
            job_id=(job or {}).get("id"),
            status=status,
            payload=request_payload,
            response=lean,
            url=(job or {}).get("url"),
            company=(job or {}).get("company"),
            title=(job or {}).get("title"),
        )
        reason = str(
            lean.get("reason")
            or lean.get("error")
            or (audit or {}).get("label")
            or (audit or {}).get("note")
            or ""
        )
        block = completion.block(
            status,
            role=(job or {}).get("title") or (resume.get("lens") or ""),
            org=(job or {}).get("company") or ledger_mod.org_from_url((job or {}).get("url")),
            reason=reason,
            where=(audit or {}).get("url") or (job or {}).get("url") or "",
            tab=window,
            ledger_id=application_id,
        )
        # The row carries the terminal state AND the words a human read.
        lean["completion_block"] = "\n".join(block)
        db.set_application_response(conn, application_id, lean)

        code = completion.exit_code(status)
        notified = False
        if completion.wants_notification(
            getattr(args, "notify", None), bool(getattr(args, "visible", False))
        ):
            notified = completion.notify(
                completion.notification(
                    status,
                    (job or {}).get("title") or (resume.get("lens") or ""),
                    (job or {}).get("company") or "",
                )
            )

        human = [
            f"apply #{application_id} via local chromium → {status}",
            f"  trace file {trace_file}",
            f"  trace     {len(lean.get('trace') or [])} actions",
            f"  reason    {reason[:160] or '—'}",
            f"  confirm   {(lean.get('confirmation_text') or '')[:120]}",
        ]
        if status in (local_agent.AUDIT_PENDING, "held") and audit:
            human += [
                "  HUMAN AUDIT — not submitted. Finish this one in the open window:"
                if status == local_agent.AUDIT_PENDING
                else "  HELD — filled, never submitted. Review and press send yourself:",
                f"    field   {audit.get('label') or audit.get('field') or '(anti-bot wall)'}",
                f"    value   {audit.get('value') or '(none declared)'}",
                f"    url     {audit.get('url') or '—'}",
            ]
        emit(
            args,
            {
                "ok": code == EXIT_OK,
                "dry_run": False,
                "application_id": application_id,
                "status": status,
                "exit_code": code,
                "completion_block": lean["completion_block"],
                "notified": notified,
                "response": lean,
                "final_screenshot_b64": screenshot,
            },
            human,
        )
        # stderr, always: the block must survive `| jq` and a JSON pipe alike.
        print("\n".join(block), file=sys.stderr)
        return code

    try:
        response = local_agent.apply(
            request_payload,
            resume_url=resume_url,
            max_actions=max_actions,
            headless=not getattr(args, "visible", False),
            hold=getattr(args, "hold", False),
            cover_path=getattr(args, "cover", None),
            cdp_url=cdp_url,
            trace_file=trace_file,
        )
    except KeyboardInterrupt:
        return end_run("failed", {"error": "interrupted (Ctrl-C) — the run never finished"})
    except BaseException as exc:  # noqa: BLE001 — nothing may escape without a row
        error = f"{type(exc).__name__}: {exc}"
        # A borrowed Chrome fails in ways that read as weaver bugs (live test #5:
        # "Browser context management is not supported"). Say where to look.
        hint = _real_chrome_attach_hint(_data_dir) if real_chrome_host else None
        return end_run("failed", {"error": f"{error} — {hint}" if hint else error})

    lean, screenshot = payload_lib.split_screenshots(response)
    # `--hold` parks at audit_pending; the ledger records that park as `held`.
    status = ledger_mod.hold_status(lean, payload_lib.ledger_status(response))
    return end_run(status, lean, screenshot)


#: An application row in one of these states means the resume is done with the
#: browser: a human is reviewing the held tab, or the form is already sent.
_BATCH_TERMINAL = frozenset({"held", "submitted", "applied"})

#: Every state a ledger row may legitimately hold. `apps set-status` validates
#: against this so a typo cannot invent a state the reports do not count.
LEDGER_STATUSES = frozenset(
    {"dry_run", "failed", "stopped", "audit_pending", "held", "submitted", "applied"}
)


def _take_batch_lock(data_dir: Path) -> tuple[Path, int | None]:
    """Single-flight lock for fill cycles: (lock_path, live_holder_pid|None).

    Overlapping cron ticks used to be able to race two cycles into the same
    tab-host (the per-entry dedupe reads happen before the fills land). O_EXCL
    creation is the arbiter; a lock whose pid is dead is stale and replaced.
    Holder None means WE hold the lock and must release it.
    """
    path = data_dir / "batch.lock"
    for _ in range(2):
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {stamp()}\n")
            return path, None
        except FileExistsError:
            pid = None
            with contextlib.suppress(Exception):
                pid = int(path.read_text(encoding="utf-8").split()[0])
            if pid:
                try:
                    os.kill(pid, 0)
                    return path, pid  # a live cycle owns it
                except OSError:
                    pass  # stale — its process is gone
            with contextlib.suppress(OSError):
                path.unlink()
    return path, None


def _release_batch_lock(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _ensure_tab_host(data_dir: Path) -> str:
    """Indirection so tests can stub the browser away from batch runs."""
    return local_agent.local_driver.ensure_tab_host(data_dir)


#: What to do when the shared window cannot start — the cron/harness answer.
_TAB_HOST_REMEDY = (
    "run the cycle inside the logged-in GUI session (a launchd USER agent or "
    "the user's own terminal, not a system daemon/ssh cron), or pre-start the "
    "window once with `weaver tab-host`"
)

# ------------------------------------------- real-chrome host (bot-walled ATSes)

#: Some ATSes run an anti-bot check that fails inside the Playwright tab-host
#: and clears from a real Chrome attached over CDP. These fills are routed to a
#: real Chrome instance — weaver still only fills and parks; the human answers
#: the verification and presses send, as always.
#:
#: - workable: Cloudflare Turnstile at submit, which fails even a HUMAN clicking
#:   the checkbox in the tab-host (2026-08-19 Bridgit run: infinite re-challenge
#:   loop; the same form sent fine from a real Chrome).
#: - lever: repeated CAPTCHA re-challenge on the apply page (2026-08-24 Metabase,
#:   run 145 — the run never attached a resume and was abandoned).
DEFAULT_REAL_CDP_PORT = 9223

_REAL_CHROME_HOSTS = ("apply.workable.com", "jobs.lever.co")

_REAL_CHROME_REMEDY = (
    "some forms (workable, lever) verify you are human and need a real Chrome: "
    "install Google Chrome, or pre-start one with --remote-debugging-port="
    f"{DEFAULT_REAL_CDP_PORT} and a dedicated --user-data-dir "
    "(WEAVER_REAL_CDP_PORT overrides the port)"
)

#: Attaching to a stranger on the port is the 2026-08-19 live-test #5 failure:
#: an unrelated Chrome held 9223 and the fill died at
#: "Browser.setDownloadBehavior: Browser context management is not supported".
#: A free port makes weaver launch its own dedicated-profile instance instead.
_REAL_CHROME_FOREIGN_REMEDY = (
    "if that browser is not weaver's, quit it or move weaver to a free port "
    "with WEAVER_REAL_CDP_PORT=<port> so it launches its own "
    "dedicated-profile Chrome"
)

#: Ownership evidence for the instance weaver launched: pid + port + the
#: endpoint's own Browser string, so a later attach can tell its own Chrome
#: from whatever else happens to be listening.
_REAL_CHROME_RECORD = "real-chrome.pid"


def _real_chrome_hosts() -> tuple[str, ...]:
    """The bot-walled hosts, overridable per AGENTS.md rule 7 — a fresh clone
    adds a provider via WEAVER_REAL_CHROME_HOSTS (comma-separated), not a patch.
    An explicit set REPLACES the defaults; a blank one is an unset one."""
    raw = os.environ.get("WEAVER_REAL_CHROME_HOSTS") or ""
    hosts = tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return hosts or _REAL_CHROME_HOSTS


def _needs_real_chrome(job_url: str) -> bool:
    url = (job_url or "").lower()
    return any(host in url for host in _real_chrome_hosts())


def _real_chrome_port() -> int:
    try:
        return int(os.environ.get("WEAVER_REAL_CDP_PORT") or DEFAULT_REAL_CDP_PORT)
    except ValueError:
        return DEFAULT_REAL_CDP_PORT


def _chrome_binary() -> str | None:
    """A real Chrome/Chromium binary, or None. Overridable via WEAVER_CHROME_BIN."""
    import shutil

    explicit = (os.environ.get("WEAVER_CHROME_BIN") or "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    mac_default = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac_default).exists():
        return mac_default
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _cdp_browser(port: int, timeout: float = 1.0) -> str | None:
    """The endpoint's own `Browser` string (e.g. "Chrome/139.0.7258.67"), or
    None when /json/version cannot be read — best effort identification only."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as fh:
            return str(json.loads(fh.read().decode("utf-8")).get("Browser") or "") or None
    except Exception:  # noqa: BLE001 — an unreadable endpoint is just unknown
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _record_real_chrome(data_dir: Path, port: int, pid: int) -> None:
    """Note that WE launched the Chrome now listening on `port`."""
    with contextlib.suppress(OSError):
        (Path(data_dir) / _REAL_CHROME_RECORD).write_text(
            json.dumps(
                {
                    "pid": pid,
                    "port": port,
                    "browser": _cdp_browser(port) or "",
                    "started": stamp(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def _foreign_real_chrome(data_dir: Path, port: int | None = None) -> str | None:
    """Why the Chrome listening on `port` does not look like weaver's own, or
    None when it does (nothing listening reads as None too — there is no
    stranger to warn about yet).

    Live test #5 (2026-08-19) attached to an unrelated Chrome and only found
    out inside the fill; this is the cheap check that names the problem first.
    """
    port = _real_chrome_port() if port is None else port
    if not local_agent.local_driver._port_open(port):
        return None
    record: Any = None
    with contextlib.suppress(Exception):
        record = json.loads((Path(data_dir) / _REAL_CHROME_RECORD).read_text(encoding="utf-8"))
    pid, was_port = 0, 0
    if isinstance(record, dict):
        with contextlib.suppress(TypeError, ValueError):  # a hand-edited record is no record
            pid, was_port = int(record.get("pid") or 0), int(record.get("port") or 0)
    if pid <= 0:
        return f"no usable {_REAL_CHROME_RECORD} record under {data_dir} — weaver never launched a Chrome here"
    if was_port != port:
        return f"weaver's own Chrome was launched on port {was_port}, not {port}"
    if not _pid_alive(pid):
        return f"weaver's Chrome (pid {pid}) is gone, so something else holds the port"
    browser, was = _cdp_browser(port), str(record.get("browser") or "")
    if browser and was and browser != was:
        return f"the endpoint now reports {browser}, not the {was} weaver launched"
    return None


def _warn_foreign_real_chrome(port: int, reason: str) -> None:
    """Loud, on stderr: the run is about to drive a browser weaver does not own."""
    browser = _cdp_browser(port)
    seen = f" (it reports {browser})" if browser else ""
    print(
        f"warning: attaching to a Chrome already listening on CDP port {port} "
        f"that weaver did not launch{seen} — {reason}. "
        f"{_REAL_CHROME_FOREIGN_REMEDY}",
        file=sys.stderr,
    )


def _real_chrome_attach_hint(data_dir: Path) -> str | None:
    """Remediation to append when a real-Chrome run dies after attaching, or
    None when the endpoint was weaver's own (then the failure is elsewhere)."""
    port = _real_chrome_port()
    reason = _foreign_real_chrome(data_dir, port)
    if reason is None:
        return None
    return (
        f"the run attached to a Chrome on CDP port {port} that weaver did not "
        f"launch ({reason}) — {_REAL_CHROME_FOREIGN_REMEDY}"
    )


def _ensure_real_chrome(data_dir: Path, wait_s: float = 25.0) -> str:
    """CDP url of a real Chrome — starting one if none is listening.

    Chrome 136+ refuses remote debugging on the default profile, so the
    instance runs on a dedicated profile dir under the data dir. Held tabs
    survive weaver's CDP disconnect exactly like the Playwright tab-host's.

    An already-open port is attached to as before, but only after checking it
    against the launch record — a stranger on the port gets a warning naming
    the port and the WEAVER_REAL_CDP_PORT override rather than a mystery
    failure deep inside the fill.
    """
    import subprocess
    import time as _time

    port = _real_chrome_port()
    url = f"http://127.0.0.1:{port}"
    if local_agent.local_driver._port_open(port):
        reason = _foreign_real_chrome(data_dir, port)
        if reason:
            _warn_foreign_real_chrome(port, reason)
        return url
    binary = _chrome_binary()
    if not binary:
        raise RuntimeError(f"no Chrome binary found — {_REAL_CHROME_REMEDY}")
    profile = Path(data_dir) / "real-chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    with open(Path(data_dir) / "real-chrome.log", "a") as log:
        child = subprocess.Popen(
            [
                binary,
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=log,
            stderr=log,
            start_new_session=True,  # the browser must outlive this run
        )
    deadline = _time.monotonic() + wait_s
    while _time.monotonic() < deadline:
        if local_agent.local_driver._port_open(port):
            _record_real_chrome(data_dir, port, child.pid)
            return url
        _time.sleep(0.25)
    raise RuntimeError(f"real Chrome did not open CDP port {port} within {wait_s:.0f}s — {_REAL_CHROME_REMEDY}")


def _applications_for(conn: sqlite3.Connection, resume_id: int) -> list[dict[str, Any]]:
    """This resume's application rows, newest first."""
    return [r for r in db.get_applications(conn) if r.get("resume_id") == resume_id]


def cmd_batch(args: argparse.Namespace) -> int:
    """The fill cycle (docs/batch-cycle-contract.md): preflight each resume,
    skip what can't be answered, fill the rest back-to-back as tabs of the
    shared window — each run marks complete ON FILL — then one report that
    relays the skips, the holds, and the fields a human must finish."""
    data_dir, conn, config = open_db(args)
    lock, holder = _take_batch_lock(data_dir)
    if holder is not None:
        return fail(
            args,
            f"another fill cycle is already running (pid {holder}) — one cycle at a "
            f"time; if that pid is not a live weaver run, delete {lock}",
            EXIT_USAGE,
        )
    try:
        code, data, human = _batch_run(args, conn, config, data_dir, list(args.resume_ids))
    finally:
        _release_batch_lock(lock)
    emit(args, data, human)
    # A JSON pipe still gets the human report on stderr; a tty already saw it.
    if wants_json(args):
        print("\n".join(human), file=sys.stderr)
    return code


def _batch_run(
    args: argparse.Namespace,
    conn: sqlite3.Connection,
    config: dict[str, Any],
    data_dir: Path,
    resume_ids: list[int],
) -> tuple[int, dict[str, Any], list[str]]:
    """The cycle body, emit-free so `weaver cycle` can wrap it in ONE report.
    Caller holds the batch lock. Returns (exit_code, json_data, human_lines)."""
    results: list[dict[str, Any]] = []
    #: Each browser host is ensured once, before its first fill — a dead host
    #: fails every entry that needs it with the same remediation instead of N
    #: different-looking errors (and skip-only cycles never touch a browser).
    #: Two hosts exist: the Playwright tab-host, and a real Chrome for
    #: bot-walled entries (workable, lever — see _ensure_real_chrome).
    host_errors: dict[str, str | None] = {}

    def record(resume_id: int, outcome: str, reason: str, **extra: Any) -> None:
        results.append({"resume_id": resume_id, "outcome": outcome, "reason": reason, **extra})

    for resume_id in resume_ids:
        resume = db.get_resume(conn, resume_id)
        if resume is None:
            record(resume_id, "skipped", "no such resume")
            continue
        job = db.get_job(conn, resume["job_id"]) if resume.get("job_id") else None
        if job is None or not (job.get("url") or "").strip():
            record(resume_id, "skipped", "no job (or job URL) linked — `weaver jobs add --fetch` first")
            continue
        title = job.get("title") or resume.get("lens") or ""
        company = job.get("company") or ledger_mod.org_from_url(job.get("url")) or ""
        rows = _applications_for(conn, resume_id)
        # ANY terminal row skips, not just the newest: a later dry_run row must
        # never unmask a form a human is reviewing or has already sent.
        prior = next((r for r in rows if r.get("status") in _BATCH_TERMINAL), None)
        if prior:
            record(
                resume_id, "skipped",
                f"already {prior['status']} (ledger #{prior['id']}) — never refill a reviewed form",
                title=title, company=company,
            )
            continue
        # The JOB is the dedupe unit too: a re-adapted resume (new id, same
        # posting) must never open a second application on a job a human is
        # already reviewing or has sent.
        job_prior = next(
            (
                r
                for r in db.get_applications(conn)
                if r.get("job_id") == resume.get("job_id")
                and r.get("status") in _BATCH_TERMINAL
            ),
            None,
        )
        if job_prior:
            record(
                resume_id, "skipped",
                f"job already {job_prior['status']} via resume "
                f"#{job_prior['resume_id']} (ledger #{job_prior['id']}) — one application per posting",
                title=title, company=company,
            )
            continue

        # Review the required forms; skip the unknown ones, relay at cycle end.
        if not getattr(args, "force", False):
            applicant = payload_lib.applicant_from_profile(
                config.get("profile") or {},
                (config.get("profile") or {}).get("links") or [],
            )
            applicant["resume"] = resume.get("path") or os.environ.get("WEAVER_RESUME_URL", "")
            report = preflight.preflight(job.get("url") or "", applicant)
            if report["verdict"] == "fail":
                missing = "; ".join(
                    f"{m['question']} (needs fact: {m['needs_fact']})" for m in report["missing"]
                )
                record(
                    resume_id, "skipped", f"preflight: {missing}",
                    title=title, company=company, missing=report["missing"],
                )
                continue

        real_chrome = _needs_real_chrome(job.get("url") or "")
        host_kind = "real-chrome" if real_chrome else "tab-host"
        if host_kind not in host_errors:
            try:
                (_ensure_real_chrome if real_chrome else _ensure_tab_host)(data_dir)
                host_errors[host_kind] = None
            except RuntimeError as exc:
                host_errors[host_kind] = str(exc)
        if host_errors[host_kind] is not None:
            remedy = _REAL_CHROME_REMEDY if real_chrome else _TAB_HOST_REMEDY
            record(
                resume_id, "failed",
                f"{host_kind} unavailable ({host_errors[host_kind]}) — {remedy}",
                title=title, company=company, ledger_id=None, status="failed",
                exit_code=EXIT_FAILED,
            )
            continue
        before = max((r["id"] for r in rows), default=0)
        try:
            # Inner runs print human lines, and to STDERR: batch's stdout must
            # stay ONE JSON document under `> file` / a pipe, never N per-run
            # blobs (screenshots included) with a report at the end.
            with contextlib.redirect_stdout(sys.stderr):
                code = cmd_apply(
                    argparse.Namespace(
                        data_dir=getattr(args, "data_dir", None),
                        json=False,
                        force_human=True,
                        resume_id=resume_id,
                        dry_run=False,
                        resume_url=None,
                        max_actions=getattr(args, "max_actions", None),
                        force=True,  # the gate already ran above (or --force waived it)
                        visible=True,
                        hold=True,
                        tab=True,
                        notify=False,  # one ping for the whole cycle, not one per fill
                        cover=None,
                    )
                )
        except Exception as exc:  # one bad entry must never abort the cycle
            record(
                resume_id, "failed", f"{type(exc).__name__}: {exc}",
                title=title, company=company, ledger_id=None, status="failed", exit_code=EXIT_FAILED,
            )
            continue
        # Only a row this run wrote may speak for it: a usage-error exit writes
        # none, and a STALE row (say an old audit_pending park) must not turn
        # a run that never opened a browser into a "pending" success.
        row = next((r for r in _applications_for(conn, resume_id) if r["id"] > before), None)
        if row is None:
            record(
                resume_id, "failed",
                f"run exited {code} before reaching the browser — no ledger row written (see its error above)",
                title=title, company=company, ledger_id=None, status="failed", exit_code=code,
            )
            continue
        status = row.get("status") or "failed"
        reason = str((row.get("response") or {}).get("reason") or (row.get("response") or {}).get("error") or "")
        entry = {
            "title": title,
            "company": company,
            "ledger_id": row.get("id"),
            "status": status,
            "exit_code": code,
        }
        if status in _BATCH_TERMINAL:
            record(resume_id, "filled", reason or "filled — review the tab and press send", **entry)
        elif status == "audit_pending":
            record(resume_id, "pending", reason or "a human finishes one field, then sends", **entry)
        else:
            record(resume_id, "failed", reason or "no reason recorded", **entry)

    filled = [r for r in results if r["outcome"] == "filled"]
    pending = [r for r in results if r["outcome"] == "pending"]
    skipped = [r for r in results if r["outcome"] == "skipped"]
    failed = [r for r in results if r["outcome"] == "failed"]

    def _job_cell(entry: dict[str, Any]) -> str:
        return completion.job_line(entry.get("title"), entry.get("company"))

    human = [completion.RULE, f"CYCLE REPORT — {len(results)} application(s)"]
    for entry in filled:
        human.append(f"  ✓ held     #{entry['resume_id']} {_job_cell(entry)} (ledger #{entry['ledger_id']})")
    for entry in pending:
        human.append(f"  ⏸ pending  #{entry['resume_id']} {_job_cell(entry)} — {entry['reason'][:120]}")
    for entry in skipped:
        human.append(f"  → skipped  #{entry['resume_id']} {_job_cell(entry)} — {entry['reason'][:160]}")
    for entry in failed:
        human.append(f"  ✗ failed   #{entry['resume_id']} {_job_cell(entry)} — {entry['reason'][:120]}")
    if filled or pending:
        human.append("  next    A. finish any flagged fields in the open tabs")
        human.append("          B. review each held tab and press send yourself")
    human.append(completion.RULE)

    if completion.wants_notification(getattr(args, "notify", None), visible=True):
        completion.notify(
            f"cycle done — {len(filled)} held, {len(pending)} need a human, "
            f"{len(skipped)} skipped, {len(failed)} failed"
        )

    code = EXIT_OK if not failed else EXIT_FAILED
    data = {
        "ok": not failed,
        "results": results,
        "held": len(filled),
        "pending": len(pending),
        "skipped": len(skipped),
        "failed": len(failed),
        "exit_code": code,
    }
    return code, data, human


def cmd_cycle(args: argparse.Namespace) -> int:
    """One argv, whole pipeline: find → add+adapt → batch fill as held tabs.

    Built for cron/launchd and gated agent harnesses that can execute exactly
    ONE command with no shell features — no `source`, no `&&`, no pipes:
    `<repo>/.venv/bin/weaver cycle --count 5` does the entire morning flow.
    Credentials ride in via <data-dir>/env (see config.load_env_file). All
    progress prints to stderr; stdout is ONE JSON document (or the human
    report on a tty). Never submits — every fill is a --hold park.
    """
    data_dir, conn, config = open_db(args)
    lock, holder = _take_batch_lock(data_dir)
    if holder is not None:
        return fail(
            args,
            f"another fill cycle is already running (pid {holder}) — one cycle at a "
            f"time; if that pid is not a live weaver run, delete {lock}",
            EXIT_USAGE,
        )
    try:
        find_ns = argparse.Namespace(
            data_dir=getattr(args, "data_dir", None),
            json=False,
            force_human=True,
            orgs=list(args.orgs or []),
            wide=getattr(args, "wide", False),
            threshold=args.threshold,
            limit=max(0, args.count),
            max_boards=getattr(args, "max_boards", boards_mod.DEFAULT_MAX_BOARDS),
            delay=getattr(args, "delay", boards_mod.DEFAULT_DELAY),
        )
        with contextlib.redirect_stdout(sys.stderr):
            find_code = cmd_find(find_ns)
        if find_code != EXIT_OK:
            return fail(args, "the find lane failed — its report is on stderr", find_code)
        try:
            shortlist = json.loads((data_dir / "jobs.json").read_text(encoding="utf-8"))
        except Exception as exc:
            return fail(args, f"could not read the shortlist ({exc})")
        entries = list(shortlist.get("jobs") or [])[: max(0, args.count)]

        #: Shortlist entries that never became a batchable resume, and why —
        #: the same relay-don't-die rule the batch applies to preflight skips.
        prep: list[dict[str, Any]] = []
        resume_ids: list[int] = []

        def prep_skip(entry: dict[str, Any], stage: str, reason: str) -> None:
            prep.append(
                {
                    "url": entry.get("url") or "",
                    "role": entry.get("role") or "",
                    "org": entry.get("org") or "",
                    "stage": stage,
                    "reason": reason,
                }
            )

        for entry in entries:
            url = str(entry.get("url") or "").strip()
            if not url:
                prep_skip(entry, "add", "shortlist entry has no URL")
                continue
            # Re-adding an already-fetched URL would mint a duplicate job row —
            # and job-level dedupe keys on job_id, so duplicates are dangerous.
            row = conn.execute("SELECT id, raw_text FROM jobs WHERE url = ?", (url,)).fetchone()
            if row is not None and len(str(row["raw_text"] or "").strip()) >= 200:
                job_id = int(row["id"])
            else:
                try:
                    job_id = int(jobs_mod.add(conn, url, fetch_url=True)["id"])
                except RuntimeError as exc:
                    prep_skip(entry, "add", str(exc))
                    continue
            tailor_ns = argparse.Namespace(
                data_dir=getattr(args, "data_dir", None),
                json=False,
                force_human=True,
                source="graph",
                job=str(job_id),
                lens=None,
                no_llm=False,
                format=args.format,
                out=None,
                max_roles=tailor_mod.MAX_ROLES,
            )
            with contextlib.redirect_stdout(sys.stderr):
                tailor_code = cmd_tailor(tailor_ns)
            if tailor_code != EXIT_OK:
                prep_skip(entry, "adapt", f"adapt refused (exit {tailor_code}) — see stderr")
                continue
            resume = next((r for r in db.get_resumes(conn) if r.get("job_id") == job_id), None)
            if resume is None:
                prep_skip(entry, "adapt", "adapt reported success but no resume row exists")
                continue
            resume_ids.append(int(resume["id"]))

        code, batch_data, batch_human = _batch_run(args, conn, config, data_dir, resume_ids)
        data = {
            "found": len(entries),
            "adapted": len(resume_ids),
            "prep_skips": prep,
            **batch_data,
        }
        human = [
            f"cycle — {len(entries)} job(s) found, {len(resume_ids)} adapted "
            f"({args.format}), {len(prep)} prep skip(s)"
        ]
        for p in prep:
            human.append(
                f"  → prep-skip [{p['stage']}] "
                f"{completion.job_line(p.get('role'), p.get('org'))} — {p['reason'][:140]}"
            )
        human += batch_human
        emit(args, data, human)
        if wants_json(args):
            print("\n".join(human), file=sys.stderr)
        return code
    finally:
        _release_batch_lock(lock)


def cmd_apps_list(args: argparse.Namespace) -> int:
    _data_dir, conn, _config = open_db(args)
    applications = db.get_applications(conn, limit=getattr(args, "limit", None))
    rows = []
    for app in applications:
        rows.append(
            {
                "id": app["id"],
                "resume_id": app["resume_id"],
                "job_id": app["job_id"],
                "status": app["status"],
                "lens": app.get("lens"),
                "company": app.get("company"),
                "title": app.get("title"),
                "url": app.get("url"),
                "resume_path": app.get("resume_path"),
                "created_at": app["created_at"],
            }
        )
    human = [f"{len(rows)} application(s)"]
    for row in rows:
        human.append(
            f"  #{row['id']:<3d} {row['status']:<9s} resume #{row['resume_id']} "
            f"({row['lens'] or '-'})  {row['title'] or row['company'] or '(no job linked)'}"
        )
        human.append(f"       {row['created_at']}  {row['resume_path'] or ''}")
    return emit(args, {"ok": True, "count": len(rows), "applications": rows}, human)


def cmd_apps_set_status(args: argparse.Namespace) -> int:
    """Reconcile a ledger row with what actually happened.

    weaver parks a filled form and a HUMAN presses send — the engine never sees
    that, so `held` rows stay `held` forever and the ledger drifts away from
    reality. The alternative was hand-written UPDATEs against the live db, which
    is how a ledger stops being worth reading.
    """
    _data_dir, conn, _config = open_db(args)
    status = str(args.status or "").strip()
    if status not in LEDGER_STATUSES:
        return fail(
            args,
            f"unknown status {status!r} — use one of: {', '.join(sorted(LEDGER_STATUSES))}",
            EXIT_USAGE,
        )
    app = db.get_application(conn, args.application_id)
    if app is None:
        return fail(args, f"no such application: {args.application_id}", EXIT_USAGE)
    was = str(app.get("status") or "")
    note = str(getattr(args, "note", "") or "").strip()
    response = app.get("response")
    response = dict(response) if isinstance(response, dict) else {}
    # Append, never overwrite: the run's own response is evidence of what the
    # engine did, and a later human correction must not erase it.
    history = list(response.get("status_history") or [])
    history.append({"from": was, "to": status, "at": db.now(), "note": note})
    response["status_history"] = history
    conn.execute(
        "UPDATE applications SET status = ?, response = ? WHERE id = ?",
        (status, json.dumps(response, ensure_ascii=False, default=str), args.application_id),
    )
    conn.commit()
    human = [f"application #{args.application_id}: {was or '(none)'} → {status}"]
    if note:
        human.append(f"  note: {note}")
    return emit(
        args,
        {"ok": True, "id": args.application_id, "was": was, "status": status, "note": note},
        human,
    )


def cmd_apps_show(args: argparse.Namespace) -> int:
    _data_dir, conn, _config = open_db(args)
    app = db.get_application(conn, args.application_id)
    if app is None:
        return fail(args, f"no such application: {args.application_id}", EXIT_USAGE)
    human = [
        f"application #{app['id']} ({app['status']})",
        f"  resume  #{app['resume_id']}",
        f"  job     {app.get('title') or '-'} @ {app.get('company') or '-'}",
        f"  created {app['created_at']}",
        "",
        json.dumps(app["payload"], indent=2, ensure_ascii=False)[:4000],
    ]
    return emit(args, {"ok": True, "application": app}, human)


def cmd_ledger(args: argparse.Namespace) -> int:
    """The application ledger — one line per run, newest first (`--json` to pipe)."""
    _data_dir, conn, _config = open_db(args)
    entries = ledger_mod.rows(conn)
    status_filter = (getattr(args, "status", None) or "").strip()
    if status_filter:
        entries = [e for e in entries if e["status"] == status_filter]
    limit = getattr(args, "limit", None)
    if limit is not None:
        entries = entries[: max(0, limit)]
    data = {
        "ok": True,
        "count": len(entries),
        "by_status": ledger_mod.counts(entries),
        "applications": entries,
    }
    return emit(args, data, ledger_mod.format_table(entries))


def cmd_serve(args: argparse.Namespace) -> int:
    from . import serve as serve_mod

    data_dir, conn, _config = open_db(args)
    conn.close()
    server = serve_mod.make_server(data_dir, args.host, args.port)
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{port}/"
    if wants_json(args):
        print(json.dumps({"ok": True, "url": url, "data_dir": str(data_dir)}, indent=2))
    else:
        print(f"weaver serve → {url}  (ctrl-c to stop)")
        print(f"  data dir  {data_dir}")
        print(f"  api       {url}api")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    _data_dir, conn, config = open_db(args)
    data = {"ok": True, **db.stats(conn), "llm": llm.describe(), "profile": config.get("profile")}
    human = [
        f"facts       {data['facts_total']}",
        "  " + ", ".join(f"{k}={v}" for k, v in data["facts_by_kind"].items()),
        f"verified    {data['verified']} ({data['verified_pct']}%)",
        f"skills      {data['skills']}",
        f"domains     {data['domains']}",
        f"lenses      {data['lenses']}",
        f"jobs        {data['jobs']}",
        f"resumes     {data['resumes']}",
        f"seed files  {len(data['seed_files'])}",
        f"llm         {data['llm']['provider']}",
    ]
    return emit(args, data, human)


def cmd_boards(args: argparse.Namespace) -> int:
    """The board registry `weaver find --wide` sweeps (list / add)."""
    data_dir = cfg.resolve_data_dir(getattr(args, "data_dir", None))
    config = cfg.load_config(data_dir)
    problems: list[str] = []
    if getattr(args, "board_action", "list") == "add":
        try:
            entry = boards_mod.add(data_dir, args.org, args.board)
        except boards_mod.BoardError as exc:
            return fail(args, str(exc), EXIT_USAGE)
        registry = boards_mod.load_registry(data_dir, config, problems)
        data = {
            "ok": True,
            "added": entry,
            "registry": str(boards_mod.registry_path(data_dir)),
            "count": len(registry),
            "problems": problems,
        }
        return emit(
            args,
            data,
            [
                f"added {entry['name']} ({entry['kind']}:{entry['slug']}) → "
                f"{boards_mod.registry_path(data_dir)}",
                f"{len(registry)} board(s) in the registry — sweep them with `weaver find --wide`",
            ],
        )

    registry = boards_mod.load_registry(data_dir, config, problems)
    by_kind: dict[str, int] = {}
    for entry in registry:
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
    data = {
        "ok": True,
        "count": len(registry),
        "by_kind": dict(sorted(by_kind.items())),
        "registry": str(boards_mod.registry_path(data_dir)),
        "boards": registry,
        "problems": problems,
    }
    human = [
        f"{len(registry)} board(s)  " + ", ".join(f"{k}={v}" for k, v in data["by_kind"].items()),
        f"{'name':<24}  {'kind':<10}  {'slug':<24}  tags",
        f"{'-' * 24}  {'-' * 10}  {'-' * 24}  {'-' * 4}",
    ]
    for entry in registry:
        human.append(
            f"{entry['name'][:24]:<24}  {entry['kind']:<10}  "
            f"{(entry['slug'] or entry['url'])[:24]:<24}  {', '.join(entry['tags'])}"
        )
    for problem in problems:
        human.append(f"  ! {problem}")
    return emit(args, data, human)


def cmd_find(args: argparse.Namespace) -> int:
    """Lane 1 — scan public ATS boards, score fit vs the saved profile, rank."""
    data_dir, conn, config = open_db(args)
    targets = jobs_mod.load_targets(config)
    configured = dict((config.get("find") or {}).get("orgs") or {})
    wide = bool(getattr(args, "wide", False))
    problems: list[str] = []
    registry: list[dict[str, Any]] = []
    if wide:
        if args.orgs:
            return fail(args, "--wide sweeps the board registry — drop the org arguments", EXIT_USAGE)
        registry = boards_mod.load_registry(data_dir, config, problems)
        if not registry:
            return fail(args, "empty board registry — add one with `weaver boards add <org> [board]`", EXIT_USAGE)
        boards = {}
    else:
        try:
            boards = jobs_mod.resolve_orgs(list(args.orgs or []), configured)
        except jobs_mod.FindError as exc:
            return fail(args, str(exc), EXIT_USAGE)
        if not boards:
            return fail(args, "no orgs configured — add find.orgs to config.json or pass org:board", EXIT_USAGE)
    # Rank everything above the threshold first, drop what's already in the
    # ledger, and only then apply --limit — deduping must not silently shrink
    # the shortlist below the number of fresh jobs the user asked for.
    if wide:
        result = boards_mod.sweep(
            registry,
            targets,
            threshold=args.threshold,
            limit=sys.maxsize,
            max_boards=args.max_boards,
            delay=args.delay,
        )
        boards = {entry["slug"]: entry["kind"] for entry in result["swept"]}
    else:
        result = jobs_mod.find_jobs(boards, targets, threshold=args.threshold, limit=sys.maxsize)
    if result["errors"] and not result["jobs"]:
        detail = "; ".join(f"{e['org']}: {e['error']}" for e in result["errors"])
        return fail(args, f"all boards failed — {detail}")
    fresh, excluded = ledger_mod.dedupe(result["jobs"], ledger_mod.applied_urls(conn))
    result["jobs"] = fresh[: max(0, args.limit)]
    shortlist = jobs_mod.write_shortlist(data_dir, result, targets)

    data = {
        "ok": True,
        "wide": wide,
        "orgs": boards,
        "threshold": args.threshold,
        "limit": args.limit,
        "scanned": result["scanned"],
        "count": len(result["jobs"]),
        "already_applied": len(excluded),
        "already_applied_jobs": [
            {"role": job["role"], "org": job["org"], "url": job["url"]} for job in excluded
        ],
        "errors": result["errors"],
        "shortlist": str(shortlist),
        "jobs": result["jobs"],
    }
    if wide:
        data["registry"] = {
            "registered": result["boards_registered"],
            "swept": result["boards_swept"],
            "over_cap": result["boards_over_cap"],
            "unsupported": result["boards_unsupported"],
            "max_boards": args.max_boards,
            "delay": args.delay,
        }
        data["problems"] = problems
    human = [
        f"scanned {result['scanned']} posting(s) across {len(boards)} board(s) — "
        f"{len(result['jobs'])} at or above {args.threshold:.2f} fit → {shortlist}"
    ]
    if wide:
        human.insert(
            0,
            f"wide sweep — {result['boards_swept']}/{result['boards_registered']} registry "
            f"board(s), {args.delay:.2f}s apart, cap {args.max_boards}",
        )
        # No silent caps: say exactly what the sweep did not reach.
        if result["boards_over_cap"]:
            human.append(
                f"  ({len(result['boards_over_cap'])} board(s) past the --max-boards cap, "
                f"not swept: {', '.join(result['boards_over_cap'][:5])}…)"
            )
        if result["boards_unsupported"]:
            human.append(
                f"  ({len(result['boards_unsupported'])} registry board(s) have no public "
                f"feed parser yet: {', '.join(result['boards_unsupported'][:5])})"
            )
        for problem in problems:
            human.append(f"  ! {problem}")
    if excluded:
        human.append(f"  ({len(excluded)} already applied, skipped — see `weaver ledger`)")
    for err in result["errors"]:
        human.append(f"  ! {err['org']} ({err['board']}): {err['error']}")
    for index, job in enumerate(result["jobs"], 1):
        where = f" · {job['location']}" if job["location"] else ""
        human.append(f"  {index:>2d}. {job['fit']:.2f}  {job['role']} — {job['org']} ({job['board']}){where}")
        if job["reasons"]:
            human.append(f"       {' · '.join(job['reasons'])}")
        if job["url"]:
            human.append(f"       {job['url']}")
    if not result["jobs"]:
        human.append(
            f"  (every match at or above {args.threshold:.2f} fit is already in the ledger)"
            if excluded
            else f"  (nothing at or above {args.threshold:.2f} fit — lower --fit or widen "
            "profile.target_roles/target_skills in config.json)"
        )
    return emit(args, data, human)


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        default=argparse.SUPPRESS,
        help="where weaver.db and config.json live (default ./data)",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="force JSON output (automatic when stdout is piped)",
    )

    parser = argparse.ArgumentParser(
        prog="weaver",
        parents=[common],
        description="CareerWeaver — canonical fact graph + persona lenses → tailored resumes.",
    )
    parser.add_argument("--version", action="version", version=f"weaver {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser(
        "init",
        parents=[common],
        help="create the DB + a blank profile config (your details, never shipped)",
        description=(
            "Create <data-dir>/weaver.db and <data-dir>/config.json. The profile "
            "starts empty; fill it with --interactive, with the flags below, or "
            "with WEAVER_PROFILE_<FIELD> environment variables. Re-running never "
            "blanks a field you already set."
        ),
    )
    p_init.add_argument("--reset-lenses", action="store_true", help="re-seed shipped lenses")
    p_init.add_argument(
        "-i", "--interactive", action="store_true", help="prompt for each profile field"
    )
    for key, label in cfg.PROFILE_PROMPTS:
        p_init.add_argument(
            f"--{key.replace('_', '-')}",
            dest=key,
            default=None,
            help=label + (" (comma separated)" if key in cfg.LIST_KEYS else ""),
        )
    p_init.set_defaults(func=cmd_init)

    p_seed = sub.add_parser(
        "seed-import", parents=[common], help="import seed resumes into the fact graph"
    )
    p_seed.add_argument("--dir", default="seed_resumes", help="directory of seed resumes")
    p_seed.add_argument("--no-llm", action="store_true", help="force the deterministic parser")
    p_seed.add_argument(
        "--overwrite-profile", action="store_true", help="replace config profile fields"
    )
    p_seed.set_defaults(func=cmd_seed_import)

    p_lens = sub.add_parser("lens", parents=[common], help="persona lenses")
    lens_sub = p_lens.add_subparsers(dest="lens_command")
    p_lens_list = lens_sub.add_parser("list", parents=[common])
    p_lens_list.set_defaults(func=cmd_lens_list)
    p_lens_show = lens_sub.add_parser("show", parents=[common])
    p_lens_show.add_argument("name")
    p_lens_show.set_defaults(func=cmd_lens_show)
    p_lens_create = lens_sub.add_parser("create", parents=[common])
    p_lens_create.add_argument("--name", required=True)
    p_lens_create.add_argument("--lead-domains", required=True, help="comma separated")
    p_lens_create.add_argument("--titles", default="", help="semicolon separated")
    p_lens_create.add_argument("--compress-domains", default="")
    p_lens_create.add_argument("--tone", default="")
    p_lens_create.add_argument("--notes", default="")
    p_lens_create.set_defaults(func=cmd_lens_create)
    p_lens.set_defaults(func=cmd_lens_list)

    p_tailor = sub.add_parser(
        "tailor", parents=[common], aliases=["adapt"],
        help="generate a tailored resume",
    )
    p_tailor.add_argument(
        "source",
        nargs="?",
        default=None,
        help="source resume file (imported if new), or 'graph' for the whole fact graph",
    )
    p_tailor.add_argument("--job", help="job id, url, file, or raw posting text")
    p_tailor.add_argument("--lens", help="lens name")
    p_tailor.add_argument("--out", help="output path")
    p_tailor.add_argument("--format", default="md", choices=["md", "docx", "pdf"])
    p_tailor.add_argument("--status", default="draft", choices=["draft", "final"])
    p_tailor.add_argument("--max-roles", type=int, default=tailor_mod.MAX_ROLES)
    p_tailor.add_argument("--no-llm", action="store_true", help="force the deterministic path")
    p_tailor.set_defaults(func=cmd_tailor)

    p_jobs = sub.add_parser("jobs", parents=[common], help="saved job postings")
    jobs_sub = p_jobs.add_subparsers(dest="jobs_command")
    p_jobs_add = jobs_sub.add_parser("add", parents=[common])
    p_jobs_add.add_argument("value", help="url, file path, raw text, or - for stdin")
    p_jobs_add.add_argument("--fetch", action="store_true", help="fetch the url (network)")
    p_jobs_add.set_defaults(func=cmd_jobs_add)
    p_jobs_list = jobs_sub.add_parser("list", parents=[common])
    p_jobs_list.set_defaults(func=cmd_jobs_list)
    p_jobs.set_defaults(func=cmd_jobs_list)

    p_preflight = sub.add_parser("preflight", parents=[common], help="check a saved job's form questions against the applicant facts (no browser involved)")
    p_preflight.add_argument("job_id", type=int)
    p_preflight.set_defaults(func=cmd_preflight)

    p_apply = sub.add_parser("apply", parents=[common], help="auto-apply via the weaver-apply worker (Cloudflare Browser Rendering)")
    p_apply.add_argument("resume_id", type=int)
    p_apply.add_argument("--dry-run", action="store_true", help="build and print the request only")
    p_apply.add_argument("--resume-url", default=None, help="public http(s) URL of the resume file (default: WEAVER_RESUME_URL or the payload's hosted resume)")
    p_apply.add_argument(
        "--max-actions",
        type=int,
        default=None,
        help=(
f"cap the agent loop (default {local_agent.DEFAULT_MAX_ACTIONS})"
        ),
    )
    p_apply.add_argument(
        "--force",
        action="store_true",
        help="skip the preflight coverage gate (still never fabricates — the guardrail stays in the agent)",
    )
    p_apply.add_argument(
        "--local",
        action="store_true",
        help="deprecated no-op: the local chromium is the only engine",
    )
    p_apply.add_argument(
        "--visible",
        action="store_true",
        help="show the browser window so you can watch (and stop) the run",
    )
    p_apply.add_argument(
        "--hold",
        action="store_true",
        help="fill everything but NEVER submit — park at audit_pending "
        "(window stays open with --visible) so a human reviews and presses send",
    )
    p_apply.add_argument(
        "--tab",
        action="store_true",
        help="open the run as a TAB of the shared tab-host window (started "
        "automatically) instead of a new browser window per run",
    )
    p_apply.add_argument(
        "--notify",
        dest="notify",
        action="store_true",
        default=None,
        help="macOS notification when the run ends (default: on for --visible runs)",
    )
    p_apply.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        help="never post the end-of-run notification, even on a --visible run",
    )
    p_apply.add_argument(
        "--cover",
        default=None,
        help="cover letter file (path or URL) — attached to a cover-letter input; "
        "without it, cover-letter inputs are left empty (never the resume)",
    )
    p_apply.set_defaults(func=cmd_apply)

    p_batch = sub.add_parser(
        "batch",
        parents=[common],
        help="fill a queue of applications back-to-back as tabs of the shared "
        "window — each run marks complete on fill (never on tab close); skips "
        "and holds are relayed in one cycle report at the end",
    )
    p_batch.add_argument("resume_ids", nargs="+", type=int, metavar="resume_id")
    p_batch.add_argument(
        "--force",
        action="store_true",
        help="skip the preflight coverage gate for every run in the cycle",
    )
    p_batch.add_argument(
        "--max-actions",
        type=int,
        default=None,
        help=f"cap each run's agent loop (default {local_agent.DEFAULT_MAX_ACTIONS})",
    )
    p_batch.add_argument(
        "--notify",
        dest="notify",
        action="store_true",
        default=None,
        help="macOS notification when the cycle ends (default: on)",
    )
    p_batch.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        help="never post the end-of-cycle notification",
    )
    p_batch.set_defaults(func=cmd_batch)

    p_cycle = sub.add_parser(
        "cycle",
        parents=[common],
        help="one-shot pipeline for cron/agent harnesses: find → adapt → batch "
        "fill as held tabs — a single argv with no shell required; credentials "
        "load from <data-dir>/env; stdout is one JSON document",
    )
    p_cycle.add_argument(
        "orgs",
        nargs="*",
        metavar="org",
        help="org slug(s) to scan; none = the configured find.orgs (or --wide)",
    )
    p_cycle.add_argument(
        "--count", type=int, default=5, help="fresh jobs to fill this cycle (default 5)"
    )
    p_cycle.add_argument(
        "--wide",
        action="store_true",
        help="sweep the whole board registry instead of the configured orgs",
    )
    p_cycle.add_argument(
        "--fit", dest="threshold", type=float, default=0.6, help="minimum fit, 0..1 (default 0.6)"
    )
    p_cycle.add_argument(
        "--format",
        default="docx",
        choices=["docx", "pdf", "md"],
        help="resume format to adapt (default docx — fastest render, ATS-accepted)",
    )
    p_cycle.add_argument(
        "--force",
        action="store_true",
        help="skip the preflight coverage gate for every run in the cycle",
    )
    p_cycle.add_argument(
        "--max-actions",
        type=int,
        default=None,
        help=f"cap each run's agent loop (default {local_agent.DEFAULT_MAX_ACTIONS})",
    )
    p_cycle.add_argument(
        "--max-boards",
        dest="max_boards",
        type=int,
        default=boards_mod.DEFAULT_MAX_BOARDS,
        help=f"--wide: max boards per sweep (default {boards_mod.DEFAULT_MAX_BOARDS})",
    )
    p_cycle.add_argument(
        "--delay",
        type=float,
        default=boards_mod.DEFAULT_DELAY,
        help=f"--wide: seconds between board requests (default {boards_mod.DEFAULT_DELAY})",
    )
    p_cycle.add_argument(
        "--notify",
        dest="notify",
        action="store_true",
        default=None,
        help="macOS notification when the cycle ends (default: on)",
    )
    p_cycle.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        help="never post the end-of-cycle notification",
    )
    p_cycle.set_defaults(func=cmd_cycle)

    p_tab_host = sub.add_parser(
        "tab-host",
        parents=[common],
        help="the shared browser window that `apply --tab` runs open tabs in "
        "(started automatically by --tab; run it yourself to pre-open it)",
    )
    p_tab_host.add_argument("--port", type=int, default=None, help="CDP port (default WEAVER_CDP_PORT or 9777)")
    p_tab_host.set_defaults(func=cmd_tab_host)

    p_apps = sub.add_parser("apps", parents=[common], help="application ledger")
    apps_sub = p_apps.add_subparsers(dest="apps_command")
    p_apps_list = apps_sub.add_parser("list", parents=[common])
    p_apps_list.add_argument("--limit", type=int, default=None)
    p_apps_list.set_defaults(func=cmd_apps_list)
    p_apps_show = apps_sub.add_parser("show", parents=[common])
    p_apps_show.add_argument("application_id", type=int)
    p_apps_show.set_defaults(func=cmd_apps_show)
    p_apps_set = apps_sub.add_parser(
        "set-status",
        parents=[common],
        help="record what really happened to an application (weaver cannot see a human send)",
    )
    p_apps_set.add_argument("application_id", type=int)
    # Not argparse `choices=`: a typo must come back as weaver's own usage
    # failure (JSON-shaped, EXIT_USAGE) like every other bad argument here,
    # not argparse's bare exit-2 with no machine-readable error.
    p_apps_set.add_argument("status", help=f"one of: {', '.join(sorted(LEDGER_STATUSES))}")
    p_apps_set.add_argument(
        "--note", default="", help="why it changed — kept on the row alongside the run's response"
    )
    p_apps_set.set_defaults(func=cmd_apps_set_status)
    p_apps.set_defaults(func=cmd_apps_list)

    p_ledger = sub.add_parser(
        "ledger",
        parents=[common],
        help="the application ledger — id · date · org · role · status · note",
        description=(
            "One line per application run, newest first. `held` means the form is "
            "filled and waiting for your send (--hold); `audit_pending` means the "
            "run hit a wall a human must clear."
        ),
    )
    p_ledger.add_argument("--limit", type=int, default=None, help="show only the newest N")
    p_ledger.add_argument(
        "--status",
        default=None,
        choices=list(ledger_mod.STATUSES),
        help="show only rows with this status",
    )
    p_ledger.set_defaults(func=cmd_ledger)

    p_serve = sub.add_parser(
        "serve", parents=[common], help="local read-only viewer for the graph and ledger"
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.set_defaults(func=cmd_serve)

    p_find = sub.add_parser(
        "find",
        parents=[common],
        help="Lane 1 — scan public ATS boards (greenhouse/lever/ashby/workable) and rank postings by profile fit",
    )
    p_find.add_argument(
        "orgs",
        nargs="*",
        metavar="org",
        help="org slug, or org:greenhouse|lever|ashby|workable; none = the configured find.orgs set",
    )
    p_find.add_argument("--limit", type=int, default=10, help="max shortlist entries (default 10)")
    p_find.add_argument(
        "--fit", dest="threshold", type=float, default=0.6, help="minimum fit, 0..1 (default 0.6)"
    )
    p_find.add_argument(
        "--wide",
        action="store_true",
        help="sweep the whole board registry (see `weaver boards`) instead of the configured orgs",
    )
    p_find.add_argument(
        "--max-boards",
        dest="max_boards",
        type=int,
        default=boards_mod.DEFAULT_MAX_BOARDS,
        help=f"--wide: max boards per sweep (default {boards_mod.DEFAULT_MAX_BOARDS})",
    )
    p_find.add_argument(
        "--delay",
        type=float,
        default=boards_mod.DEFAULT_DELAY,
        help=f"--wide: seconds between board requests (default {boards_mod.DEFAULT_DELAY})",
    )
    p_find.set_defaults(func=cmd_find)

    p_boards = sub.add_parser(
        "boards",
        parents=[common],
        help="the board registry `weaver find --wide` sweeps (list / add)",
    )
    boards_sub = p_boards.add_subparsers(dest="board_action")
    p_boards_list = boards_sub.add_parser("list", parents=[common], help="show the merged registry")
    p_boards_list.set_defaults(func=cmd_boards, board_action="list")
    p_boards_add = boards_sub.add_parser(
        "add", parents=[common], help="add a board to <data-dir>/boards.json"
    )
    p_boards_add.add_argument("org", help="board slug (as it appears in the public feed URL)")
    p_boards_add.add_argument(
        "board",
        nargs="?",
        default="greenhouse",
        choices=list(boards_mod.BOARD_KINDS),
        help="board kind (default greenhouse)",
    )
    p_boards_add.set_defaults(func=cmd_boards, board_action="add")
    p_boards.set_defaults(func=cmd_boards, board_action="list")

    p_stats = sub.add_parser("stats", parents=[common], help="fact graph statistics")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return EXIT_USAGE
    # Before ANY command: a cron/launchd/agent-harness invocation has no shell
    # profile, so <data-dir>/env is the one place credentials can ride in from.
    with contextlib.suppress(Exception):
        cfg.load_env_file(cfg.resolve_data_dir(getattr(args, "data_dir", None)))
    try:
        return int(args.func(args))
    except BrokenPipeError:  # `weaver stats | head`
        return EXIT_OK
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
