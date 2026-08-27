# Agent setup — driving CareerWeaver from any harness

Weaver is a **CLI-first, agent-driven** job-application engine. Every command is built so an LLM agent (Claude Code, Codex, OpenCode, Hermes, GLM, etc.) can run the full `find → tailor → apply` loop without a human in the loop — except for the final CAPTCHA/audit, which is intentionally human-gated.

This is the agent contract. The human-facing counterpart is `README.md` and `docs/setup.md`.

---

## 1. The contract

* **JSON when piped, human when TTY.** `weaver` detects `isatty()`; `--json` forces JSON. In JSON mode, `stdout` is exactly one JSON document. Human progress and per-run completion blocks stream to **stderr** so `| jq` and `> file` don't swallow them.
* **Never submit without a human.** `apply`/`batch`/`cycle` only fill; every fill parks at `held` (or `audit_pending` for one named field). A submitted row is terminal and never refilled.
* **Never invent a fact.** Anything typed must trace to `data/config.json` or a trivial declared answer (`Yes`/`No`/`I do not wish to disclose`). Untrusted page text never steers a typed value (PII guard).
* **A value is only "filled" when it has been verified to have actually landed** (re-read from the DOM, not logged optimistically). Combobox selections are the one `trust-the-selection` exception.
* **Deterministic fallback is always available.** No `WEAVER_API_KEY` → local rules assemble resumes, `llm.describe()` reports `deterministic`. Tests run this way; CI must not need a key.

---

## 2. Binary & environment

