# weaver — near-autonomy path (Monday-ship v1)

Objective (Leo): Hermes finds jobs, adapts a resume per posting, and 99%-auto-fills
so Leo wakes up and just presses send (CAPTCHA click + audit). Full autonomy later.

Three lanes:

## Lane 1 — JOB FINDER  (build now, cheap arm)
Scan public ATS feeds for roles that fit Leo's profile + intent, rank by fit, emit a
daily shortlist. Sources (public, no auth):
  - Greenhouse: https://boards-api.greenhouse.io/v1/boards/{org}/jobs  (filter by
    group/updated/experience), content via .../jobs/{id}?questions=true
  - Lever: public API (boards-api.greenhouse.io-adjacent) — check 
  - Ashby / Workable / JazzHR / Greenhouse orgs (Webflow is Greenhouse: https://
    boards.greenhouse.io/webflow)
Fit scorer vs the saved profile (design roles: Webflow Staff Brand Designer; skills:
Webflow/Framer/design-systems/branding; seniority staff; location CA Remote/US Remote).
Output: ranked shortlist (role, org, url, fit%, why) -> jobs.json for Lane 3.
CLI: `weaver find` (per org or all), flags for MAX results, fit threshold.

## Lane 2 — RESUME ADAPTOR  (after Lane 1)
For each shortlisted posting, tailor the base resume (data/config.json profile +
resume source) to the role: extract the posting's required/qualification tokens,
align the summary + top-3 relevant bullets + skills ordering, output one resume file
per posting. Deterministic + verified (never invent facts — only re-order/re-word from
the real record). CLI: `weaver adapt --job <id>`.

## Lane 3 — AUTO-FILLER  (near-done)
The apply engine (done): takes the shortlisted job + adapted resume, fills the ATS
form ~99% in VISIBLE Chrome, pauses at a human seam (CAPTCHA / review). CLI:
`weaver apply --job <id> --visible`.

## WIRING — the morning batch
`weaver morning`: find -> adapt(top N) -> prefill each in visible Chrome -> Leo audits
+ presses send. This is the "wake up and press send" flow.

Build order: Lane 1 now -> Lane 2 -> wire `morning`. Lane 3 (fill) continues to fine
tune via GLM/Claude audits.
