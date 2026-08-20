# Running weaver from cron / launchd / gated agent harnesses

Agent harnesses (and cron) often can only execute ONE command, with a bare
environment, no shell features (`source`, `&&`, pipes), and no terminal
permissions. Weaver supports that shape natively:

## The one command

```
<repo>/.venv/bin/weaver cycle --count 5 --wide --json
```

- Absolute path to the console script — no `uv`, no PATH setup, no venv
  activation.
- `cycle` = find → add+fetch → adapt (docx by default) → batch fill as held
  tabs, in one process. `--force` waives the preflight gate; `--fit`,
  `--format`, org args as usual. Never submits — every fill parks `--hold`
  for a human.
- stdout is exactly ONE JSON document (progress and per-run blocks stream to
  stderr). Exit 0 = cycle done (skips are relayed, not failures); exit 2 =
  at least one fill failed; the JSON carries per-entry outcomes and reasons.

## Credentials with no shell

Put a `KEY=VALUE` file at `<data-dir>/env` (the data dir is gitignored):

```
WEAVER_API_KEY=sk-…
WEAVER_BASE_URL=https://…/v1
WEAVER_MODEL=…
```

It loads before every command; real environment variables always win; `export `
prefixes and quotes are tolerated so an existing shell env file can be copied
in unchanged. Keys never print.

## The browser needs the GUI session

Fills open tabs in the shared tab-host window, so the process must run inside
the logged-in GUI session:

- ✅ launchd **user agent** (`~/Library/LaunchAgents`, loaded while logged in)
- ✅ any terminal/harness running as the logged-in user
- ❌ system daemons, ssh-only crontabs with no Aqua session

If the window can't start, every fillable entry fails with the same
remediation string (nothing hangs), and skip-only cycles never touch a
browser. Pre-starting `weaver tab-host` once from a terminal also works.

### Workable is real-Chrome-only

Workable forms verify at submit (Cloudflare Turnstile), and the check fails
even a HUMAN clicking it inside the Playwright tab-host. Workable entries are
therefore routed to a real Chrome instance instead: weaver starts one on a
dedicated profile (`<data-dir>/real-chrome-profile`, CDP port 9223 /
`WEAVER_REAL_CDP_PORT`), fills there, and parks held as usual — the human
answers the verification and presses send. Needs Google Chrome installed
(`WEAVER_CHROME_BIN` overrides the binary); if it can't start, only the
workable entries fail, with the remediation in their reason string.

An already-open port is attached to rather than fought over, so whatever
Chrome happens to hold 9223 is what the fill drives. Weaver records the
instance it launched in `<data-dir>/real-chrome.pid` (pid, port, the
endpoint's `Browser` string) and checks the port against that record: a
browser it did not launch gets a warning on stderr naming the port and the
`WEAVER_REAL_CDP_PORT` override, and a run that then dies mid-fill (live test
#5: "Browser context management is not supported") repeats it in its error.
The fix is to quit the other browser, or give weaver a free port —
`WEAVER_REAL_CDP_PORT=9231` — so it launches its own dedicated-profile Chrome.

## Overlap safety

`batch` and `cycle` take a single-flight lock (`<data-dir>/batch.lock`).
A second invocation while one runs exits 2 with "already running (pid N)";
a lock whose pid is dead is treated as stale and replaced. Schedule ticks
freely — overlaps refuse instead of racing.

## Job-level dedupe

Re-runs never refill a posting that already has a `held`/`submitted` row —
by resume AND by job — so an aggressive schedule cannot double-apply.
