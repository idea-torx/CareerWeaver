# Contract — options-upfront selects + decline containment (from Leo's Webflow live audit, run 81)

Evidence (data/weaver.db, application 81, Webflow Staff Brand Designer):

1. **Decline text in a free-text field.** n=8 `type f8 — typed 23 chars`:
   the model typed "I do not wish to answer" (23 chars) into "LinkedIn Profile",
   while `links` held the applicant's LinkedIn URL. `typed_text_allowed` passed it
   because decline phrases are in `SAFE_TYPED_TOKENS` regardless of the target
   field's shape. Nothing in code confines decline answers to choice widgets.
2. **Type-to-filter fails on Webflow's selects.** Rounds 9–24: every custom
   select reports "typed N chars (combo-box) — no option matched, search text
   left as typed". The widget does not filter on these tokens ("No options"
   rendered), the leftover search text stays in the box (Leo's screenshot:
   "Not"), and three model rounds burn on re-typing.
3. **Menu scoping misses portal-mounted menus.** n=29: the fixer's option scan
   returned page navigation ("Back to jobs, Apply, …") — `COMBO_OPTION_REFS_JS`
   / `_WIDGET_OPTIONS_JS` only look under aria-owns targets and the field's
   ancestor wrap; a menu rendered in a body-level portal is invisible to both,
   so even the correct "open then pick" path had garbage options.
4. **The model never sees a custom select's options.** Only native `<select>`
   fields carry `options=` in the prompt's field lines, so the model guesses
   phrasing ("Man", "Not a veteran") instead of choosing among real options.

Direction (Leo): inform the model of every dropdown's actual options UPFRONT so
it plans with a memory of what it should do, and keep the choice flexible —
the model picks the option; nothing form-specific is hardcoded. The engine's
deterministic decline backstop stays for the standard self-id five.

## Changes

**C1 — options upfront (driver `harvest_options` + loop merge).**
- `LocalDriver.harvest_options(target)`: native select → its options; combo →
  open (ArrowDown), settle, read the scoped menu texts, Escape closed. Commits
  nothing.
- `SNAPSHOT_JS` marks combo-like fields (`combo: true`: select__input class,
  role/aria combobox/haspopup/autocomplete, or inside `.select__control`).
- Loop, per round: after the snapshot, harvest options for unfilled combo
  fields not yet in a label-keyed cache; re-snapshot after harvesting (menus
  opened/closed churn b-refs); merge cached options into the state's fields so
  `field_line` shows `options=[...]` to the model. Harvest once per field per
  page, capped (12/round) to bound wall-time.
- Prompt: for a field listing options, `text` must be exactly one listed option
  label; voluntary self-id questions: declared value's option, else the listed
  option that declines; `links` holds the applicant's URLs — use the matching
  one for profile/URL questions; decline phrases never go into text fields; an
  optional text field with no supporting datum stays empty.

**C2 — portal-aware menu scoping (both JS scopers).**
When aria-owns/ancestor roots yield no options, fall back to the page's OPEN
menu: visible `[role="listbox"]` / `.select__menu` — and only when exactly one
is open (two open menus = ambiguous = no fallback). Applies to
`COMBO_OPTION_REFS_JS` (driver) and `_WIDGET_OPTIONS_JS` (agent), which then
treats it as a strict scope.

**C3 — enumerate-first combo fill, clean-on-miss (driver `type()`).**
Combo path becomes: open → read scoped options → match → REAL-click the option
(no typing at all). Only when the open menu shows nothing (async lists: country,
location) fall back to today's type-to-filter path. On a rendered-but-unmatched
menu: do NOT leave search text — clear it, press Escape, and report the real
option texts in the note so the model can choose next round. Same clean-up in
`_select_typed_option`'s no-match branch.

**C4 — decline answers are choice-only (loop guard).**
At the type guard: a decline-phrase `text` aimed at a field with no options, not
a select/combo/checkbox/radio, is refused before it reaches the page, with a
note that decline is for choice questions and optional text fields stay empty.
Counts toward the repeat limit like the PII refusal.

