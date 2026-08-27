"""Step-by-step application forms: the engine walks a state graph, not one page.

`tests/test_local_agent.py` drives the loop over a SINGLE page. Everything here
drives it over a SEQUENCE of pages, because that is where the engine used to
fail: a wizard's first step shows two questions and a `Continue`, the model reads
it as "there is no application form here", and the run parked with the actual
form never seen.

Every driver below re-stamps its refs per page (`f0`/`b0` on every step) exactly
the way the real snapshot does — that positional drift is the thing a stepwise
engine has to survive, so no fixture is allowed to paper over it.

Grew out of the scratch probes in `.weaver-scratch/` (fixtures F1-F10); each
one that came back RED is a test here.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from weaver import local_agent

APPLICANT = {
    "first_name": "Mira",
    "last_name": "Halloway",
    "full_name": "Mira Halloway",
    "email": "mira@halloway.example",
    "phone": "+1 (503) 555-0148",
    "how_did_you_hear": "LinkedIn",
    "authorized_to_work": "Yes",
}
JOB = {"title": "Staff Designer", "company": "Verdant Systems"}


# ------------------------------------------------------------------ the fixture


def field(ref: str, label: str, **overrides: Any) -> dict[str, Any]:
    return {
        "ref": ref,
        "tag": "input",
        "type": "text",
        "label": label,
        "name": label.lower().replace(" ", "_"),
        "id": "",
        "placeholder": "",
        "value": "",
        "required": False,
        "disabled": False,
        **overrides,
    }


class Step:
    """One page state of a step-by-step application.

    `forward` is the ref of the control that advances to the next step; None
    means this step has no path forward (the review page, or a `Continue` wired
    to nothing — the navigation-loop fixture).
    """

    def __init__(
        self,
        url: str,
        fields: list[dict[str, Any]] | None = None,
        buttons: list[dict[str, Any]] | None = None,
        forward: str | None = None,
        text: str = "Application form",
        rerender_on_type: bool = False,
    ) -> None:
        self.url = url
        self.fields = [dict(f) for f in (fields or [])]
        self.buttons = [dict(b) for b in (buttons or [])]
        self.forward = forward
        self.text = text
        #: React-style re-render: every write re-orders the DOM and the next
        #: snapshot re-stamps the refs, so `f0` names a different question.
        self.rerender_on_type = rerender_on_type


class WizardDriver:
    """A multi-step form: N page states, at most one forward control each."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps = steps
        self.index = 0
        #: "<step index>:<ref>" for every click, in order.
        self.clicks: list[str] = []
        #: (page url, ref, text) for every value that reached the page.
        self.typed: list[tuple[str, str, str]] = []
        self.snapshots = 0

    @property
    def step(self) -> Step:
        return self.steps[self.index]

    def values_on(self, url_suffix: str) -> dict[str, str]:
        """What actually landed on a step, keyed by label."""
        for step in self.steps:
            if step.url.endswith(url_suffix):
                return {f["label"]: str(f.get("value") or "") for f in step.fields}
        raise AssertionError(f"no step ends with {url_suffix!r}")

    # -- reads
    def snapshot(self) -> dict[str, Any]:
        self.snapshots += 1
        step = self.step
        return copy.deepcopy(
            {
                "url": step.url,
                "title": "Apply — Verdant Systems",
                "fields": step.fields,
                "buttons": step.buttons,
                "text": step.text,
                "confirmation": {"detected": False, "matched": [], "snippet": ""},
            }
        )

    def confirm_text(self) -> str:
        return ""

    def screenshot(self) -> str:
        return "ZmFrZS1qcGVn"

    # -- writes
    def type(self, target: str, text: str) -> dict[str, Any]:
        step = self.step
        target_field = next((f for f in step.fields if f["ref"] == target), None)
        if target_field is None:
            return {"ok": False, "note": f"no element for {target}"}
        target_field["value"] = text
        self.typed.append((step.url, target, text))
        if step.rerender_on_type:
            self._rerender(step)
        return {"ok": True, "note": f"typed {len(text)} chars", "value": text}

    def click(self, target: str) -> dict[str, Any]:
        step = self.step
        self.clicks.append(f"{self.index}:{target}")
        target_field = next((f for f in step.fields if f["ref"] == target), None)
        if target_field is not None:
            if target_field.get("type") in ("checkbox", "radio"):
                target_field["value"] = "false" if target_field.get("value") == "true" else "true"
            return {"ok": True, "note": f'clicked "{target_field["label"]}"'}
        button = next((b for b in step.buttons if b["ref"] == target), None)
        if button is None:
            return {"ok": False, "note": f"no element for {target}"}
        if step.forward and target == step.forward and self.index + 1 < len(self.steps):
            self.index += 1
        return {"ok": True, "note": f'clicked "{button["text"]}"'}

    def upload(self, target: str) -> dict[str, Any]:
        return {"ok": True, "note": "attached resume.pdf"}

    @staticmethod
    def _rerender(step: Step) -> None:
        """Re-order the fields and re-stamp the refs, like a real re-render."""
        step.fields.reverse()
        for i, item in enumerate(step.fields):
            item["ref"] = f"f{i}"


