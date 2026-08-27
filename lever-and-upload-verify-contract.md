# Contract — Lever needs a real Chrome + `upload` must verify (from run 145, Metabase)

Evidence (data/traces/apply-20260824-164544.jsonl, application 145, resume 125,
`https://jobs.lever.co/metabase/b6ab96a1-5d3f-4e5b-a611-5a79d333d62f/apply`):

1. **The run died on a bot wall, not on form logic.** Operator report (2026-08-24): lever's bot detection is strong — it repeatedly
   CAPTCHA'd and failed; it is meant to be driven from a normal Chrome, and in
   the automation browser it flags as a bot. Same failure family as Workable's Turnstile-at-submit
   (2026-08-19 Bridgit run): the challenge re-issues forever inside the
   Playwright tab-host and clears from a real Chrome over CDP. `_needs_real_chrome`
   (cli.py:879) matches `apply.workable.com` only, so every Lever fill runs in
   the tab-host that Lever flags.
2. **The resume never landed, and the engine said it did.** n=2
   `upload f0 ok=true — "attached <applicant-resume>.docx"`.
   At the form: Lever had no resume. `LocalDriver.upload` (local_driver.py:1648)
   returns `{"ok": True}` the instant `frame.set_input_files(...)` does not raise —
   it never re-reads the input or the page. The `click` path 8 lines above it reads
   `aria-pressed` back "so a landed answer is VERIFIED, not assumed (hard rule 2)".
   `upload` is the one action in the file that skips the read-back the file's own
   comments demand.
3. **The false `ok` propagated.** `any_value_landed` (local_agent.py:2099) counts
   `ok` upload actions as evidence a value reached the form, and
   `milestone = "upload"` (local_agent.py:2923) captures a receipt on it. A fill
   with nothing attached can therefore park as legitimately-filled.
4. **Cost of the lie.** The upload was action 2 of an 8-action batch. Every later
   action in run 145 reasoned about a page state that did not exist; the run burned
   118s and 5 calls before stopping at the location autocomplete.

Direction: a bot-walled provider is a routing fact, not something the fill loop
should fight; and an attachment is a landed value like any other — subject to
rule 2, no exceptions. Neither patch hardcodes a form.

## Patch L — route Lever to the real Chrome host (cli.py only) — **LANDED 2026-08-24**

**L1.** `_needs_real_chrome(job_url)` matches a HOST SET, not one literal:

```python
#: Providers whose anti-bot wall fails inside the Playwright tab-host and
#: clears from a real Chrome over CDP. Workable: Turnstile at submit (Bridgit,
#: 2026-08-19). Lever: repeated CAPTCHA re-challenge on the apply page
#: (Metabase, 2026-08-24 — run 145 never attached a file).
_REAL_CHROME_HOSTS = ("apply.workable.com", "jobs.lever.co")

def _needs_real_chrome(job_url: str) -> bool:
    return any(h in (job_url or "") for h in _REAL_CHROME_HOSTS)
```

Overridable per AGENTS.md rule 7 — `WEAVER_REAL_CHROME_HOSTS` (comma-separated)
replaces the default tuple when set, so a fresh clone can add a provider without
a code change.

**L2.** Widen the operator-facing strings that say "workable" so a Lever failure
does not print Workable remediation: `_REAL_CHROME_REMEDY` (cli.py:856) becomes
"some forms (workable, lever) verify at submit and need a real Chrome…"; the
cycle comment at cli.py:1092 likewise.

**L3.** No change to `_ensure_real_chrome`, port handling, or the ownership
record — Lever reuses the Workable host as-is.

Scope: `_needs_real_chrome`, one module constant, two comment/message strings.
Does not touch the minefield files.

## Patch U — `upload` proves the attachment landed (local_driver.py only) — **LANDED 2026-08-24**

**U1.** After `set_input_files` succeeds, probe before returning. Two independent
signals, in order:

- **input-level** — the input's own `files[0].name` and `files.length`, read from
  the same element the file was set on.
- **page-level** — the file's basename rendered as visible text anywhere in the
  widget's subtree (walk up from the input to the nearest container, or the form
  when there is none). This is the signal Lever/Greenhouse/Ashby all produce: an
  ATS that accepted the file names it back.

**U2.** Three verdicts, and only one of them is `ok: True` unqualified:

| input `files[0]` | basename rendered | verdict |
|---|---|---|
| present | present | `{"ok": True, "verified": "rendered", "note": "attached X — page shows it"}` |
| present | absent | `{"ok": True, "verified": "input-only", "note": "attached X — the page has not confirmed it yet"}` |
| absent | — | `{"ok": False, "note": "attach did not stick: the input holds no file after set_input_files"}` |

