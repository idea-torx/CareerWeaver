# Setup — humans

This is the 5-minute path from a fresh clone to your first held application. Agent harnesses: see `docs/agents.md` and `docs/cron-and-harness-setup.md` — same binary, JSON mode, one-argv `cycle`.

## Install

**Python 3.11+** required.

```bash
git clone https://github.com/idea-torx/CareerWeaver.git
cd CareerWeaver

# uv (recommended)
uv sync
uv run playwright install chromium

# or pip
pip install -e .
playwright install chromium

uv run weaver --version
```

`uv` keeps everything in `.venv`; `pip install -e .` puts `weaver` on your PATH.

## Profile — blank template, nothing ships

```bash
weaver init --interactive
```

Press Enter to leave a field blank — edit `data/config.json` later. Fields include name, email, phone, location, links, target roles/skills, resume path, work authorization, availability/earliest start date, ATS-consent and "how did you hear about us" answers, and voluntary self-ID (empty = "I do not wish to disclose").

Prefer flags/env for scripting:

```bash
weaver init --email you@example.com --location "Portland, OR" \
            --links "linkedin.com/in/you,you.example" \
            --target-roles "Design Engineer,Full Stack Engineer" \
            --availability "Immediately"

WEAVER_PROFILE_EMAIL=you@example.com weaver init
```

Re-running `weaver init` never blanks a field you already set. `data/` is gitignored — your DB, config, and renders stay local.

## LLM provider — any OpenAI-compatible endpoint

```bash
export WEAVER_API_KEY=sk-...
# a different provider:
export WEAVER_BASE_URL=https://your-provider.example/v1
export WEAVER_MODEL=their-model-id
```

`WEAVER_BASE_URL` without `WEAVER_MODEL` is a config error (weaver tells you on stderr and in `weaver stats --json`, then falls back deterministically). **No key is fine** — every command degrades to deterministic local assembly.

Already have a model behind a CLI? Skip the key entirely — `WEAVER_LLM_CMD` runs
that argv and pipes the prompt through stdin (see `docs/agents.md`).

For headless/cron harnesses that can't run a shell, put `KEY=VALUE` lines in `data/env` instead (see `docs/agents.md`).

## Fact graph — import what you've already written

```bash
mkdir -p seed_resumes
# drop your .md/.txt/.docx/.pdf resumes in there — synthetic fixtures
# go in samples/ (mira@halloway.example, never your real details)

weaver seed-import --dir seed_resumes
weaver stats
weaver lens list
```

A fact is `verified` when ≥2 seeds agree on it. The graph is `data/weaver.db`.

## Tailor — your first resume

```bash
weaver tailor graph --lens fde --format docx      # explicit lens
weaver tailor graph --lens multimedia --format md --out ./out/resume.md
```

With a job saved, the lens is auto-picked to fit the posting (or pass `--lens` to override). Anything not backed by a fact is reported as `unverified_mentions`.

## Find → preflight → apply

```bash
weaver jobs add https://boards.example.com/acme/jobs/42 --fetch
weaver jobs list

weaver tailor graph --job 3           # one resume for that posting
weaver preflight 3                    # can the facts answer the form?
weaver apply 7 --dry-run              # print the request, submit nothing
weaver apply 7 --visible --hold --tab # fill, park for audit, you press send
weaver apps list
weaver serve  # http://127.0.0.1:8787 — read-only ledger
```

`--visible --hold --tab` is the safe default: one shared window, tabs survive individually, every run streams to `data/traces/*.jsonl`. Workable postings auto-route to a real Chrome (dedicated profile, CDP 9223) for Turnstile — you still answer the check and press send.

## Privacy

Weaver is local-first. No server, no account, no telemetry. The only outbound requests are your LLM provider, a posting URL you asked to `--fetch`, and the job site on a real `apply`. `data/` and `seed_resumes/` are gitignored; `samples/` ships only synthetic fixtures. Run without a key and it makes no LLM calls at all.

## Next steps

* `weaver lens create --name platform --lead-domains sre_cloud,fullstack_engineering --titles "Platform Engineer;Infrastructure Engineer"`
* `weaver batch 12 13 14` and `weaver cycle --count 5 --wide` for queues
* `docs/agents.md` for JSON, exit codes, and harness wiring
* `docs/cron-and-harness-setup.md` for `launchd` user agents and overlap locks

## Troubleshooting

* `job has no posting text — lens auto-pick would be a blind guess` → `weaver jobs add <url> --fetch` or `--lens <name>`.
* `preflight FAIL` → add the missing fact to `data/config.json` or `--force`.
* `resume file does not exist` → `weaver tailor graph --job <id> --format docx` again.
* `another fill cycle already running (pid N)` → wait or delete a stale `data/batch.lock`.
* `real Chrome did not open CDP port` → quit the foreign Chrome on 9223 or `WEAVER_REAL_CDP_PORT=9231`.