def scripted(replies: list[Any]) -> Callable[[str, str], Any]:
    """A model that plays a fixed script; the last reply repeats if it runs out."""
    calls: list[dict[str, str]] = []

    def chat(system: str, user: str) -> Any:
        calls.append({"system": system, "user": user})
        return replies[min(len(calls) - 1, len(replies) - 1)]

    chat.calls = calls  # type: ignore[attr-defined]
    return chat


def run(driver: WizardDriver, chat: Any, **kwargs: Any) -> dict[str, Any]:
    return local_agent.run_apply(
        driver,
        chat,
        applicant=APPLICANT,
        job=JOB,
        has_resume=True,
        sleep=lambda _ms: None,
        **kwargs,
    )


def notes(result: dict[str, Any], action: str = "") -> str:
    """Every trace note, joined — what the run says it did."""
    return " | ".join(
        str(e["note"]) for e in result["trace"] if not action or e["action"] == action
    )


CONTINUE = {"ref": "b0", "text": "Continue", "type": "button"}
NEXT = {"ref": "b0", "text": "Next", "type": "button"}
SUBMIT = {"ref": "b0", "text": "Submit application", "type": "submit"}
STOP_HERE = {"actions": [{"action": "stop", "text": "held for audit"}]}


# ------------------------------------------- (1) a sparse first page is not the end