**Absolute path (for launchers that don't inherit PATH):**
```
<repo>/.venv/bin/weaver
```
`uv sync` creates `.venv`; `pip install -e .` also installs a `weaver` console script.

**Credentials with no shell** (cron / gated launchers that can only run one argv):

Put `KEY=VALUE` lines in `<data-dir>/env` (default `data/env`, gitignored):

```
WEAVER_API_KEY=sk-...
WEAVER_BASE_URL=https://your-provider.example/v1
WEAVER_MODEL=their-model-id
WEAVER_TEMPERATURE=0.3
WEAVER_TIMEOUT=60
```

**Or no endpoint at all** — point weaver at a local CLI and it never opens a
socket:

```
WEAVER_LLM_CMD=/path/to/some-cli --print --model their-model-id
```

`WEAVER_LLM_CMD` wins over every variable above. Weaver runs that argv verbatim
(adding no flags of its own), pipes `system + "\n\n---\n\n" + user` to stdin,
and parses the JSON object it finds on stdout. The CLI brings its own auth and
its own model flag, so no key and no base url are needed or consulted — put any
system-prompt or output-format flags the CLI needs into the argv itself.

* Real environment variables always win over the file.
* `export` prefixes and surrounding quotes are tolerated — copy an existing shell env file unchanged.
* The key **never prints**; `weaver stats --json` reports `llm.key_present` only.

**Any OpenAI-compatible endpoint:**

| Variable | Meaning |
|---|---|
| `WEAVER_API_KEY` / `OPENAI_API_KEY` | key |
| `WEAVER_BASE_URL` / `OPENAI_BASE_URL` | endpoint root (default `https://api.openai.com/v1`) |
| `WEAVER_MODEL` | model id — **required** whenever you change the base URL |
| `WEAVER_TEMPERATURE`, `WEAVER_TIMEOUT` | tuning |
| `WEAVER_USER_AGENT` / `WEAVER_HTTP_REFERER` / `WEAVER_APP_TITLE` | attribution headers (some relays 403 without them) |

`WEAVER_BASE_URL` without `WEAVER_MODEL` is a loud `config_error()` — reported on stderr, in `weaver stats --json`, and in every fallback reason — then degraded deterministically, never sent.

**Profile without flags:**

```
WEAVER_PROFILE_EMAIL=you@example.com
WEAVER_PROFILE_PHONE=+1 (503) 555-0148
WEAVER_PROFILE_LOCATION=Portland, OR
# ...any key in config.DEFAULT_PROFILE
```

Global override:
```
weaver --data-dir /path/to/data stats
```

---

## 3. The three lanes for agents

### Lane 1 — Find

```bash
weaver jobs add https://boards.example.com/acme/jobs/42 --fetch
weaver jobs add ./posting.txt
echo "$text" | weaver jobs add -
weaver jobs list --json | jq

weaver find --json
weaver find --wide --limit 10 --json
weaver find --wide --max-boards 60 --delay 1.2 --json
weaver boards list --json
weaver boards add acme lever   # writes <data-dir>/boards.json
```

* `jobs add <url>` is **offline by default** — no fetch unless `--fetch`. Ashby URLs *require* `--fetch` (resolved via the public Ashby API).
* `--wide` walks the committed registry of public Greenhouse/Lever/Ashby/Workable boards, one at a time, with a delay, capped at 60 boards per sweep. Postings already in `applications` are reported as `N already applied, skipped` and never re-surfaced.
* Tailor auto-lens and preflight are **blind** on a job with `< 200 chars` of `raw_text` — re-add with `--fetch` or pass `--lens` explicitly.
* Fit scorer vetoes titles in `config.find.exclude_titles` unless a `target_role` matches the title in full.

### Lane 2 — Adapt

```bash
weaver seed-import --dir seed_resumes --json
weaver seed-import --dir seed_resumes --no-llm --json
weaver stats --json
weaver lens list --json
weaver lens show fde --json

weaver tailor graph --lens fde --format docx --json
weaver tailor graph --job 3 --json
weaver tailor graph --job 3 --format pdf --out ./out/resume.pdf --json
weaver tailor ./seed_resumes/resume.docx --lens multimedia --json  # also imports the file if new
```

* `seed-import` reads `.md`/`.txt`/`.docx`/`.pdf`, merges into `facts` (kind `role|project|metric|tool|award|client|education|skill|domain`), marks `verified` when ≥2 seeds agree.
* `tailor` loads facts, picks a lens (explicit or auto-scored from the posting), calls `llm.complete_json()` with lens spec + fact graph + job text, and falls back deterministically if the LLM is unavailable or the config is incoherent.
* Render targets: `md`, `docx` (`python-docx`, styled like the originals), `pdf` (via `pypdf`-aware pipeline). Every render writes an `out/` artifact and a `resumes` row with `source_facts` (fact ids used, auditable).
* Guardrails (enforced in code, reported in JSON):
  * `unverified_mentions` — claims with no backing fact.
  * `avoid` — `profile.avoid` topics (e.g. `["Dreamcast"]`) are stripped from the prompt and refused if the model mentions them.
  * Compensation guard — dollar/salary patterns in essays are refused unless the field explicitly asks for them.

### Lane 3 — Apply

```bash
weaver preflight 3 --json
weaver apply 7 --dry-run --json
weaver apply 7 --visible --hold --tab --json
weaver apply 7 --visible --hold --tab --force --json   # waive preflight
weaver apps list --json
weaver apps show 12 --json
weaver batch 12 13 14 --json
weaver cycle --count 5 --wide --json
weaver tab-host --port 9222
weaver serve --port 8787
```

**Preflight gate (no browser):** fetches public form questions (Greenhouse/Lever/Ashby public endpoints; Workable enumerates every field with its required flag) and checks each required one against `applicant_from_profile()` facts. Verdict `pass` → browser-safe; `fail` → lists `missing: [{question, needs_fact}]`. `apply` (and `batch`/`cycle`) refuse before opening a browser unless `--force`.

**Engine:** Playwright Chromium on your machine — the only engine (Skyvern/Cloudflare adapters were removed Aug 2026).

| Flag | Effect |
|---|---|
| `--visible` | headed Chrome, watch it work; closing the window stops that run |
| `--hold` | **never submits** — parks at `held`/`audit_pending`, window stays open for audit |
| `--tab` | tabs of the shared window (`weaver tab-host`, auto-started) instead of a window per run |
| `--force` | waive the preflight gate |
| `--max-actions N` | cap the agent loop (default `local_agent.DEFAULT_MAX_ACTIONS`) |
| `--resume-url URL` | hosted resume override (otherwise uses the `resumes.path` artifact) |

Every real run:
* Verifies every typed value actually landed (re-read from the DOM).
* Streams its trace to `data/traces/<stamp>.jsonl` (survives kills).
* Writes an `applications` row **before** printing — `Ctrl-C`, exception, and held parks all land in the ledger.
* Prints its loud completion block to **stderr** (survives `| jq`).
* Pings `completion.notify()` once per `batch`/`cycle` (not per-run).

**Workable is real-Chrome-only.** Turnstile at submit fails even a human inside the Playwright tab-host. Workable entries are routed to a real Chrome on a dedicated profile (`<data-dir>/real-chrome-profile`, CDP `127.0.0.1:9223` / `WEAVER_REAL_CDP_PORT`, `WEAVER_CHROME_BIN` overrides the binary). Weaver records the launched instance in `<data-dir>/real-chrome.pid` and warns if a foreign browser holds the port. If the port is busy with a non-weaver Chrome, the run fails with `WEAVER_REAL_CDP_PORT` remediation rather than a mystery mid-fill error.

**Batch & cycle (orchestration, pure `cli.py`):**

`batch` (2+ resumes, one window):
1. `no such resume / no linked job URL` → skip
2. **Already terminal?** Any `held|submitted|applied` row for that resume *or that job* → skip (never refill a reviewed form, even via a re-adapted resume id)
3. **Preflight** (unless `--force`) → `fail` → skip with uncovered questions relayed
4. **Fill** as `--visible --hold --tab` with `force=True` (gate already ran), notifications off
5. Record outcome from the run's newest ledger row; **one cycle report** at the end groups `held` / `pending` (one field needs a human) / `skipped` (verbatim why) / `failed` (reason). Exit `0` when nothing failed (skips are expected), `2` when any run failed.

`cycle` = `find → add+fetch → adapt (docx) → batch as held tabs` in **one argv** for harnesses that can only execute a single command with a bare environment. `stdout` is one JSON doc; `stderr` carries the per-run blocks. `--count`, `--wide`, `--fit`, `--format`, `--force` as usual.

**Single-flight lock:** `batch`/`cycle` take `<data-dir>/batch.lock` (O_EXCL). A second invocation while one runs exits `2` with `already running (pid N)`; a dead-pid lock is treated as stale and replaced. Aggressive schedules don't race.

**Harness constraint — the browser needs the GUI session:**

* ✅ `launchd` user agent (`~/Library/LaunchAgents`, loaded while logged in)
* ✅ any terminal/harness running as the logged-in user
* ❌ system daemons, ssh-only crontabs with no Aqua session

If the window can't start, every fillable entry fails with the same remediation; skip-only cycles never touch a browser. Pre-start `weaver tab-host` from a terminal if needed.

See `docs/cron-and-harness-setup.md` and `docs/batch-cycle-contract.md` for the full contracts.

---

## 4. Output & exit codes

**Human vs JSON:**
```python
wants_json = args.json or not sys.stdout.isatty()  # force_human wins for batch inner runs
```

JSON payloads always include `ok`, and on failure `error`. `llm` shape: `{provider, model, base_url, key_present, config_error}`. Tailor JSON: `{resume_id, lens, source, job_id, format, path, provider, fallback_reason, source_facts, unverified_mentions, avoided, structure}`.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` (`EXIT_OK`) | success / `held`+`audit_pending` (human to finish) / `batch` with only skips+holds |
| `2` (`EXIT_FAILED`) | failed (LLM config incoherent, preflight refused without `--force`, browser couldn't start, at least one batch entry failed) |
| `3` (`EXIT_AUDIT_PENDING`) | apply parked at audit — one required field needs a human, then send |
| `2` (`EXIT_USAGE`) for bad argv (`no such lens`, unknown `--job` id) |

Real apply also returns `final_screenshot_b64` and a `completion_block` string (the stderr block, also stored in the ledger row's response).

---

## 5. The morning flow (what a `weaver morning` agent does)

```bash
weaver jobs add <url> --fetch --json
weaver tailor graph --job <id> --format docx --json
weaver preflight <job-id> --json
weaver apply <resume-id> --visible --hold --tab --json
weaver apps list --json
weaver serve   # human reads the ledger at :8787 while the tabs stay open
```

Wire `weaver cycle --count 5 --wide --json` from `launchd`/Hermes cron for the unattended variant: one JSON doc out, tabs ready to send when the user wakes.

---

## 6. Contributing as an agent

Read `AGENTS.md` before touching code — it encodes the safety rules:

1. **Never invent applicant facts.** Every typed value must trace to the user's record.
2. **Verify before trust.** No "OK" without DOM re-read evidence.
3. **Single-editor for `local_driver.py` / `local_agent.py`** — these are the minefield; small, single-concern patches only, with a regression test per fix.
4. **Never commit/push unless told to.** Local path is the product.
5. **`uv run pytest` must be fully green.** No red suites. Tests must pass with no keys, no network, on a fresh clone.
6. **Never commit PII.** `data/`, `seed_resumes/`, `config.json`, keys are local-only. Audit with `git ls-files | grep` for names/emails/paths.
7. **Cost-light.** Cheap model for breadth, expensive only for the single highest-leverage step. No hardcoded provider/account/user — everything overridable via env/config.
8. **Build → audit → test.** Written contract → single-concern patch + regression test → independent audit by a different model than the builder → green tests → real run.

Contracts live as `.md` working files **in the repo** so agents can read them without sandbox permission issues. Diagnose failures from direct evidence (`data/traces/*.jsonl`, `data/weaver.db` `applications`) before changing code.

---

## 7. Quick troubleshooting

* `WEAVER_BASE_URL is ... but WEAVER_MODEL is unset` → set `WEAVER_MODEL` for any non-OpenAI endpoint.
* `job #X has no posting text — the lens auto-pick would be a blind guess` → re-add with `--fetch`, or pass `--lens <name>` explicitly.
* `preflight FAIL — N required question(s) with no supporting fact` → add facts to `data/config.json` (`availability`, `how_did_you_hear`, etc.) or `--force`.
* `resume #X: the resume file ... does not exist` → re-render with `weaver tailor --job <id> --format docx`; the row always points at the last rendered artifact.
* `another fill cycle is already running (pid N)` → overlapping `batch`/`cycle`; wait or delete a stale `<data-dir>/batch.lock`.
* `real Chrome did not open CDP port 9223` / `Browser context management is not supported` → foreign Chrome holds the port; quit it or `WEAVER_REAL_CDP_PORT=9231`.
* `Browser ... needs the GUI session` → run inside a logged-in GUI session or pre-start `weaver tab-host`.