Out of scope (follow-up, separate concern): verify-line ref drift after uploads
re-render the DOM (n=8's verify checked f5 for a type that went to f8).

## Regression tests
- driver: non-filtering react-select (search always yields "No options") —
  `type()` still selects the wanted option via enumeration; unmatched wanted
  leaves the search box EMPTY and the note carries the real option texts.
- driver: portal-mounted listbox menu — scoped enumeration finds its options;
  selection lands in `.select__single-value`.
- driver: `harvest_options` returns the texts and leaves the menu closed with
  no value committed.
- agent: decline text into a labelled plain-text field is refused with the
  choice-only note; a combo field's harvested options appear as `options=` in
  the built user message.

Suite must stay green with no keys and no network: `uv run pytest`.

## Addendum — run 82 (live-audit findings, same evening)

The C1–C4 build fixed the type-to-filter stall but selections still "didn't
lock". Live DOM probes on the real form found three deeper causes, all fixed
and pinned by tests:

1. **Committed selects vanished from the snapshot.** react-select sets its
   search input to `opacity: 0` once a value lands (hidden-search state);
   `visible()` dropped the field, every later ref shifted by one, and the
   verify read the NEIGHBOR — so the loop re-filled fields it had already set.
   Fix: an opacity-0 input inside `.select__control` stays a field while its
   control is visible. Fixtures reproduce the hidden-search state.
2. **Stale ref stamps.** Re-stamps skipped invisible elements, so two inputs
   could answer to one ref (both "f17" — proven by a playwright strict-mode
   error). Fix: every snapshot wipes ALL `data-weaver-ref` stamps first.
3. **Mid-menu snapshots corrupted stamps; option scans were re-entrant.**
   Fix: options are read from the OPEN menu in place (`aria-controls` first,
   single visible listbox for portals), anchored by option DOM ids, listbox
   scope only (no more "Toggle flyout" pollution). `_scoped_options` and
   `COMBO_OPTION_REFS_JS` deleted.

Rule (Leo): **if it drops down, the pick comes from the submenu — no typing
in dropdowns.** A rendered menu is final: matched → clicked; unmatched → the
real options reported back. Keystrokes only for menus that render empty until
searched (country/location).

Bias (Leo): **prefer real answers over declining.** The prompt maps declared
data onto listed options and derives what data supports (age range from
date_of_birth, region from location); the decline option is only for questions
nothing declared answers. `listed_option_answer` widens the typed-text guard
to the target field's own option labels so derived answers ("25-34") pass.

Verification: live probe — select "Man" on the gender-identity widget, fresh
snapshot reads "Man" at a stable ref, field count 29→29, zero duplicate refs.

## Addendum 2 — the Ashby pressure round (Aug 15–16 2026, runs 83–90)

Three-ATS pressure test (Ramp/Ashby, Figma/Greenhouse, Linear/Ashby) drove the
remaining fixes, each with a regression test:

- **Question-bound answers.** Ashby's Yes/No pairs are `<button>`s defaulting
  to type=submit, its radios are opacity-0 inputs with `value="on"` — all
  anonymous or invisible to the old snapshot. Answer buttons and radio/checkbox
  fields now carry their question (`answers: "…"`), radio words come from their
  `<label>`, opacity-0 styled boxes stay fields, and the hold gate knows a
  question-bound button is an answer, not a submission.
- **Durable open answers.** The posting text rides into the fill context;
  composed essays pass the guard when anchored by several declared facts; no
  em dashes. `work_preference` is first-class applicant data so relocation/
  in-office/remote questions have an honest declared answer.
- **Durable evidence.** WEAVER_TRACE_FILE streams every trace entry to disk as
  it happens — two kills lost two full traces before this existed.
- **Operational**: `--hold` (fill, never submit, park for audit), tab mode
  (`WEAVER_CDP_URL` / `launch(cdp_url=…)` — concurrent runs as tabs of one
  window), job-named resume files (`Name_Resume_Title_Company.docx`), cover
  letters (`--cover`, section-labelled file inputs, resume never in the cover
  box), Ashby posting fetch via its public API, and a fully local stack
  (skyvern/cf_browser/keychain/worker deleted; payload.py absorbed the rest).

Final validation, run 90 (Ramp): 19 trace entries, zero failures — radio
clicked in batch two, both segmented pairs verified by read-back state, three
grounded essays, clean audit park.