def test_a_sparse_first_page_advances_instead_of_being_skipped() -> None:
    """Step 1 exposes one optional question and a `Continue`. Nothing else.

    The live Pinterest/Dropbox traces all died here: the model reads the sparse
    page, returns `stop` ("there is no application form here"), and the run
    parked with the application never seen. A model `stop` is a SUGGESTION —
    while nothing required is unresolved and a forward control exists, the
    ENGINE takes it.
    """
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/step1",
                [field("f0", "How did you hear about us?", tag="select", type="select",
                       options=["LinkedIn", "Referral", "Other"])],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/step2",
                [field("f0", "First name", required=True),
                 field("f1", "Email address", type="email", required=True)],
                [NEXT],
                forward="b0",
            ),
            Step("https://apply.example.test/review", [], [SUBMIT]),
        ]
    )
    chat = scripted(
        [
            # the sparse page: the model sees nothing worth doing
            {"actions": [{"action": "stop", "text": "this page only asks how I heard "
                                                    "about the role; there is no form here"}]},
            # step 2 — now it has something to fill
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "type", "target": "f1", "text": "mira@halloway.example"}]},
            {"actions": [{"action": "click", "target": "b0"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.index == 2, "the engine never left the sparse first page"
    assert driver.values_on("step2") == {
        "First name": "Mira",
        "Email address": "mira@halloway.example",
    }
    assert result["status"] == local_agent.AUDIT_PENDING  # the hold seam, not a skip
    assert "step advanced" in notes(result)
    assert driver.clicks[0] == "0:b0", "the forward control the engine took"


def test_the_engine_will_not_walk_forward_over_an_unfilled_required_field() -> None:
    """The other half of the rule: a sparse page advances, an UNFINISHED one
    does not. Step 1 has a required field the model left empty — the engine
    nudges instead of clicking `Continue` past it."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/one",
                [field("f0", "First name", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step("https://apply.example.test/two", [], [SUBMIT]),
        ]
    )
    chat = scripted([{"actions": [{"action": "stop", "text": "nothing to do here"}]}])
    result = run(driver, chat, hold=True)

    assert driver.index == 0, "the engine walked past an empty required field"
    assert driver.clicks == []
    assert "stop ignored" in notes(result)
    assert result["status"] == local_agent.AUDIT_PENDING


# ------------------------- (2) page-local fill + verified forward transitions


def test_page_local_fill_then_a_verified_transition_on_every_step() -> None:
    """The happy path the brief describes: fill what is on THIS page, verify it
    landed, take the forward control, repeat — and park at the human seam."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/a",
                [field("f0", "First name", required=True), field("f1", "Last name", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/b",
                [field("f0", "Email address", type="email", required=True),
                 field("f1", "Phone number", type="tel")],
                [NEXT],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/c",
                [field("f0", "How did you hear about us?", tag="select", type="select",
                       options=["LinkedIn", "Referral", "Other"])],
                [{"ref": "b0", "text": "Save and continue", "type": "submit"}],
                forward="b0",
            ),
            Step("https://apply.example.test/review", [], [{"ref": "b0", "text": "Apply", "type": "submit"}]),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "type", "target": "f1", "text": "Halloway"}]},
            {"actions": [{"action": "click", "target": "b0"}]},
            {"actions": [{"action": "type", "target": "f0", "text": "mira@halloway.example"},
                         {"action": "type", "target": "f1", "text": "+1 (503) 555-0148"}]},
            {"actions": [{"action": "click", "target": "b0"}]},
            {"actions": [{"action": "type", "target": "f0", "text": "LinkedIn"}]},
            {"actions": [{"action": "click", "target": "b0"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.index == 3, "the run did not reach the review step"
    assert driver.values_on("/a") == {"First name": "Mira", "Last name": "Halloway"}
    assert driver.values_on("/b") == {
        "Email address": "mira@halloway.example",
        "Phone number": "+1 (503) 555-0148",
    }
    assert driver.values_on("/c") == {"How did you hear about us?": "LinkedIn"}
    # three forward transitions, each one observed rather than assumed
    assert notes(result).count("step advanced") == 3
    # and the human seam still holds: nothing was submitted
    assert result["status"] == local_agent.AUDIT_PENDING
    assert result["confirmation_text"] == ""
    assert "3:b0" not in driver.clicks, "the review page's Apply was clicked"


def test_a_save_and_continue_that_is_a_real_submit_button_still_advances() -> None:
    """Greenhouse/Lever put the step's forward control in the step's own <form>,
    so it is a literal `<button type=submit>`. Under `--hold` the submit gate
    used to eat it and park on page 1 claiming "form filled" (probe F5)."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/s1",
                [field("f0", "First name", required=True)],
                [{"ref": "b0", "text": "Continue", "type": "submit"}],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/s2",
                [field("f0", "Email address", type="email", required=True)],
                [SUBMIT],
            ),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "click", "target": "b0"}]},
            {"actions": [{"action": "type", "target": "f0", "text": "mira@halloway.example"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.index == 1, "the hold gate blocked the step's forward control"
    assert "submit blocked" not in str(result.get("reason") or "")
    assert driver.values_on("s1")["First name"] == "Mira"
    assert driver.values_on("s2")["Email address"] == "mira@halloway.example"


# --------------------------------------- (3) positional refs are not identity


def test_a_re_render_that_reshuffles_refs_does_not_misattribute_a_value() -> None:
    """The page re-orders its fields on every write, so `f0` names a different
    question by verification time. The value must be re-located by its stable
    LABEL — a ref lookup would report the name as an email miss and "repair" it."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/churn",
                [field("f0", "Full name", required=True),
                 field("f1", "Email address", type="email", required=True)],
                [CONTINUE],
                forward="b0",
                rerender_on_type=True,
            ),
            Step("https://apply.example.test/done", [], [SUBMIT]),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira Halloway"},
                         {"action": "type", "target": "f1", "text": "mira@halloway.example"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.values_on("churn") == {
        "Full name": "Mira Halloway",
        "Email address": "mira@halloway.example",
    }
    assert "value mismatch" not in notes(result)
    assert "email field must read" not in notes(result)


def test_a_question_that_the_re_render_removes_is_dropped_not_written_over() -> None:
    """A re-render does not only re-order — it can RETIRE a question. The action
    planned for it must be dropped: whatever inherited that ref is a different
    question, and typing into it is the same misattribution by another route."""

    class VanishingDriver(WizardDriver):
        """Answering the name retires the optional follow-up and re-stamps refs."""

        def type(self, target: str, text: str) -> dict[str, Any]:
            result = super().type(target, text)
            step = self.step
            if result.get("ok") and any(f["label"] == "Preferred name" for f in step.fields):
                step.fields = [f for f in step.fields if f["label"] != "Preferred name"]
                for i, item in enumerate(step.fields):
                    item["ref"] = f"f{i}"
            return result

    driver = VanishingDriver(
        [
            Step(
                "https://apply.example.test/vanish",
                [field("f0", "Full name", required=True),
                 field("f1", "Preferred name"),
                 field("f2", "Email address", type="email", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step("https://apply.example.test/after", [], [SUBMIT]),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira Halloway"},
                         {"action": "type", "target": "f1", "text": "Mira"},
                         {"action": "type", "target": "f2", "text": "mira@halloway.example"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.values_on("vanish") == {
        "Full name": "Mira Halloway",
        "Email address": "mira@halloway.example",
    }
    assert "no longer on the page" in notes(result)
    assert ("https://apply.example.test/vanish", "f0", "Mira") not in driver.typed


def test_a_write_that_navigates_mid_batch_does_not_carry_the_rest_into_the_new_step() -> None:
    """The first write jumps the wizard forward (a magic-link style email box).
    The values planned for the old step must not be typed into the new one, and
    the ones already landed there cannot be re-read — the run says so instead of
    verifying them against whatever field inherited the ref."""

    class JumpOnTypeDriver(WizardDriver):
        def type(self, target: str, text: str) -> dict[str, Any]:
            result = super().type(target, text)
            if result.get("ok") and self.index + 1 < len(self.steps):
                self.index += 1
            return result

    driver = JumpOnTypeDriver(
        [
            Step(
                "https://apply.example.test/j1",
                [field("f0", "Email address", type="email", required=True),
                 field("f1", "Phone number", type="tel")],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/j2",
                [field("f0", "Full name", required=True),
                 field("f1", "Why do you want this role?")],
                [NEXT],
                forward="b0",
            ),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "mira@halloway.example"},
                         {"action": "type", "target": "f1", "text": "+1 (503) 555-0148"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.values_on("j1")["Email address"] == "mira@halloway.example"
    assert driver.values_on("j2") == {"Full name": "", "Why do you want this role?": ""}
    assert [t for t in driver.typed if t[0].endswith("j2")] == []
    assert "cannot be re-read" in notes(result)


def test_a_transition_never_lets_the_email_repair_write_into_the_next_step() -> None:
    """Step 1 asks for the email; step 2's FIRST field is a name (probe F9).

    Verified after the transition, the label lookup misses, the ref fallback
    lands on step 2's name box, the "@" branch fires, and the engine types the
    applicant's address into a stranger's field. The batch is verified against
    the step it was typed on, so none of that happens.
    """
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/e1",
                [field("f0", "Email address", type="email", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/e2",
                [field("f0", "Full name", required=True), field("f1", "Phone number", type="tel")],
                [NEXT],
                forward="b0",
            ),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "mira@halloway.example"},
                         {"action": "click", "target": "b0"}]},
            STOP_HERE,
        ]
    )
    run(driver, chat, hold=True)

    assert driver.values_on("e1")["Email address"] == "mira@halloway.example"
    assert driver.values_on("e2")["Full name"] == "", "the email landed in the next step's name field"
    assert [t for t in driver.typed if t[0].endswith("e2")] == []


def test_a_transition_never_lets_the_verifier_tick_the_next_steps_consent_box() -> None:
    """Same drift, worse consequence (probe F10): step 2's first field is an
    "I consent…" checkbox, and the verifier's checkbox branch CLICKS. A consent
    the applicant never gave must never be ticked by a ref collision."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/c1",
                [field("f0", "How did you hear about us?", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/c2",
                [field("f0", "I consent to the processing of my personal data",
                       type="checkbox", value="false", required=True)],
                [SUBMIT],
            ),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "LinkedIn"},
                         {"action": "click", "target": "b0"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.values_on("c2")["I consent to the processing of my personal data"] == "false"
    assert [c for c in driver.clicks if c.startswith("1:")] == []
    assert result["status"] == local_agent.AUDIT_PENDING


# ------------------------------------------ (4) a real fact gap on a later step


def test_a_required_fact_gap_on_a_later_step_parks_naming_the_page_and_question() -> None:
    """Advancing must not turn into guessing. A required question on step 2 with
    no supporting applicant datum parks at `audit_pending` naming BOTH the exact
    question and the page it blocked on — and types nothing."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/g1",
                [field("f0", "First name", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/g2",
                [field("f0", "What is your employee referral code?", required=True)],
                [NEXT],
                forward="b0",
            ),
            Step("https://apply.example.test/g3", [], [SUBMIT]),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "click", "target": "b0"}]},
            {"actions": [{"action": "stop", "text": "no referral code in the applicant data"}]},
        ]
    )
    result = run(driver, chat, hold=True)
    audit = result["audit"] or {}
    evidence = f"{result['reason']} {audit.get('note')} {audit.get('label')}".lower()

    assert result["status"] == local_agent.AUDIT_PENDING
    assert "referral code" in evidence, "the park does not name the blocking question"
    assert "g2" in str(audit.get("url")), "the park does not name the page it blocked on"
    assert "g2" in str(result["reason"])
    assert driver.index == 1, "the engine walked past the question it could not answer"
    assert [t for t in driver.typed if t[0].endswith("g2")] == [], "a value was invented"
    assert result["status"] != "applied"


# -------------------------------- (5) a repeated fingerprint parks, never loops


def test_a_forward_control_that_changes_nothing_parks_as_a_navigation_loop() -> None:
    """`Continue` is wired to nothing: the page fingerprint comes back identical.

    One bounded settle/re-observe retry, then a park whose reason names
    NAVIGATION and the control — not the generic "the model repeated an action"
    field escalation, which sent a human looking at the wrong thing.
    """
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/stuck",
                [field("f0", "First name", required=True)],
                [CONTINUE],
                forward=None,  # the click resolves ok and goes nowhere
            )
        ]
    )
    chat = scripted([{"actions": [{"action": "click", "target": "b0"}]}])
    result = run(driver, chat, hold=True)
    audit = result["audit"] or {}

    assert result["status"] == local_agent.AUDIT_PENDING
    assert audit.get("kind") == "navigation"
    assert "navigation loop" in str(result["reason"])
    assert "Continue" in str(result["reason"])
    assert "stuck" in str(result["reason"])
    assert len(driver.clicks) == local_agent.NAV_STALL_LIMIT, "the loop was not bounded"


def test_an_engine_driven_forward_that_stalls_parks_too() -> None:
    """The same guard on the engine's OWN forward advance: a model that keeps
    stopping on a page whose `Continue` does nothing cannot spin the run."""
    driver = WizardDriver(
        [
            Step("https://apply.example.test/spin", [field("f0", "Nickname")], [CONTINUE], forward=None)
        ]
    )
    chat = scripted([{"actions": [{"action": "stop", "text": "nothing here for me"}]}])
    result = run(driver, chat, hold=True)

    assert result["status"] == local_agent.AUDIT_PENDING
    assert (result["audit"] or {}).get("kind") == "navigation"
    assert len(driver.clicks) == local_agent.NAV_STALL_LIMIT
    assert result["actions_used"] < local_agent.DEFAULT_MAX_ACTIONS, "the run burned its budget spinning"


# ------------------- (6) answer buttons vs Continue/Next vs the final Apply


def test_answer_buttons_forward_controls_and_the_final_submit_are_distinguished() -> None:
    """Three kinds of button live on a wizard step and none may be confused:
    a question-bound ANSWER button, the step's FORWARD control, and the final
    submit. Ashby's Yes/No answer buttons are `type=submit` inside the form."""
    state = {
        "url": "https://apply.example.test/mix",
        "fields": [],
        "buttons": [
            {"ref": "b0", "text": "Yes", "type": "submit",
             "question": "Are you authorized to work in the US?"},
            {"ref": "b1", "text": "No", "type": "submit",
             "question": "Are you authorized to work in the US?"},
            {"ref": "b2", "text": "Continue", "type": "submit"},
            {"ref": "b3", "text": "Back", "type": "button"},
            {"ref": "b4", "text": "Continue with Google", "type": "button"},
            {"ref": "b5", "text": "Submit application", "type": "submit"},
            {"ref": "b6", "text": "Next steps", "type": "a"},
        ],
    }

    # the step's forward control: exactly one, and it is not an answer or a link
    assert [b["ref"] for b in local_agent.forward_controls(state)] == ["b2"]
    assert local_agent.forward_control(state)["text"] == "Continue"

    # answer buttons answer, they never navigate and they never submit
    assert local_agent.is_forward_control(state, "b0") is False
    assert local_agent._submit_like(state, "b0") is False

    # the forward control is not the submit gate, whatever its type attribute
    assert local_agent.is_forward_control(state, "b2") is True
    assert local_agent._submit_like(state, "b2") is False

    # the real submit still is
    assert local_agent._submit_like(state, "b5") is True
    assert local_agent.is_forward_control(state, "b5") is False

    # and the near-misses stay out
    assert local_agent.forward_label("Back") is False
    assert local_agent.forward_label("Continue with Google") is False
    assert local_agent.forward_label("Continue shopping") is False
    assert local_agent.forward_label("Submit application") is False
    assert local_agent.forward_label("Apply") is False
    assert local_agent.forward_label("Save and continue") is True
    assert local_agent.forward_label("Next →") is True
    assert local_agent.forward_label("Save & Next") is True


def test_a_step_answers_its_question_advances_and_still_holds_at_the_final_apply() -> None:
    """End to end over the three button kinds: click the answer, take the
    forward control, and let the hold gate stop the real submit."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/q1",
                [],
                [
                    {"ref": "b0", "text": "Yes", "type": "submit",
                     "question": "Are you authorized to work in the US?"},
                    {"ref": "b1", "text": "No", "type": "submit",
                     "question": "Are you authorized to work in the US?"},
                    {"ref": "b2", "text": "Continue", "type": "submit"},
                ],
                forward="b2",
            ),
            Step("https://apply.example.test/q2", [], [{"ref": "b0", "text": "Apply", "type": "submit"}]),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "click", "target": "b0"}]},
            {"actions": [{"action": "click", "target": "b2"}]},
            {"actions": [{"action": "click", "target": "b0"}]},  # the real Apply
        ]
    )
    result = run(driver, chat, hold=True)

    assert "0:b0" in driver.clicks, "the answer button was blocked by the submit gate"
    assert "0:b2" in driver.clicks, "the forward control was blocked by the submit gate"
    assert driver.index == 1
    assert "1:b0" not in driver.clicks, "--hold let the final Apply through"
    assert result["status"] == local_agent.AUDIT_PENDING
    assert "submit blocked" in str(result["reason"])
    assert result["confirmation_text"] == ""


# --------------------------- (7) an SPA wizard whose steps all share one URL


def test_a_same_url_spa_wizard_progresses_without_a_bogus_no_progress_park() -> None:
    """Four steps, ONE route, an identically-labelled `Continue` on each.

    URL was the engine's whole notion of page identity, so the per-field memory
    never cleared and the repeat guard read four legitimate forward clicks as
    "the model repeated click Continue 4 times". Page identity is the step's
    FINGERPRINT — route, step marker and question shape.
    """
    url = "https://apply.example.test/wizard"
    steps = [
        Step(url, [field("f0", f"Question {i + 1}", required=True)], [CONTINUE], forward="b0",
             text=f"Step {i + 1} of 5")
        for i in range(4)
    ]
    steps.append(Step(url, [], [SUBMIT], text="Step 5 of 5"))
    driver = WizardDriver(steps)
    chat = scripted(
        [
            reply
            for i in range(4)
            for reply in (
                {"actions": [{"action": "type", "target": "f0", "text": "Yes"}]},
                {"actions": [{"action": "click", "target": "b0"}]},
            )
        ]
        + [STOP_HERE]
    )
    result = run(driver, chat, hold=True)

    assert driver.index == 4, "the wizard did not reach its last step"
    assert "no progress" not in str(result["reason"] or "")
    assert result["status"] == local_agent.AUDIT_PENDING
    assert notes(result).count("step advanced") == 4
    assert [s.fields[0]["value"] for s in driver.steps[:4]] == ["Yes"] * 4


def test_the_step_fingerprint_tells_wizard_steps_apart_on_one_url() -> None:
    """The identity primitive behind the test above, on its own."""
    url = "https://apply.example.test/wizard"
    one = {"url": url, "title": "Apply", "text": "Step 1 of 3",
           "fields": [field("f0", "Question one")], "buttons": [CONTINUE]}
    two = {"url": url, "title": "Apply", "text": "Step 2 of 3",
           "fields": [field("f0", "Question two")], "buttons": [CONTINUE]}

    assert local_agent.page_fingerprint(one) != local_agent.page_fingerprint(two)
    assert local_agent.page_transition(one, two) == "step"
    assert local_agent.page_transition(one, copy.deepcopy(one)) == ""
    assert local_agent.step_marker(two) == "2/3"


def test_revealing_a_follow_up_question_is_not_a_new_step() -> None:
    """The counterweight: a conditional field appearing must NOT read as a new
    page, or the engine throws away the verified-selection memory that protects
    the answers already on the page and re-types over them."""
    before = {
        "url": "https://apply.example.test/one",
        "fields": [field("f0", "Do you need visa sponsorship?")],
        "buttons": [CONTINUE],
    }
    after = {
        "url": "https://apply.example.test/one",
        "fields": [field("f0", "Do you need visa sponsorship?"),
                   field("f1", "Which country issued your passport?")],
        "buttons": [CONTINUE],
    }

    assert local_agent.page_transition(before, after) == ""
    assert local_agent.page_fingerprint(before) != local_agent.page_fingerprint(after)


# ----------------------------------- (8) a batch that navigates mid-flight


def test_a_batch_that_navigates_mid_flight_drops_its_stale_actions() -> None:
    """The model batches [type, click Continue, type]. The third action was
    planned against step 1 and its ref now names a step-2 field — executing it
    writes a last name into "Why do you want to work here?" (probe F3)."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/p1",
                [field("f0", "First name", required=True), field("f1", "Last name", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/p2",
                [field("f0", "Why do you want to work here?", tag="textarea", type="textarea"),
                 field("f1", "Phone number", type="tel")],
                [NEXT],
                forward="b0",
            ),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "click", "target": "b0"},
                         {"action": "type", "target": "f1", "text": "Halloway"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert [t for t in driver.typed if t[0].endswith("p2")] == [], "a stale action wrote to the next step"
    assert driver.values_on("p2")["Phone number"] == ""
    assert "belong to that step and are dropped" in notes(result)


def test_a_batch_that_navigates_mid_flight_verifies_against_the_page_it_typed_on() -> None:
    """The value DID land on step 1. Verified against step 2 it reads as a miss,
    and the miss is what sends the engine back to "repair" a field that was
    never wrong."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/v1",
                [field("f0", "First name", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/v2",
                [field("f0", "Preferred pronouns"), field("f1", "Phone number", type="tel")],
                [NEXT],
                forward="b0",
            ),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "click", "target": "b0"}]},
            STOP_HERE,
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.values_on("v1")["First name"] == "Mira"
    assert "value mismatch" not in notes(result), "a landed value was reported as a miss"
    assert "rejects typing" not in notes(result)


# ------------------------------------------------------- the model's contract


def test_the_prompt_tells_the_model_it_is_on_one_step_of_a_longer_form() -> None:
    prompt = local_agent.SYSTEM_PROMPT
    assert "STEP-BY-STEP FORMS" in prompt
    assert "NOT necessarily the entire form" in prompt
    assert "CLICK IT" in prompt
    assert "never" in prompt and "sparse" in prompt
    assert "Do NOT click forward while a visible required field" in prompt
    # the guardrails this brief is NOT allowed to weaken
    assert "Fill fields ONLY from the applicant data provided" in prompt
    assert "Never invent answers" in prompt


def test_the_user_message_carries_the_form_memory_not_just_the_page() -> None:
    """The model reasons over current page + compact form memory."""
    state = {
        "url": "https://apply.example.test/step3",
        "title": "Apply",
        "text": "Step 3 of 4",
        "fields": [field("f0", "Portfolio URL")],
        "buttons": [CONTINUE, {"ref": "b1", "text": "Back", "type": "button"}],
        "confirmation": {"detected": False, "matched": [], "snippet": ""},
    }
    message = local_agent.build_user_message(
        APPLICANT,
        state,
        [],
        True,
        JOB,
        pages=["…/step1", "…/step2", "…/step3"],
        answered=['First name = "Mira"'],
    )

    assert "FORM STEP: you are on step 3/4 of a possibly multi-step application" in message
    assert "NOT necessarily the whole form" in message
    assert "PAGES SEEN: …/step1 → …/step2 → …/step3" in message
    assert 'First name = "Mira"' in message
    assert 'FORWARD CONTROL ON THIS PAGE: b0 "Continue"' in message


def test_the_form_memory_stays_quiet_on_a_plain_single_page_form() -> None:
    """A one-page Greenhouse form has no forward control and no history; the
    memory block must not invent a wizard around it."""
    state = {
        "url": "https://boards.example.test/verdant/apply",
        "title": "Apply",
        "text": "Application form",
        "fields": [field("f0", "Email address", type="email", required=True)],
        "buttons": [SUBMIT],
        "confirmation": {"detected": False, "matched": [], "snippet": ""},
    }
    lines = local_agent.form_memory_lines(state)

    assert len(lines) == 1
    assert lines[0].startswith("FORM STEP: you are on page 1")
    assert "FORWARD CONTROL" not in " ".join(lines)


def test_verified_answers_survive_a_step_transition_into_the_next_prompt() -> None:
    """Page-local memory is cleared on every transition on purpose. What the
    applicant actually answered is not page-local — the model must still see it
    two steps later, or it re-asks questions it already filled."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/m1",
                [field("f0", "First name", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step("https://apply.example.test/m2", [field("f0", "Portfolio URL")], [NEXT], forward="b0"),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "click", "target": "b0"}]},
            STOP_HERE,
        ]
    )
    run(driver, chat, hold=True)

    last_prompt = chat.calls[-1]["user"]  # type: ignore[attr-defined]
    assert "VERIFIED ON EARLIER STEPS" in last_prompt
    assert 'First name = "Mira"' in last_prompt
    assert "PAGES SEEN:" in last_prompt