The middle row is the run-145 state and must NOT read like the top row. It stays
`ok: True` (many ATSes render no chip, and a hard failure here would break working
forms), but it carries `verified: "input-only"` so downstream can tell them apart.

**U3.** Retry before settling for `input-only`: poll the page-level signal for up
to ~2.5s (the parse is async — Lever posts the file and re-renders). Only report
`input-only` after the poll expires. Do not extend the existing 3-attempt
`set_input_files` loop; this is a separate, cheaper wait.

**U4.** Cover-letter uploads use the identical verdict path — a wrong-document
attach is already guarded upstream by `is_cover`; this only proves landing.

## Patch V — the audit seam must see an unconfirmed attachment (local_agent.py only) — **LANDED 2026-08-24**

Land ONLY after U, and not in the same turn as another agent (rule 3).

**V1.** `any_value_landed` stops counting `verified == "input-only"` uploads as
evidence a value reached the form. A fill whose sole "landed value" is an
unconfirmed attachment is not a filled form.

**V2.** A form with a required file input that finished at `input-only` parks
`audit_pending`, not `held` — with the reason surfaced verbatim in the hold
report: `resume attached but <provider> never confirmed it — check the form`.
The operator reads that line and looks; today it had to be discovered by hand.

**V3.** The trace note for an `input-only` upload records the distinction, so a
post-mortem can tell "attached" from "attached, unconfirmed" without re-running.

## Tests (regression, one per defect)

- `test_cli.py::test_lever_urls_route_to_the_real_chrome_host` — a `jobs.lever.co`
  URL returns True from `_needs_real_chrome`; `apply.workable.com` still True; an
  unrelated `job-boards.greenhouse.io` URL False.
- `test_cli.py::test_real_chrome_hosts_are_env_overridable` — `WEAVER_REAL_CHROME_HOSTS`
  replaces the default set (fresh-clone / rule-7 guarantee).
- `test_local_driver.py::test_upload_that_does_not_stick_is_not_ok` — a fake frame
  whose `set_input_files` succeeds while the input reports zero files returns
  `ok False`. **This is run 145's exact lie; it must go red before the fix.**
- `test_local_driver.py::test_upload_confirmed_by_the_page_is_marked_rendered` —
  basename visible in the widget subtree → `verified == "rendered"`.
- `test_local_driver.py::test_upload_without_page_confirmation_is_input_only` —
  input holds the file, page never names it → `ok True`, `verified == "input-only"`.
- `test_local_agent.py::test_input_only_upload_is_not_a_landed_value` — V1.
- `test_local_agent.py::test_unconfirmed_required_upload_parks_audit_pending` — V2.

## Order and risk

L is independent of U/V and is the root cause of run 145. **L landed 2026-08-24**
(`_REAL_CHROME_HOSTS` + `_real_chrome_hosts()`, 3 regression tests in test_cli.py,
suite green at 422). Still to do: re-run Metabase and confirm the CAPTCHA clears
in a real Chrome — the routing is verified by test and by dry check, not yet by a
live fill. U is a
correctness fix worth landing regardless of L. V depends on U's `verified` key.

Open question: whether `jobs.lever.co` postings should also route to real
Chrome for `weaver find` fetches, or only for `apply`. This contract covers apply
only — find reads public JSON and has not been observed to flag.


## Landed — 2026-08-24

All three patches are in, plus `weaver apps set-status` for the ledger. Suite
green at 436 (was 419).

Two things the build surfaced that the contract had wrong:

- **U2's page-level probe first matched the filename STEM.** That made
  `cover.docx` "confirmed" by the page's own `Cover letter` label, and
  `resume.docx` by `Resume/CV` — a false success on essentially every form,
  i.e. the exact bug the patch exists to remove. Fixed to require the full
  filename with extension. Regression test:
  `test_a_filename_stem_matching_the_forms_own_label_is_not_confirmation`.
  Cost: an ATS that truncates the displayed name now reads `input-only` rather
  than `rendered`. Under-claiming is the safe direction.
- **The contract's stated reason for using `textContent` over `innerText` was
  wrong.** It claimed Chrome leaks the chosen filename into an ancestor's
  `innerText`; measured, it does not (both are empty). The real vector is the
  input's own `value`, which Chrome sets to `C:\fakepath\<name>` on every
  attach — so a probe reading element values, not ancestor text, is the one
  that would have reinstated the bug. Test rewritten around the real hazard.

`ATTACHED_JS` already existed and did the input-level check; it was simply never
called from `upload()`. The dead-code half of rule 2 was already written.

Ledger reconciled with the new subcommand: 142/143/144/146 -> submitted,
145 -> failed (lever bot wall). Each carries a `status_history` entry with the
reason; the run's original response is appended to, never overwritten.
