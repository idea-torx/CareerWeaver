# CareerWeaver — Free, Open-Source AI-Apply Alternative

**The free, local CLI alternative to AI-Apply / LazyApply ($200-$400/mo). One canonical fact graph, six persona lenses, one CLI that turns them into tailored resumes — then fills the application form for you, without inventing a thing you didn't do.**

> **$0 · MIT · Local-first · Agent-driven** — No subscription. No hosted data. Your resume, fact graph, and every tailored doc stay on your machine. Bring your own LLM (any OpenAI-compatible endpoint) or run deterministic with no key.

**Free AI-Apply / LazyApply replacement · ATS auto-apply · Greenhouse / Lever / Workable / Ashby · Resume tailor · Cover letter**

[![CI](https://github.com/idea-torx/CareerWeaver/actions/workflows/ci.yml/badge.svg)](https://github.com/idea-torx/CareerWeaver/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](#install)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-334%20passing-brightgreen)](#development)
[![Local-first](https://img.shields.io/badge/local--first-no%20telemetry-orange)](#privacy)

Everything runs on your machine. Your resume, your profile, and your fact graph never leave it except to reach the LLM provider *you* configure — or the job site you explicitly apply to. No server, no account, no telemetry.

```bash
weaver init            # blank profile template — yours to fill
weaver seed-import     # your old resumes → a canonical fact graph
weaver tailor          # facts + a lens    → a tailored resume (md/docx/pdf)
weaver apply           # a resume + a job  → a filled form (human sends)
```

> **Agent-first.** Every command emits clean JSON when piped. Any agent — Claude Code, Codex, OpenCode, Hermes, GLM — can drive the full `find → tailor → apply` loop from the CLI. See [Agent Setup](#agent-setup) and [`docs/agents.md`](docs/agents.md).

---

## Why CareerWeaver — the free AI-Apply alternative

| | AI-Apply / LazyApply / Simplify / Teal / Huntr / JobCopilot / Sonara / LoopCV (hosted, $20-$400/mo) | **CareerWeaver** |
|---|---|---|
| **Price** | $200-$400/mo subscription | **$0 · MIT open-source** |
| **Where your data lives** | Their cloud (resume, PII, docs) | **Your machine only** (`data/` gitignored) |
| **Model** | Locked to their provider | **Bring your own** — any OpenAI-compatible endpoint, or deterministic with no key |
| **Resume tailoring** | Black-box rewrite | **Fact graph + persona lenses** — same facts, different emphasis, `unverified_mentions` never shipped |
| **Auto-apply** | Hosted browser, auto-submit | **Local Playwright Chrome** — verifies every field landed, parks at `held` for your audit, you press send |
| **ATS coverage** | Often limited / scraping | **Greenhouse + Lever + Ashby + Workable** (public APIs, no scraping) |
| **Agent / automation** | Web UI only | **CLI-first, JSON when piped** — any agent can drive `find → tailor → apply` |
| **Telemetry** | Vendor analytics | **Zero** — no server, no account, no telemetry |

* **One resume is wrong.** The same career reads differently for a Design Engineer vs. an SRE. Weaver keeps one verified fact graph and re-tells it through persona *lenses* — same facts, different emphasis.
* **LLMs are good at re-weighting, bad at truth.** The guardrail is non-negotiable: every claim in a generated resume must trace to a fact in your graph. Unbacked claims surface as `unverified_mentions`, not shipped.
* **Applying is the grind.** Weaver drives a real Chrome via Playwright, verifies every field actually landed, and parks at `held` for your audit — you clear CAPTCHA and press send.
* **Bring your own model.** Any OpenAI-compatible `/chat/completions` endpoint. No LLM key? Every command degrades to a deterministic local path — the test suite runs this way.

**Search keywords:** `ai-apply alternative, lazyapply alternative, simplify alternative, teal alternative, huntr alternative, jobcopilot alternative, sonara alternative, loopcv alternative, applyhero alternative, careerflow alternative, jobright alternative — free open-source job application automation, ATS auto-apply, Greenhouse Lever Workable Ashby Workday iCIMS Taleo BambooHR, resume tailoring, cover letter generator, autofill, auto-apply bot`**

---

## Install

Requires **Python 3.11+**.

```bash
git clone https://github.com/idea-torx/CareerWeaver.git
cd CareerWeaver

# with uv (recommended)
uv sync
uv run playwright install chromium   # apply engine

# or with pip
pip install -e .
playwright install chromium

uv run weaver --version      # or `weaver` once the venv is active
# every example below also works as `uv run weaver ...`
```

`pip install -e .` installs a `weaver` console script; `uv sync` keeps it inside `.venv`.

---

## 60-second quickstart

```bash
# 1. Profile (blank template, nothing ships with real details)
weaver init --interactive
# or non-interactively:
weaver init --email you@example.com --location "Portland, OR" \
            --links "linkedin.com/in/you,you.example" \
            --target-roles "Design Engineer,Full Stack Engineer"
# env-var alternative:
WEAVER_PROFILE_EMAIL=you@example.com weaver init

# 2. Fact graph — import every resume you've ever written
weaver seed-import --dir ./seed_resumes   # reads .md/.txt/.docx/.pdf
weaver stats                               # facts_total, verified %, lenses, jobs

# 3. Tailor — lens or job auto-picks the lens
weaver lens list
weaver tailor graph --lens fde --format docx
weaver tailor graph --job 3   # lens chosen to fit the posting

# 4. Apply — dry-run first, then fill held for audit
weaver preflight 3
weaver apply 7 --dry-run
weaver apply 7 --visible --hold --tab   # one shared window, trace survives kills
weaver apps list
weaver serve   # read-only ledger viewer at http://127.0.0.1:8787
```

Re-running `weaver init` never blanks a field you already set. `data/` (config, DB, traces, renders) is gitignored by construction.

---

## LLM provider

Weaver speaks **any OpenAI-compatible `/chat/completions` endpoint**. Nothing is baked in beyond a default pair you are free to replace.

| Variable | Meaning | Default |
|---|---|---|
| `WEAVER_API_KEY` (or `OPENAI_API_KEY`) | provider key | — |
| `WEAVER_BASE_URL` (or `OPENAI_BASE_URL`) | endpoint root | `https://api.openai.com/v1` |
| `WEAVER_MODEL` | model id — **required** when you change the base URL | `gpt-4o-mini` |
| `WEAVER_TEMPERATURE` | sampling temperature | `0.3` |
| `WEAVER_TIMEOUT` | seconds per request | `60` |
| `WEAVER_USER_AGENT` / `WEAVER_HTTP_REFERER` / `WEAVER_APP_TITLE` | attribution headers (some relays 403 without them) | `CareerWeaver/0.1` |

```bash
export WEAVER_API_KEY=sk-...
# a different provider — set both, or weaver refuses loudly:
export WEAVER_BASE_URL=https://your-provider.example/v1
export WEAVER_MODEL=their-model-id
```

`WEAVER_BASE_URL` without `WEAVER_MODEL` is a configuration error: weaver reports it on stderr, in `weaver stats --json` (`llm.config_error`), and in every fallback reason, then degrades to the deterministic path rather than shipping an OpenAI model id to someone else's endpoint.

**No key is a supported mode.** Every command falls back to deterministic local assembly — resumes still get written, just without LLM re-wording.

---

## Your profile

`weaver init` writes `data/config.json` — the only place your personal details live:

* Name, email, phone, location, links (LinkedIn/GitHub/portfolio)
* Target roles & skills (drives `find` scoring and lens ranking)
* Work authorization, visa, availability / earliest start date
* ATS-consent and "how did you hear about us" defaults
* Voluntary self-ID fields (empty = "I do not wish to disclose" — exactly what the agent answers)
* `avoid` — topics the model must never mention (e.g. an employer you don't want surfaced)

Prefer flags or env vars over prompts for scripting:

```bash
weaver init --phone "+1 (503) 555-0148" --availability "Immediately" \
            --how-did-you-hear "Company careers page"
```

`data/` is gitignored. Real resumes belong in `seed_resumes/` (also gitignored); `samples/` ships only synthetic fixtures (`mira@halloway.example`, `+1 (503) 555-01xx` — reserved `.example` names).

---

## The three lanes

### 1. Find — collect the postings

```bash
weaver jobs add https://boards.example.com/acme/jobs/42
weaver jobs add ./posting.txt            # file
pbpaste | weaver jobs add -              # stdin
weaver jobs add https://... --fetch      # also fetch the posting text now
weaver jobs list

weaver find                              # orgs in config.json find.orgs
weaver find --wide --limit 10            # sweep the whole board registry
weaver find --wide --max-boards 60 --delay 1.2
weaver boards list                       # what --wide sweeps
weaver boards add acme lever             # extend it (writes <data-dir>/boards.json)
```

`jobs add <url>` records the URL and **does not touch the network** unless `--fetch` is passed. `--wide` walks the committed registry of public Greenhouse/Lever/Ashby/Workable boards — product companies and creative agencies — one board at a time with a delay between requests, capped at 60 boards per sweep. Postings already in the ledger are reported as `N already applied, skipped`.

Ashby URLs require `--fetch` (resolved via the public Ashby API). Auto-lens, preflight, and fit scoring are blind on a job with no posting text.

### 2. Adapt — build the fact graph, then the resume

```bash
weaver seed-import --dir seed_resumes     # every resume you have ever written
weaver seed-import --dir seed_resumes --no-llm  # deterministic, no network
weaver stats                              # what the graph knows
weaver lens list
weaver lens show fde
weaver lens create --name platform \
  --lead-domains sre_cloud,fullstack_engineering \
  --titles "Platform Engineer;Infrastructure Engineer"

weaver tailor graph --lens fde --format docx
weaver tailor graph --lens multimedia --format md --out ./out/resume.md
weaver tailor graph --job 3               # picks the lens that fits the posting
weaver tailor graph --job 3 --json        # structured JSON + unverified_mentions
```

`seed-import` reads `.md`/`.txt`/`.docx`/`.pdf`, merges into one fact graph, and marks a fact **verified** when ≥2 seed documents agree on it. A lens re-tells those same facts for a different audience; it never adds a new one. Rendered output is diffed back against the graph — claims with no supporting fact surface as `unverified_mentions`.

A `do-not-mention` guard also runs: any fact whose topic is in `profile.avoid` is stripped from the prompt and refused if the model tries to mention it.

**Six shipped lenses** (from `src/weaver/lenses.py`):

| Lens | Leads | For |
|---|---|---|
| `fde` | `agentic_engineering`, `ai_expertise`, `fullstack_engineering`, `sre_cloud` | Forward Deployed AI Engineer |
| `fdc` | `ai_expertise`, `cgi_motion`, `video_multimedia`, `direction_pm` | Forward Deployed Creative |
| `design-engineer` | `design_engineering`, `graphics_brand`, `fullstack_engineering`, `ai_expertise` | Design Engineer |
| `multimedia` | `video_multimedia`, `cgi_motion`, `graphics_brand`, `direction_pm` | Multimedia Production |
| `creative` | `graphics_brand`, `cgi_motion`, `direction_pm` | Brand / Creative Director |
| `sre` | `sre_cloud`, `fullstack_engineering`, `agentic_engineering` | SRE / Platform |

### 3. Apply — fill the form

```bash
weaver preflight 3                        # can the facts answer this form? (no browser)
weaver apply 7 --dry-run                  # build + print the request, submit nothing
weaver apply 7 --visible --hold           # fill in a real browser, park for audit
weaver apply 7 --visible                  # fill AND submit, watched
weaver apply 7 --visible --hold --tab     # tab of the shared window (recommended)
weaver apps list | weaver apps show 12
```

**Preflight** fetches a posting's public questions (Greenhouse/Lever/Ashby/Workable expose them; Workable enumerates every field with its required flag) and checks each required one against your facts. If a required question has no supporting fact, the apply is refused *before* a browser opens — add the fact or pass `--force`.

**One engine, on your machine:** Playwright Chromium driven by the agent loop.

* `--visible` watches it work; closing the window stops that run.
* `--hold` fills everything but **never submits** — parks at `audit_pending`/`held` with the window open for you to review and press send.
* `--tab` opens concurrent runs as tabs of one shared window (`weaver tab-host`, auto-started) instead of a window per run; closing a tab ends only that run.
* Every real run streams its trace to `data/traces/*.jsonl` as it happens — a killed window never loses evidence.

**Workable is real-Chrome-only.** Turnstile at submit fails even a human inside the Playwright tab-host (Aug 2026). Workable fills auto-route to a real Chrome on a dedicated profile (CDP `127.0.0.1:9223` / `WEAVER_REAL_CDP_PORT`, `WEAVER_CHROME_BIN` overrides the binary). The hold semantics are identical — you answer Turnstile and press send. If Chrome can't start, only Workable entries fail, with remediation in their reason string.

**Batch & cycle:**

```bash
weaver batch 12 13 14                     # fill a queue as held tabs, one report at end
weaver cycle --count 5 --wide --json      # one-shot: find → adapt → batch (for cron)
weaver tab-host                           # pre-start the shared window
```

The agent stops and reports rather than guessing at a required question it has no fact for.

### The morning flow

```bash
weaver jobs add <url> --fetch             # paste the postings worth a shot
weaver tailor graph --job <id>            # a resume per posting, lens auto-picked
weaver preflight <id>                     # cheap gate, no browser
weaver apply <resume-id> --visible --hold --tab
weaver apps list                          # what went out, and what stopped
weaver serve                              # read-only viewer at :8787
```

---

## How it works

```
seed resumes ──► extract ──► fact graph (sqlite) ──► lens ──► render ──► .md / .docx / .pdf
                              ▲                      ▲                     │
                        corroboration           job posting                ▼
                        (2+ seeds = verified)                       preflight → apply → ledger
```

* **Fact graph** — `data/weaver.db` (`facts`, `skills`, `domains`, `lenses`, `jobs`, `resumes`, `applications`). Facts carry `kind`, `title`, `org`, `bullets`, `metrics`, `tags`, `source`, `verified`.
* **Lenses** — ordered `lead_domains`, `compress_domains`, `skills_order`, `summary_tone`. Same facts, different emphasis.
* **Guardrail** — output diffed back against the graph; `unverified_mentions` are reported. The apply agent refuses to type a value that didn't come from your profile (PII guard).
* **Ledger** — every dry-run and real apply lands in `applications` — `weaver apps list`, `weaver apps show <id>`, `weaver serve` (`/api/*` JSON endpoints).

---

## Agent setup

Weaver is built for agents. Every command emits **human text on a TTY, clean JSON when piped** (`--json` forces JSON).

### For any agent harness

```bash
# JSON mode (automatic when piped)
weaver stats --json | jq
weaver tailor graph --job 3 --json
weaver apply 7 --dry-run --json
weaver batch 12 13 --json          # one JSON doc on stdout, blocks on stderr
weaver cycle --count 5 --wide --json
```

* Credentials without a shell: put `KEY=VALUE` lines in `<data-dir>/env` (`data/env` by default, gitignored) — `WEAVER_API_KEY`, `WEAVER_BASE_URL`, `WEAVER_MODEL`, or `WEAVER_LLM_CMD` (a local CLI instead of an endpoint). Real env vars always win. `export` prefixes and quotes are tolerated.
* Absolute binary for launchers: `<repo>/.venv/bin/weaver`
* One argv, no shell: `cycle` is designed for harnesses that can only run a single command with a bare environment (cron, gated launchers) — no `source`, `&&`, or pipes required.
* `WEAVER_PROFILE_*` env vars set any profile field without a flag (e.g. `WEAVER_PROFILE_EMAIL`).
* Global `--data-dir` overrides where `weaver.db`/`config.json` live (default `./data`).
* `--visible --hold --tab` is the safe default for agents: never auto-submits, tabs share one window, traces survive kills.

### MCP — Claude, Codex, Hermes

`weaver-mcp` exposes the CLI over MCP stdio. It runs **on your machine**: nothing is
hosted, no account, and `weaver.db` never leaves the laptop. The fill step drives your
own Chrome with your own logged-in ATS sessions, which is why a hosted server could not
do this job.

Nine tools — `weaver_stats`, `weaver_lens_list`, `weaver_jobs_add`, `weaver_jobs_list`,
`weaver_preflight`, `weaver_tailor`, `weaver_apply_hold`, `weaver_apps_list`,
`weaver_apps_show`. **There is no submit tool.** `weaver_apply_hold` always passes
`--hold`, so it fills and parks at `audit_pending` for you to review and send.

**Claude Code** — via the plugin (bundles the server config):

```bash
claude plugin marketplace add idea-torx/CareerWeaver
```

```bash
claude plugin install careerweaver@careerweaver
```

Or wire the server directly:

```bash
claude mcp add careerweaver --env WEAVER_DATA_DIR=$PWD/data -- weaver-mcp
```

**Codex** — `~/.codex/config.toml`:

```toml
[mcp_servers.careerweaver]
command = "weaver-mcp"
env = { WEAVER_DATA_DIR = "/absolute/path/to/CareerWeaver/data" }
```

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "careerweaver": {
      "command": "weaver-mcp",
      "env": { "WEAVER_DATA_DIR": "/absolute/path/to/CareerWeaver/data" }
    }
  }
}
```

**Hermes** — `hermes mcp install careerweaver` once
[`packaging/hermes/careerweaver/manifest.yaml`](packaging/hermes/careerweaver/manifest.yaml)
is merged into its `optional-mcps/` catalog.

**Set `WEAVER_DATA_DIR` to an absolute path.** The CLI defaults to `./data` relative to
the working directory, and an MCP client spawns the server with whatever cwd it likes —
without it the server creates or reads an empty database wherever the client
happened to start it (or errors outright, if that directory is not writable).

Run `weaver init` and `weaver seed-import` before first use — an empty fact graph has
nothing to tailor from. If `weaver-mcp` is not on PATH, use the absolute path
`<repo>/.venv/bin/weaver-mcp`.

### Full guide

See **[`docs/agents.md`](docs/agents.md)** for the complete agent contract:
JSON vs human output, exit codes, `cycle` one-shot, `batch` orchestration, `preflight` gate, single-flight lock, Workable/real-Chrome routing, harness constraints (`launchd` user agent vs system daemon), and the build/audit/test contributor loop.

---

## Privacy

Weaver runs on your machine. There is no CareerWeaver server, no telemetry, and no account. The database, your config, and every generated resume live in `data/`, which is gitignored. The only outbound requests weaver ever makes are:

1. Your LLM provider (only if `WEAVER_API_KEY` is set)
2. A posting URL you explicitly asked it to `--fetch`
3. On a real, non-dry-run `apply` — the job site itself

Run with no key at all and it makes **zero** LLM calls. Tests are built from `samples/` (synthetic `*.example` fixtures); real seeds belong in `seed_resumes/` (gitignored).

---

## CLI reference

```
weaver [--data-dir DIR] [--json] [--version]
  init [--interactive] [--email E --phone P --location L --links A,B ...]
  seed-import --dir DIR [--no-llm] [--overwrite-profile]
  lens list | lens show <name> | lens create --name N --lead-domains a,b --titles "A;B"
  tailor [graph|PATH] --lens <name> | --job <id> [--format md|docx|pdf] [--out PATH] [--json] [--no-llm]
    alias: adapt
  jobs add <url|path|-> [--fetch] | jobs list
  find [--wide] [--limit N] [--max-boards N] [--delay S]
  boards list | boards add <name> <type>
  preflight <job-id>
  apply <resume-id> [--dry-run] [--visible] [--hold] [--tab] [--force] [--max-actions N] [--resume-url URL]
  batch <resume-id>... [--force] [--max-actions N] [--notify/--no-notify]
  cycle [--count N] [--wide] [--force] [--format FMT] [--max-actions N] [--json]
  tab-host [--port P]
  apps list | apps show <id>    (alias: ledger)
  serve [--port 8787]
  stats [--json]
```

`weaver apply` and `weaver batch`/`weaver cycle` stream their loud completion block to **stderr** so it survives `| jq` and `> file`. Exit codes: `0` success / `held`+`audit_pending` (human to finish), `2` failed, `3` audit-pending (needs one field).

---

## Development

```bash
uv run pytest            # full suite, no keys, no network
uv run pytest -m live    # opt-in network tests
```

* `samples/` is synthetic; `seed_resumes/` is private and must not be committed (see `.gitignore`).
* `AGENTS.md` encodes the build/audit/test loop and hard rules (no invented facts, verify-before-trust, keep `local_driver.py`/`local_agent.py` single-editor).
* `docs/batch-cycle-contract.md` and `docs/cron-and-harness-setup.md` document orchestration and harness constraints.

## Branding & scope

No web UI, no sales assets, no hosted deployment. Branding/PDF polish and D1/R2 port are later phases. Open-source from commit one — MIT.

## License

MIT — see [LICENSE](LICENSE).