# ----------------------------------------------- the guardrails still stand


def test_no_step_of_a_wizard_may_invent_an_applicant_fact() -> None:
    """The PII guard is per-action and unchanged by the navigation model: a
    value the applicant never declared is refused on step 2 exactly as on step 1."""
    driver = WizardDriver(
        [
            Step(
                "https://apply.example.test/i1",
                [field("f0", "First name", required=True)],
                [CONTINUE],
                forward="b0",
            ),
            Step(
                "https://apply.example.test/i2",
                [field("f0", "Current employer", required=True)],
                [NEXT],
                forward="b0",
            ),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "click", "target": "b0"}]},
            {"actions": [{"action": "type", "target": "f0", "text": "Northwind Robotics"}]},
            {"actions": [{"action": "stop", "text": "no employer in the applicant data"}]},
        ]
    )
    result = run(driver, chat, hold=True)

    assert driver.values_on("i2")["Current employer"] == ""
    assert "is not a value in the applicant data" in notes(result)
    assert result["status"] == local_agent.AUDIT_PENDING
    assert result["status"] != "applied"


def test_a_wizard_that_reaches_the_end_never_claims_a_submission_it_did_not_make() -> None:
    """`done` without a visible confirmation is not "applied", on step 4 of a
    wizard exactly as on a single page."""
    driver = WizardDriver(
        [
            Step("https://apply.example.test/f1", [field("f0", "First name")], [CONTINUE], forward="b0"),
            Step("https://apply.example.test/f2", [], [SUBMIT]),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "type", "target": "f0", "text": "Mira"},
                         {"action": "click", "target": "b0"}]},
            {"actions": [{"action": "done", "text": "sent"}]},
        ]
    )
    result = run(driver, chat)

    assert result["status"] == "stopped"
    assert result["status"] != "applied"
    assert "no confirmation message was visible" in str(result["reason"])


