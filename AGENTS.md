# AGENTS.md — how to work on weaver (for AI agents: Claude Code, OpenCode, GLM, Codex…)

Read this BEFORE touching the repo. It encodes the project's conventions, safety
rules, and the build/audit loop. Follow it even if a task prompt omits it.

## What this is
`weaver` — a local-first, MIT, open-source auto-apply bot. Agent-driven: it finds
jobs, adapts a resume per posting, and fills ATS application forms in a real
(non-headless) browser, pausing for a human to clear CAPTCHA / audit before send.
Fully local: the Skyvern and Cloudflare-worker adapters were removed (Aug 2026);
the Playwright chromium on this machine is the only engine. The engine decides
nothing about the applicant: every value is verified to have actually landed
before it's trusted.

## Hard rules (non-negotiable)
1. **NEVER invent or fabricate applicant facts.** Anything the bot types must trace
   to the user's real record (config.json) or a trivial yes/no/decline the user
   declared. Untrusted page text can never steer a typed value (PII guard).
2. **A value is only "filled" when it has been verified to have actually landed.**
   Trust-the-selection is allowed for combos, but every other field must be
   re-read and confirmed. No "OK" logs without evidence.
3. **Do NOT edit `src/weaver/local_driver.py` or `src/weaver/local_agent.py` in the
   same turn as another agent.** These files are the minefield — never two editors.
   Prefer small, precisely-specified, single-concern patches (+ regression tests).
4. **Never deploy / never commit** unless the task explicitly says so. Do NOT
   push. The local path is the product; there is no remote to deploy.
5. **`uv run pytest` MUST be fully green** before you finish. No red suites.
6. **Never commit PII or user data** — see .gitignore. `data/`, resumes, config.json,
   keys are local-only. A fresh clone must NOT reproduce any specific user's details
   (audit: run `git ls-files` and grep for real names/emails/paths/hardcoded ids).
7. **Cost-light by default.** Agents: prefer the cheap/fast model for breadth; use
   the expensive one only for the single highest-leverage step. Effort "medium".
   A fresh clone must work with ANY LLM/CLI provider via config — no hardcoded model,
   provider, account, or user in the code (everything overridable by env/config).

## The build · audit · test loop (this is the disciplined workflow)
- **Build**: implement from a written, precise contract (a .md working-file), single
  concern, with regression tests. Claude Code (Opus@medium) is the reference builder.
- **Audit**: a SECOND independent mind reviews the change read-only before it's
  trusted — GLM 5.3 via opencode is the cheap audit arm (the "fable alternative");
  Claude can audit too but costs more. Audits flag: correctness, false-success paths,
  wrong-widget clicks, ref/identity drift, dead code, unproven code paths.
- **Test**: `uv run pytest`. Add a regression test capturing the bug for every fix.
- Only after green + audit does the change get a real run.

## Architecture (near-autonomy path — see near-autonomy-path.md)
- Lane 1 `weaver find` — job finder: public ATS feeds (Greenhouse/Lever/Ashby), fit
  scorer vs the user's config profile, ranked shortlist > jobs.json.
- Lane 2 `weaver adapt` — per-posting resume tailoring from the real record (never
  invents; re-orders/re-words only).
- Lane 3 `weaver apply --visible` — the fill engine, visible Chrome, ~99% fill,
  pauses at a human seam (CAPTCHA/review).
- `weaver morning` — the wired daily flow: find → adapt(top N) → prefill → human send.

## Conventions
- Config is per-user (config.json, gitignored) + env overrides. The code MUST NOT
  hardcode a specific user (no names, emails, accounts, api keys, base URLs that
  assume OpenRouter/OpenAI/zen — everything provider-overridable).
- Contracts live as `.md` working files; put them IN the repo so agents can read them
  without sandbox permission issues (agents block `~/`/`/tmp` external reads).
- If a run fails: diagnose the root with direct evidence (DOM probes, the run's trace
  in data/weaver.db) BEFORE changing anything; send it to an audit mind; fix small.
- Playwright local driver; `--local` path is default; do not switch the core to a
  hosted browser service without good reason.
- Visible runs: ALWAYS `weaver apply <id> --visible --hold --tab` unless told to
  submit — tabs share one window (tab-host, auto-started), --hold parks at the
  audit seam. Traces stream to data/traces/*.jsonl (survive kills); read them
  before diagnosing.
- Shortlisting: jobs must be added with `--fetch` (Ashby resolves via its public
  API); tailor REFUSES auto-lens on a job with no posting text. The fit scorer
  vetoes titles in config find.exclude_titles (developer roles for a designer
  profile) unless a target role matches the title in full — keep target_roles
  honest about qualification, they are matching fuel.
