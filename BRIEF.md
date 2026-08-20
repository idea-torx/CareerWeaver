# CareerWeaver — build brief (Leo approved: build locally, core + CLI, no branding)

**APPLY ENGINE (Aug 14 2026): Cloudflare Browser Rendering is the default auto-apply engine**
(worker/ subdir, LLM-in-the-loop agent, BROWSER binding — Leo's account already runs it in
prod on invoicer-pro). Skyvern = optional adapter only (`--provider skyvern`). Full spec:
/tmp/weaver_cf_browser_brief.md. No step caps, pay-per-browser-minute, open-source "bring your
own CF account".

Conductor: Hermes. Workhorse: you (Claude Code). Project: the repo root (this checkout).
Goal: a working, tested, CLI-first resume-tailoring engine. No web UI, no sales assets,
no branding beyond the name. Branding/PDFs/banners come later (fal.ai).

**Open-source from commit one** (Leo's call — AIApply-class tools are "outrageously
expensive"; we ship the free/agent-first alternative). MIT LICENSE is in the repo root.
Public repo will live at github.com/idea-torx/resumeweaver (like gitvoice).

## PRIVACY HARD RULE (this matters more than any feature)
`seed_resumes/` contains LEO'S REAL PERSONAL DATA (real phone, email, employers, metrics).
It is gitignored and MUST NEVER be committed or referenced in any public-facing file/README.
For demos/tests that ship in the repo, create `samples/` with a SYNTHETIC persona
(fictional name/company/metrics) exercising the same shapes — e.g. `samples/sample-resume.md`,
`samples/sample-job.txt`. The seed-import command must accept any dir; tests must not depend
on the private seed files existing (CI on a fresh clone must pass with samples/ only).

## The product in one sentence
Given Leo's canonical career facts + a persona lens (or a job posting), generate a tailored
resume that flexes the narrative — same facts, different emphasis — without ever inventing
experience. Auto-apply via Skyvern is a secondary hook (dry-run only for now).

## Design (already decided — implement as specified)

### Data model — SQLite (single file `data/weaver.db`)
- `facts` — the canonical fact graph. Columns: id, kind (role|project|metric|tool|award|client|education|skill|domain), title, org, start, end, location, bullets (JSON list), metrics (JSON list), tags (JSON list), source (which seed file), verified (int, default 0).
- `skills` — skill entries: id, name, domains (JSON list) — e.g. name="Blender" domains=["cgi","motion"].
- `domains` — the skill taxonomy (Leo's list): cgi_motion, design_engineering, video_multimedia, graphics_brand, direction_pm, ai_expertise, sre_cloud, agentic_engineering, fullstack_engineering.
- `lenses` — persona lens definitions: id, name, target_titles (JSON), lead_domains (JSON, ordered), compress_domains (JSON), summary_tone (text), skills_order (JSON), notes.
- `jobs` — saved job postings: id, url, title, company, raw_text, skills_required (JSON), created_at.
- `resumes` — generated resumes: id, lens, job_id, status (draft|final), format (md|docx), path, created_at, prompt_used, source_facts (JSON list of fact ids used — audit!).
- `applications` — the AUTO-APPLY LEDGER (trace): id, job_id, resume_id, cover_letter_path, cover_letter_format, status (draft|applied|error|rejected), skyvern_task_id, applied_at, response_notes, created_at. Every apply attempt (even dry-run drafts) records a row — THIS is the trace users see.

### Lightweight GUI — the trace view (NOT a product UI)
CLI + agentic end-to-end remains the primary surface. The GUI is a thin, read-only trace
so users can see WHERE the agent applied and WITH WHAT (which resume version + cover letter).
- `weaver serve` — stdlib `http.server` (ZERO new dependencies), binds 127.0.0.1, one page.
- Page shows the applications ledger: per application row — job title/company/url, applied_at,
  status, skyvern task link, and the exact resume (docx/md) + cover letter it applied with
  (links to open/download; render md inline).
- JSON endpoints under /api (same data) so agents/CLI can query the ledger too.
- No auth, localhost only, read-only, no editing. No CSS framework — one small stylesheet,
  dark, clean. This is a trace, not a dashboard.

### CLI surface (`weaver`, from `src/weaver/cli.py`)
```
weaver init                                  # create DB + config
weaver seed-import --dir seed_resumes        # import ALL seed resumes → fact graph (LLM-assisted, deterministic fallback)
weaver lens list | lens show <name> | lens create --name X --lead-domains a,b --titles "FDE;FDC"
weaver tailor <source> --job <url|text>|--lens <name> --out <path> [--format md|docx] [--json]
weaver jobs add <url-or-text> | jobs list
weaver apply <resume-id> [--job <id>] [--cover <path>] [--dry-run]  # records an `applications` row ALWAYS; dry-run = status draft, no network
weaver apps list [--job <id>]               # the ledger from CLI (JSON/piped friendly)
weaver serve                                 # trace GUI (localhost)
weaver stats                                 # fact counts, verified %, lens count, application count
```
Global `--data-dir` option. All commands emit clean JSON when piped (agent-first, like gitvoice).

### Tailor engine (the heart)
1. Load facts (optionally filtered by lens's lead domains first).
2. Lens prompt: system prompt = lens spec + full fact graph (facts are ground truth, NEVER add
   experience not in facts; may re-word, re-order, re-group, emphasize/compress). User content =
   job posting text (if any).
3. LLM returns STRUCTURED JSON: {title, summary, experience:[{role, org, dates, bullets[]}],
   skills:[{domain, items[]}], education, awards}. Deterministic fallback (no LLM key): re-emit
   facts grouped by lens lead domains, title = lens target title. `--json` prints the structure.
4. Render: markdown (template) and docx (python-docx; styled like Leo's originals: name header,
   contact line, sections). Store `resumes` row + the fact ids used.
5. Guardrail enforced in code: generated resume's factual claims must trace to facts —
   post-check: LLM may not introduce orgs/dates/metrics not in the fact graph (regex-scan output
   for known facts; warn on unknown company names/dates, list them in `--json` output as `unverified_mentions`).

### LLM provider (pluggable)
`src/weaver/llm.py`: `complete_json(system, user) -> dict`. Providers: env `OPENAI_API_KEY` →
OpenAI-compatible chat completions (base_url + model from env `WEAVER_MODEL`, default
`gpt-5.6-luna`-style configurable; make base_url env-overridable for any OpenAI-compatible
endpoint). No key → `deterministic_fallback()` (never crashes; tests run without keys).

### Seed lenses (must ship — from Leo's real resumes in seed_resumes/)
- `fde` — Forward Deployed AI Engineer: lead agentic_engineering, ai_expertise, fullstack_engineering, sre_cloud
- `fdc` — Forward Deployed Creative: lead ai_expertise, cgi_motion, video_multimedia, direction_pm
- `design-engineer` — lead design_engineering, graphics_brand, fullstack_engineering, ai_expertise
- `multimedia` — lead video_multimedia, cgi_motion, graphics_brand, direction_pm
- `creative` — lead graphics_brand, cgi_motion, direction_pm
- `sre` — lead sre_cloud, fullstack_engineering, agentic_engineering

### Verification (required before reporting)
- `uv run weaver init && uv run weaver seed-import --dir seed_resumes` on the REAL seed files —
  fact graph builds without an LLM key (deterministic path).
- `uv run weaver tailor seed_resumes/Leo_Felix_Resume_FDE_26.docx --lens multimedia --format md`
  → produces a multimedia-flavored md; run the A/B eye-test: output should read like Leo's
  Full-Stack-Multimedia resume (same facts, production framing).
- `uv run weaver tailor ... --lens fde --json` → valid JSON with `unverified_mentions: []`.
- `uv run weaver apply <id> --dry-run` → prints Skyvern payload (workflow_type job_application,
  fields from resume + facts), no network call.
- pytest suite (write one): extraction, fallback path, docx render, guardrail scan, CLI smoke.
  Tests must pass with NO env keys.

## Environment notes
- Use `uv` — `uv sync` / `uv run` from the repo root.
  Python ≥3.11 via uv-managed interpreter.
- python-docx reads AND writes .docx (the seed files are .docx; the 2026 PDF is a master copy —
  pypdf for it).
- Do NOT create branding assets, logos, or marketing copy. Do NOT create a web UI.
- Keep it local: no Cloudflare, no deploy steps. D1/R2 port is a later phase.
- Read seed_resumes/*.docx to understand the style/voice — they are the quality bar.

## Phase 2+ vision — the full surface (roadmap, build order)

### IN — job search (velocity in)
- `weaver search --query "…" --location remote --sources all` — multi-source ingestion:
  ATS APIs first (Greenhouse / Lever / Ashby — no scraping), then browser-agent tail
  (LinkedIn Jobs, career pages via Skyvern/browser-use).
- Normalize into `jobs`: skills_required (LLM-extracted), posted_at, remote, location, source, dedup across sources.
- **Relevance scoring**: score = overlap(job skills, lens lead-domains + fact-graph skills) → auto-suggest best lens per job, rank daily.
- `weaver cron --schedule daily` — Hermes-cron driven: nightly search → score → auto-tailor top-K →
  **digest to Leo on Telegram** ("3 new jobs, 2 resumes ready — approve to apply") → reply applies.

### THROUGH — apply automation
- `weaver apply`: direct ATS API where available; Skyvern `job_application` workflow otherwise.
- Ledger rows for EVERY attempt (already specced) + Skyvern task polling (`weaver apps status`).
- **Guardrails**: per-domain rate limits, human approval gate for first-time companies, batch windows.

### OUT — follow-ups + the learning loop (hiring-likelihood max)
- `weaver followups` — status-aware follow-up drafts at 7/14/21 days (applied→nudge, rejected→silence);
  delivered via Hermes (Telegram digest or email).
- `weaver digest` — daily/weekly: applied, statuses, interview requests, follow-ups due.
- `weaver feedback add --application <id> --outcome callback|interview|rejected` → **conversion-weighted
  lenses**: learn which lens/format converts per domain; A/B every application (resume version recorded).
- `weaver ats-check --resume <id> --job <id>` — keyword-coverage % vs the posting (ATS optimization).
- `weaver gaps` — skills demanded by target-domain jobs but missing from the fact graph → learn-or-reframe list.
- `weaver prep <job-id>` — interview prep: company research + likely questions from the posting.

### The funnel (what the trace GUI shows end-to-end)
search → scored → tailored (resume+cover) → applied (ledger) → followed-up → outcome → lens learns.
Everything local, CLI-first, JSON-piped; GUI stays the read-only trace.

## Report back
Files created, what each command produces (paste the A/B tailor output summary), test results,
and any design deviations + why.