# ----------------------------- (7) an overview page in front of the real form


def test_an_overview_page_hands_off_to_the_separate_application_form() -> None:
    """Ashby's two-step shape: a job overview whose only control is `Apply for
    this Job`, and the form on a separate `/application` route.

    `Apply for this Job` is deliberately NOT a forward control (it is the same
    text a page-wide "apply" link carries), so the handoff runs through the
    mid-batch transition instead: the click's own re-read sees the new route,
    drops the rest of the batch, and the next round's opening snapshot clears
    the per-field state — BOTH stamped at the same action index. The engine
    must then re-read and fill the form it landed on.

    Pinned after the 2026-08-26 Endex stall, where this shape looked like the
    culprit and was not: the navigation is sound, the 907s was the relay
    (`test_a_call_that_ran_out_the_clock_is_not_re_sent_for_another_full_timeout`).
    """
    overview = "https://jobs.ashbyhq.com/endex/0a7a58c3"
    driver = WizardDriver(
        [
            Step(
                overview,
                [],
                [{"ref": "b16", "text": "Apply for this Job", "type": "button"}],
                forward="b16",
                text="Endex is hiring a Founding Designer.",
            ),
            Step(
                f"{overview}/application",
                [field("f0", "Name", required=True),
                 field("f1", "Email", type="email", required=True)],
                [{"ref": "b0", "text": "Submit Application", "type": "submit"}],
            ),
        ]
    )
    chat = scripted(
        [
            {"actions": [{"action": "click", "target": "b16"}]},
            {"actions": [{"action": "type", "target": "f0", "text": "Mira Halloway"},
                         {"action": "type", "target": "f1", "text": "mira@halloway.example"}]},
            {"actions": [{"action": "stop", "text": "held for audit"}]},
        ]
    )
    result = run(driver, chat, hold=True)

    drops = [e for e in result["trace"] if "mid-batch" in str(e["note"])]
    clears = [e for e in result["trace"] if "cleared the per-field engine state" in str(e["note"])]
    assert len(drops) == 1 and len(clears) == 1
    assert drops[0]["n"] == clears[0]["n"] == 1, "both fire on the same action index"
    # ...and the round after them is a real think turn, not the end of the run.
    assert [e["action"] for e in result["trace"]].count("think") == 3
    assert driver.values_on("/application") == {
        "Name": "Mira Halloway",
        "Email": "mira@halloway.example",
    }
    assert result["status"] == local_agent.AUDIT_PENDING
