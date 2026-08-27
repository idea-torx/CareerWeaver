"""The driver against a REAL browser — a headless chromium, real DOM, real events.

`tests/test_local_agent.py` drives the loop over an in-memory page, which is the
right shape for the loop and the wrong shape for the driver: a stub cannot tell
you that `el.closest('.select__control')` misses a react-select *option*, or that
a synthetic `.click()` is enough for the option but not for the control. That is
exactly where run 71 died — the driver answered every option click with "opened
combo" and selected nothing, for a whole run, with the suite green.

So these tests load real markup into a real page and assert what the page says
afterwards. The react-select fixture below reproduces the library's event
contract: the control opens on mousedown (or ArrowDown), options select on
click, and the selection lives in `.select__single-value` — not in the input.

Skipped, never failed, when chromium is not installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from weaver import local_driver
from weaver.local_driver import LocalDriver

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import sync_playwright  # noqa: E402


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_playwright() as pw:
        try:
            instance = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - depends on the machine
            pytest.skip(f"chromium is not available: {exc}")
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    context = browser.new_context(
        user_agent=local_driver.CHROME_UA, viewport=local_driver.VIEWPORT
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def driver_for(page: Any, html: str, resume_path: str | Path | None = None) -> LocalDriver:
    page.set_content(html)
    return LocalDriver(page, resume_path)


def field_by_label(state: dict[str, Any], label: str) -> dict[str, Any]:
    for candidate in state["fields"]:
        if candidate["label"] == label:
            return candidate
    raise AssertionError(f'no field labelled "{label}" in {[f["label"] for f in state["fields"]]}')


def button_by_text(state: dict[str, Any], text: str) -> dict[str, Any]:
    for candidate in state["buttons"]:
        if candidate["text"] == text:
            return candidate
    raise AssertionError(f'no button "{text}" in {[b["text"] for b in state["buttons"]]}')


# ------------------------------------------------------------------ the fixtures

PLAIN_FORM = """
<!doctype html><html><body>
  <h1>Apply — Verdant Systems</h1>
  <form>
    <label for="first">First name *</label>
    <input id="first" name="first_name" required>

    <input name="email" type="email" aria-label="Email address" placeholder="you@example.com">

    <div class="field">
      <div class="field__label">Where are you based?</div>
      <input name="location">
    </div>

    <label>Cover letter
      <textarea name="cover"></textarea>
    </label>

    <label for="pronouns">Pronouns</label>
    <select id="pronouns" name="pronouns">
      <option value="">Select…</option>
      <option value="he">He/Him</option>
      <option value="she">She/Her</option>
      <option value="they">They/Them</option>
    </select>

    <label for="consent">I consent to the retention of my data</label>
    <input id="consent" name="consent" type="checkbox">

    <input name="csrf" type="hidden" value="tok">
    <input name="ghost" type="text" style="display:none">
    <input id="resume" name="resume" type="file" style="display:none">

    <input name="locked" type="text" disabled aria-label="Locked field">

    <div id="bio" contenteditable="true" aria-label="Short bio"></div>

    <button type="submit">Submit application</button>
  </form>
</body></html>
"""

#: What run 81 actually met: EEOC questions rendered as REAL <select> boxes that
#: also wear the combo dress (select__input class, role/aria-haspopup). Keystrokes
#: do nothing to these — the model typed, the value stayed empty, and every field
#: came back "no option matched". A keystroke listener records any typing so the
#: test can prove the driver did not resort to it.
NATIVE_SELECT_FORM = """
<!doctype html><html><body>
  <form>
    <label for="trans">Do you identify as transgender?</label>
    <select id="trans" name="transgender" class="select__input" role="combobox"
            aria-haspopup="listbox">
      <option value="">Select…</option>
      <option value="1">Yes</option>
      <option value="2">No</option>
      <option value="3">I prefer not to disclose</option>
    </select>

    <label for="race">Race / Ethnicity</label>
    <select id="race" name="race">
      <option value="">Select…</option>
      <option value="w">White - A person having origins in any of the original peoples of Europe</option>
      <option value="b">Black or African American</option>
      <option value="d">Decline to self-identify</option>
    </select>

    <label for="dead">Locked question</label>
    <select id="dead" name="dead" disabled>
      <option value="y">Yes</option>
    </select>
  </form>
<script>
  window.__keys = [];
  document.querySelectorAll('select').forEach((el) =>
    el.addEventListener('keydown', (e) => window.__keys.push(e.key)));
  window.__changes = [];
  document.querySelector('#trans').addEventListener('change', () =>
    window.__changes.push(document.querySelector('#trans').value));
</script>
</body></html>
"""

#: react-select's event contract, reproduced: mousedown/ArrowDown open the menu,
#: typing filters it, a click on an option commits the selection into
#: `.select__single-value` — the raw input never holds it.
REACT_SELECT_FORM = """
<!doctype html><html><body>
  <div class="select" id="q-auth">
    <div id="lbl-auth">Are you legally authorized to work?</div>
    <div class="select__control" aria-expanded="false">
      <div class="select__value-container"></div>
      <div class="select__input-container">
        <input class="select__input" id="auth-input" role="combobox"
               aria-autocomplete="list" aria-labelledby="lbl-auth" autocomplete="off">
      </div>
    </div>
    <div class="select__menu" role="listbox" hidden>
      <div class="select__option" role="option">Yes, I am legally authorized to work</div>
      <div class="select__option" role="option">No, I require sponsorship</div>
    </div>
  </div>
<script>
  const control = document.querySelector('.select__control');
  const menu = document.querySelector('.select__menu');
  const input = document.querySelector('.select__input');
  const values = document.querySelector('.select__value-container');
  const open = () => { menu.hidden = false; control.setAttribute('aria-expanded', 'true'); };
  const close = () => { menu.hidden = true; control.setAttribute('aria-expanded', 'false'); };
  control.addEventListener('mousedown', open);
  input.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') open(); });
  input.addEventListener('input', () => {
    open();
    const q = input.value.trim().toLowerCase();
    menu.querySelectorAll('.select__option').forEach((o) => {
      o.hidden = q ? !o.textContent.toLowerCase().includes(q) : false;
    });
  });
  menu.querySelectorAll('.select__option').forEach((option) => {
    option.addEventListener('click', () => {
      values.innerHTML = '<div class="select__single-value">' + option.textContent + '</div>';
      input.value = '';
      input.style.opacity = '0';  // react-select's hidden-search state (run 82)
      close();
    });
  });
</script>
</body></html>
"""

#: The shape that broke run: a react-select with NO aria-labelledby on the input
#: — only a placeholder and the question text in the surrounding container. The
#: placeholder is replaced by `.select__single-value` on selection, so a label
#: read from it disappears and the field goes anonymous mid-verify.
PLACEHOLDER_SELECT_FORM = """
<!doctype html><html><body>
  <div class="item-editor">
    <div class="question-text">Location (City)</div>
    <div class="select">
      <div class="select__control" aria-expanded="false">
        <div class="select__value-container">
          <div class="select__placeholder">Start typing a city…</div>
          <div class="select__input-container">
            <input class="select__input" role="combobox" autocomplete="off">
          </div>
        </div>
      </div>
      <div class="select__menu" role="listbox" hidden>
        <div class="select__option" role="option">Vancouver, British Columbia, Canada</div>
        <div class="select__option" role="option">Vancouver, Washington, United States</div>
      </div>
    </div>
  </div>
<script>
  const control = document.querySelector('.select__control');
  const menu = document.querySelector('.select__menu');
  const input = document.querySelector('.select__input');
  const values = document.querySelector('.select__value-container');
  const holder = document.querySelector('.select__placeholder');
  const open = () => { menu.hidden = false; control.setAttribute('aria-expanded', 'true'); };
  control.addEventListener('mousedown', open);
  input.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') open(); });
  menu.querySelectorAll('.select__option').forEach((option) => {
    option.addEventListener('click', () => {
      // react-select swaps the placeholder out for the single value.
      holder.remove();
      const sv = document.createElement('div');
      sv.className = 'select__single-value';
      sv.textContent = option.textContent;
      values.insertBefore(sv, values.firstChild);
      input.value = '';
      menu.hidden = true; control.setAttribute('aria-expanded', 'false');
    });
  });
</script>
</body></html>
"""
#: Run 79's f15: a self-identification combo whose options are DEFINITIONS. The
#: applicant's declared answer ("White / Caucasian") appears in none of them, so
#: searching the phrase verbatim empties the menu.
RACE_SELECT_FORM = """
<!doctype html><html><body>
  <div class="select" id="q-race">
    <div id="lbl-race">Race / Ethnicity</div>
    <div class="select__control" aria-expanded="false">
      <div class="select__value-container"></div>
      <div class="select__input-container">
        <input class="select__input" id="race-input" role="combobox"
               aria-autocomplete="list" aria-labelledby="lbl-race" autocomplete="off">
      </div>
    </div>
    <div class="select__menu" role="listbox" hidden>
      <div class="select__option" role="option">White - A person having origins in Europe</div>
      <div class="select__option" role="option">Asian - A person having origins in the Far East</div>
    </div>
  </div>
<script>
  const control = document.querySelector('.select__control');
  const menu = document.querySelector('.select__menu');
  const input = document.querySelector('.select__input');
  const values = document.querySelector('.select__value-container');
  const open = () => { menu.hidden = false; control.setAttribute('aria-expanded', 'true'); };
  control.addEventListener('mousedown', open);
  input.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') open(); });
  input.addEventListener('input', () => {
    open();
    const q = input.value.trim().toLowerCase();
    menu.querySelectorAll('.select__option').forEach((o) => {
      o.hidden = q ? !o.textContent.toLowerCase().includes(q) : false;
    });
  });
  menu.querySelectorAll('.select__option').forEach((option) => {
    option.addEventListener('click', () => {
      values.innerHTML = '<div class="select__single-value">' + option.textContent + '</div>';
      input.value = '';
      input.style.opacity = '0';  // react-select's hidden-search state (run 82)
      menu.hidden = true; control.setAttribute('aria-expanded', 'false');
    });
  });
</script>
</body></html>
"""

#: Run 79's f13: the menu is BUILT after the search round-trips (a remote option
#: fetch). The options do not exist in the DOM when the keystrokes finish, so a
#: single scan sees an empty menu and calls a real option missing.
SLOW_MENU_SELECT_FORM = """
<!doctype html><html><body>
  <div class="select" id="q-gender">
    <div id="lbl-gender">Gender</div>
    <div class="select__control" aria-expanded="false">
      <div class="select__value-container"></div>
      <div class="select__input-container">
        <input class="select__input" id="gender-input" role="combobox"
               aria-autocomplete="list" aria-labelledby="lbl-gender" autocomplete="off">
      </div>
    </div>
    <div class="select__menu" role="listbox" hidden></div>
  </div>
<script>
  const control = document.querySelector('.select__control');
  const menu = document.querySelector('.select__menu');
  const input = document.querySelector('.select__input');
  const values = document.querySelector('.select__value-container');
  const open = () => { menu.hidden = false; control.setAttribute('aria-expanded', 'true'); };
  control.addEventListener('mousedown', open);
  input.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') open(); });
  let timer = null;
  input.addEventListener('input', () => {
    open();
    menu.innerHTML = '';
    const q = input.value.trim().toLowerCase();
    if (timer) clearTimeout(timer);
    // the options arrive ~1s later, long after the keystrokes settle
    timer = setTimeout(() => {
      ['Man', 'Woman', 'Non-binary'].filter((t) => !q || t.toLowerCase().includes(q))
        .forEach((text) => {
          const option = document.createElement('div');
          option.className = 'select__option';
          option.setAttribute('role', 'option');
          option.textContent = text;
          option.addEventListener('click', () => {
            values.innerHTML = '<div class="select__single-value">' + text + '</div>';
            input.value = '';
            menu.hidden = true; control.setAttribute('aria-expanded', 'false');
          });
          menu.appendChild(option);
        });
    }, 1000);
  });
</script>
</body></html>
"""

#: A consent box the way Greenhouse ships it: the real input is a 1px visually
#: hidden checkbox inside a styled label. Its `.value` is "on" whether or not it
#: is ticked — only `.checked` says what a click did.
HIDDEN_CONSENT_FORM = """
<!doctype html><html><body><form>
  <div class="field" id="consent-field">
    <label class="checkbox" id="consent-label">
      <input type="checkbox" id="hidden-consent" name="consent"
             style="position:absolute;width:1px;height:1px;clip:rect(0 0 0 0)">
      <span class="box"></span>
      I consent to the retention of my personal data
    </label>
  </div>
</form></body></html>
"""


# -------------------------------------------------------------------- snapshot


def test_snapshot_describes_the_page_the_way_the_prompt_reads_it(page: Any) -> None:
    state = driver_for(page, PLAIN_FORM).snapshot()

    assert state["title"] == ""  # set_content leaves no <title>
    assert "Submit application" in state["text"]

    first = field_by_label(state, "First name *")
    assert (first["ref"], first["tag"], first["name"], first["required"]) == (
        "f0", "input", "first_name", True,
    )
    # every stamped ref is resolvable back to its element
    assert page.locator(local_driver.selector_for(first["ref"])).count() == 1

    email = field_by_label(state, "Email address")  # aria-label
    assert email["type"] == "email" and email["placeholder"] == "you@example.com"

    # Greenhouse/Lever style: the label is a <div> sitting above the control
    assert field_by_label(state, "Where are you based?")["name"] == "location"
    # a wrapping <label>
    assert field_by_label(state, "Cover letter")["tag"] == "textarea"

    select = field_by_label(state, "Pronouns")
    assert select["tag"] == "select"
    assert select["options"] == ["Select…", "He/Him", "She/Her", "They/Them"]

    assert field_by_label(state, "I consent to the retention of my data")["value"] == "false"
    assert field_by_label(state, "Locked field")["disabled"] is True
    assert field_by_label(state, "Short bio")["tag"] == "contenteditable"

    names = [f["name"] for f in state["fields"]]
    assert "csrf" not in names  # type=hidden
    assert "ghost" not in names  # display:none
    assert "resume" in names  # a hidden FILE input is kept on purpose
    assert button_by_text(state, "Submit application")["type"] == "submit"


def test_snapshot_reports_the_character_limit_a_field_clips_at(page: Any) -> None:
    """Live test 5: Workable renders required essay questions as single-line
    <input maxlength="127">. The browser keeps the first 127 characters of a
    357-character answer and drops the rest mid-sentence, and the write still
    reports the full length — so the LIMIT has to be in the snapshot, or the
    model composes past it every time and nothing in the loop can tell."""
    driver = driver_for(
        page,
        """
        <body><form>
          <label for="why">Why do you want this role?</label>
          <input id="why" name="why" maxlength="127" required>
          <label for="notes">Anything else?
            <textarea id="notes" name="notes" maxlength="500"></textarea>
          </label>
          <label for="free">No limit
            <input id="free" name="free">
          </label>
        </form></body>
        """,
    )
    state = driver.snapshot()

    assert field_by_label(state, "Why do you want this role?")["maxlength"] == 127
    assert field_by_label(state, "Anything else?")["maxlength"] == 500
    # No attribute means no key at all — never a limit of 0 that reads as "reject
    # everything" to the caller.
    assert "maxlength" not in field_by_label(state, "No limit")

    long_answer = "I have spent nine years shipping design systems. " * 8
    ref = field_by_label(state, "Why do you want this role?")["ref"]
    result = driver.type(ref, long_answer)

    # A programmatic .value set is NOT clipped by the DOM (maxlength polices user
    # input only) — it sails past the limit and the framework clips it later, so
    # the driver refuses the write itself rather than leave a fragment behind.
    assert result["ok"] is False
    assert result["maxlength"] == 127
    assert "at most 127 characters" in result["note"]
    assert page.eval_on_selector("#why", "el => el.value") == ""

    # An answer that fits is written normally.
    fits = long_answer[:100]
    assert driver.type(ref, fits)["ok"] is True
    assert page.eval_on_selector("#why", "el => el.value") == fits


def test_snapshot_and_confirm_text_agree_on_a_confirmation(page: Any) -> None:
    driver = driver_for(
        page,
        "<body><main><h1>All set</h1>"
        "<p>Thank you for applying to Verdant Systems. We will be in touch.</p></main></body>",
    )
    state = driver.snapshot()

    assert state["confirmation"]["detected"] is True
    assert state["confirmation"]["matched"] == ["thank you for applying"]
    assert "Thank you for applying to Verdant Systems" in state["confirmation"]["snippet"]
    assert "Thank you for applying" in driver.confirm_text()


def test_no_confirmation_is_reported_for_an_ordinary_form(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)

    assert driver.snapshot()["confirmation"]["detected"] is False
    assert driver.confirm_text() == ""


# ------------------------------------------------------------------------ type


def test_type_sets_the_value_and_fires_the_events_react_listens_for(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)
    ref = field_by_label(driver.snapshot(), "First name *")["ref"]
    page.evaluate(
        """() => {
          window.__events = [];
          const el = document.querySelector('#first');
          ['input', 'change'].forEach((n) =>
            el.addEventListener(n, () => window.__events.push(n)));
        }"""
    )

    result = driver.type(ref, "Mira")

    assert result["ok"] is True and result["value"] == "Mira"
    assert page.eval_on_selector("#first", "el => el.value") == "Mira"
    assert page.evaluate("() => window.__events") == ["input", "change"]
    assert field_by_label(driver.snapshot(), "First name *")["value"] == "Mira"


def test_type_picks_a_native_option_by_label_or_value(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)
    ref = field_by_label(driver.snapshot(), "Pronouns")["ref"]

    assert driver.type(ref, "They/Them")["value"] == "they"
    assert field_by_label(driver.snapshot(), "Pronouns")["value"] == "They/Them"
    # a partial label still resolves
    assert driver.type(ref, "She")["value"] == "she"
    assert driver.type(ref, "Ze/Zir")["ok"] is False


def test_type_toggles_a_checkbox_from_a_word(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)
    ref = field_by_label(driver.snapshot(), "I consent to the retention of my data")["ref"]

    assert driver.type(ref, "true")["value"] == "True"
    assert page.eval_on_selector("#consent", "el => el.checked") is True
    assert driver.type(ref, "no")["value"] == "False"
    assert page.eval_on_selector("#consent", "el => el.checked") is False


def test_type_writes_into_a_contenteditable(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)
    ref = field_by_label(driver.snapshot(), "Short bio")["ref"]

    assert driver.type(ref, "Designer turned engineer")["ok"] is True
    assert field_by_label(driver.snapshot(), "Short bio")["value"] == "Designer turned engineer"


def test_type_refuses_what_it_cannot_honestly_fill(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)
    state = driver.snapshot()
    resume_ref = next(f["ref"] for f in state["fields"] if f["type"] == "file")

    file_input = driver.type(resume_ref, "/tmp/resume.docx")
    assert file_input["ok"] is False and "upload action" in file_input["note"]

    locked = driver.type(field_by_label(state, "Locked field")["ref"], "x")
    assert locked["ok"] is False and "disabled" in locked["note"]

    missing = driver.type("f99", "x")
    assert missing["ok"] is False and "no element" in missing["note"]

    bad = driver.type("input[[", "x")
    assert bad["ok"] is False and "bad selector" in bad["note"]


# ----------------------------------------------------------------------- click


def test_click_reports_the_label_it_clicked(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)
    page.evaluate("() => document.querySelector('button').addEventListener('click', "
                  "() => { window.__submitted = true; })")
    ref = button_by_text(driver.snapshot(), "Submit application")["ref"]

    result = driver.click(ref)

    assert result["ok"] is True
    assert result["note"] == 'clicked "Submit application"'
    assert page.evaluate("() => window.__submitted") is True


def test_clicking_a_react_select_option_actually_selects_it(page: Any) -> None:
    """THE REGRESSION TEST for run 71.

    `.select__option` contains the substring "select__", which the old control
    probe read as "this is a combo" — so every option click returned
    `opened combo (select__option …)` and selected nothing, while the engine
    logged it as a success. An option is not a control: it must be clicked.
    """
    driver = driver_for(page, REACT_SELECT_FORM)
    control_ref = field_by_label(driver.snapshot(), "Are you legally authorized to work?")["ref"]

    opened = driver.click(control_ref)
    assert opened["ok"] is True
    assert opened["note"].startswith("opened combo")  # the control DOES open
    assert page.eval_on_selector(".select__control", "el => el.getAttribute('aria-expanded')") == "true"

    option = button_by_text(driver.snapshot(), "Yes, I am legally authorized to work")
    clicked = driver.click(option["ref"])

    assert clicked["ok"] is True
    assert "opened combo" not in clicked["note"], "the option was treated as a control again"
    assert clicked["note"] == 'clicked "Yes, I am legally authorized to work"'
    # the selection lives in the widget, and the snapshot reads it back
    assert page.eval_on_selector(
        ".select__single-value", "el => el.textContent"
    ) == "Yes, I am legally authorized to work"
    landed = field_by_label(driver.snapshot(), "Are you legally authorized to work?")
    assert landed["value"] == "Yes, I am legally authorized to work"


def test_a_combo_keeps_its_label_after_a_value_lands(page: Any) -> None:
    """THE REGRESSION TEST for the vanishing label.

    The snapshot used to name a placeholder-only react-select after its
    PLACEHOLDER. Selecting a value deletes the placeholder, so the field lost
    its name, `_field_by_label`/`_field_by_ref` could not find it again, and
    `fix_dropdown`'s verify read "" from a widget that was in fact filled.
    The label must be the same string before and after the selection.
    """
    driver = driver_for(page, PLACEHOLDER_SELECT_FORM)

    before = field_by_label(driver.snapshot(), "Location (City)")
    assert before["value"] == ""  # nothing landed yet
    assert before["placeholder"] == ""  # the label did NOT come from a placeholder attr

    driver.click(before["ref"])
    option = button_by_text(driver.snapshot(), "Vancouver, British Columbia, Canada")
    assert driver.click(option["ref"])["ok"] is True
    assert page.locator(".select__placeholder").count() == 0  # the placeholder is gone

    after = field_by_label(driver.snapshot(), "Location (City)")
    assert after["label"] == before["label"]
    assert after["value"] == "Vancouver, British Columbia, Canada"


def test_a_preloaded_menu_is_picked_from_without_typing(page: Any) -> None:
    """RUN 81 (Webflow). A menu that lists its options as soon as it opens does
    not need — and may not tolerate — search keystrokes: Webflow's selects
    filter to "No options" on any token. The driver reads the OPEN menu first
    and clicks the match; keystrokes are the fallback for async lists only."""
    driver = driver_for(page, REACT_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Are you legally authorized to work?")["ref"]
    page.evaluate(
        """() => {
          window.__typed = [];
          document.querySelector('.select__input')
            .addEventListener('input', (e) => window.__typed.push(e.target.value));
        }"""
    )

    result = driver.type(ref, "No, I require sponsorship")

    assert result["ok"] is True
    assert page.evaluate("() => window.__typed") == []  # zero keystrokes
    assert result["selected"] == "No, I require sponsorship"
    assert result["value"] == "No, I require sponsorship"


def test_an_async_menu_still_gets_real_keystrokes(page: Any) -> None:
    """react-select validates programmatic `.value` away — when the menu shows
    nothing until searched, the driver falls back to real keystrokes, searching
    on the LEADING token (a whole guardrail phrase matches no option)."""
    driver = driver_for(page, SLOW_MENU_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Gender")["ref"]
    page.evaluate(
        """() => {
          window.__typed = [];
          document.querySelector('.select__input')
            .addEventListener('input', (e) => window.__typed.push(e.target.value));
        }"""
    )

    result = driver.type(ref, "Man")

    assert result["ok"] is True and "combo-box" in result["note"]
    assert page.evaluate("() => window.__typed") == ["M", "Ma", "Man"]
    assert result["selected"] == "Man"


def test_typing_into_a_combo_selects_the_option_it_filtered_to(page: Any) -> None:
    """THE REGRESSION TEST for run 76.

    Typing into a react-select puts SEARCH TEXT in the input and selects
    NOTHING — a human then clicks the option. The driver used to stop at the
    keystrokes, so the widget read back "" forever: the verify called the field
    unfilled, the model grew frustrated, hit Apply, and the form rejected it.
    `type()` must finish the job — match the option, REAL-click it, and read the
    landed single-value back.
    """
    driver = driver_for(page, REACT_SELECT_FORM)
    page.evaluate(
        """() => {
          window.__clicks = [];
          document.querySelectorAll('.select__option').forEach((o) =>
            // a REAL click arrives with a mousedown first; a synthetic
            // el.click() does not, and react-select ignores it.
            o.addEventListener('mousedown', () => window.__clicks.push(o.textContent)));
        }"""
    )
    ref = field_by_label(driver.snapshot(), "Are you legally authorized to work?")["ref"]

    result = driver.type(ref, "Yes")

    assert result["ok"] is True
    assert result["selected"] == "Yes, I am legally authorized to work"
    assert page.evaluate("() => window.__clicks") == ["Yes, I am legally authorized to work"]
    # the SELECTION landed in the widget, not search text in the input
    assert page.eval_on_selector(".select__single-value", "el => el.textContent") == (
        "Yes, I am legally authorized to work"
    )
    assert result["value"] == "Yes, I am legally authorized to work"
    landed = field_by_label(driver.snapshot(), "Are you legally authorized to work?")
    assert landed["value"] == "Yes, I am legally authorized to work"


def test_typing_an_answer_no_option_offers_selects_nothing(page: Any) -> None:
    """No match is not a failure and not a wrong pick: `selected` stays null so
    the mismatch escalation gets its turn. A menu that LISTS options is final —
    nothing is typed at it (run 82: typing filtered Webflow's selects to "No
    options" forever) and the note reports the REAL options so the model's next
    round chooses from reality. Over-selecting here would answer wrongly."""
    driver = driver_for(page, REACT_SELECT_FORM)
    page.evaluate(
        """() => {
          window.__clicks = [];
          window.__typed = [];
          document.querySelectorAll('.select__option').forEach((o) =>
            o.addEventListener('mousedown', () => window.__clicks.push(o.textContent)));
          document.querySelector('.select__input')
            .addEventListener('input', (e) => window.__typed.push(e.target.value));
        }"""
    )
    ref = field_by_label(driver.snapshot(), "Are you legally authorized to work?")["ref"]

    result = driver.type(ref, "Maybe")

    assert result["ok"] is True and result["selected"] is None
    assert page.evaluate("() => window.__clicks") == []
    assert page.evaluate("() => window.__typed") == []  # dropdowns are never typed at
    assert page.eval_on_selector(".select__input", "el => el.value") == ""
    assert "the menu offers" in result["note"]
    assert "Yes, I am legally authorized to work" in result["note"]
    assert page.locator(".select__single-value").count() == 0
    assert field_by_label(driver.snapshot(), "Are you legally authorized to work?")["value"] == ""


def test_snapshot_restamps_clean_so_a_ref_names_one_element(page: Any) -> None:
    """RUN 82. An element invisible during a re-stamp kept its stale ref, and
    once visible again TWO inputs answered to "f17" — the verify read the wrong
    twin, called a landed selection empty, and the model re-filled a set field
    forever. Every snapshot must wipe all stamps before restamping."""
    driver = driver_for(page, PLAIN_FORM)
    first = driver.snapshot()
    hidden_ref = first["fields"][0]["ref"]
    # hide the first field, snapshot (it keeps no stamp), then show it again
    page.eval_on_selector(
        f'[data-weaver-ref="{hidden_ref}"]', "el => { el.style.display = 'none'; }"
    )
    driver.snapshot()
    page.evaluate(
        """() => {
          const el = document.querySelector('input, select, textarea');
          el.style.display = '';
        }"""
    )
    final = driver.snapshot()

    refs = [f["ref"] for f in final["fields"]]
    assert len(refs) == len(set(refs))
    for ref in refs:
        assert page.locator(f'[data-weaver-ref="{ref}"]').count() == 1


def test_a_declared_phrase_matches_its_option_by_leading_token(page: Any) -> None:
    """RUN 79 f15. The applicant declares "White / Caucasian"; the option reads
    "White - A person having origins…". The preloaded menu is read on open and
    the leading-token match lands the right option — no keystrokes involved
    (typing the phrase used to filter the menu to nothing)."""
    driver = driver_for(page, RACE_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Race / Ethnicity")["ref"]
    page.evaluate(
        """() => {
          window.__typed = [];
          document.querySelector('.select__input')
            .addEventListener('input', (e) => window.__typed.push(e.target.value));
        }"""
    )

    result = driver.type(ref, "White / Caucasian")

    assert page.evaluate("() => window.__typed") == []
    assert result["selected"] == "White - A person having origins in Europe"
    assert page.eval_on_selector(".select__single-value", "el => el.textContent") == (
        "White - A person having origins in Europe"
    )


def test_combo_search_text_is_the_leading_token_of_a_phrase() -> None:
    assert local_driver.combo_search_text("White / Caucasian") == "White"
    assert local_driver.combo_search_text("Vancouver, BC, Canada") == "Vancouver,"
    assert local_driver.combo_search_text("Man") == "Man"  # a single token is typed whole
    assert local_driver.combo_search_text("") == ""
    # a one-letter lead filters nothing useful — carry the next token with it
    assert local_driver.combo_search_text("I consent to data retention") == "I consent"


# Webflow-style (run 81): the search box NEVER filters to a match — any typed
# token hides every option and renders a "No options" notice. The only way to
# fill it is to read the open menu and click.
NONFILTER_SELECT_FORM = """
<!doctype html><html><body>
  <div class="select" id="q-gender">
    <div id="lbl-gender">Gender</div>
    <div class="select__control" aria-expanded="false">
      <div class="select__value-container"></div>
      <div class="select__input-container">
        <input class="select__input" id="gender-input" role="combobox"
               aria-autocomplete="list" aria-labelledby="lbl-gender" autocomplete="off">
      </div>
    </div>
    <div class="select__menu" role="listbox" hidden>
      <div class="select__option" role="option">Man</div>
      <div class="select__option" role="option">Woman</div>
      <div class="select__option" role="option">I don't wish to answer</div>
      <div class="select__notice" hidden>No options</div>
    </div>
  </div>
<script>
  const control = document.querySelector('.select__control');
  const menu = document.querySelector('.select__menu');
  const input = document.querySelector('.select__input');
  const values = document.querySelector('.select__value-container');
  const notice = document.querySelector('.select__notice');
  const open = () => { menu.hidden = false; control.setAttribute('aria-expanded', 'true'); };
  const close = () => { menu.hidden = true; control.setAttribute('aria-expanded', 'false'); };
  control.addEventListener('mousedown', open);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') open();
    if (e.key === 'Escape') close();
  });
  input.addEventListener('input', () => {
    open();
    const q = input.value.trim();
    // the broken filter: ANY search text hides every option
    menu.querySelectorAll('.select__option').forEach((o) => { o.hidden = !!q; });
    notice.hidden = !q;
  });
  menu.querySelectorAll('.select__option').forEach((option) => {
    option.addEventListener('click', () => {
      values.innerHTML = '<div class="select__single-value">' + option.textContent + '</div>';
      input.value = '';
      input.style.opacity = '0';  // react-select's hidden-search state (run 82)
      close();
    });
  });
</script>
</body></html>
"""

# The menu mounts in a body-level PORTAL: it lives under neither the field's
# ancestors nor an aria-owns target (Webflow, run 81 — the option scan offered
# "Back to jobs, Apply" because only page-wide buttons were in scope).
PORTAL_SELECT_FORM = """
<!doctype html><html><body>
  <div class="select" id="q-veteran">
    <div id="lbl-veteran">Veteran Status</div>
    <div class="select__control" aria-expanded="false">
      <div class="select__value-container"></div>
      <div class="select__input-container">
        <input class="select__input" id="veteran-input" role="combobox"
               aria-autocomplete="list" aria-labelledby="lbl-veteran" autocomplete="off">
      </div>
    </div>
  </div>
  <nav><a href="#top">Back to jobs</a><button type="button">Apply</button></nav>
<script>
  const control = document.querySelector('.select__control');
  const input = document.querySelector('.select__input');
  const values = document.querySelector('.select__value-container');
  let portal = null;
  const close = () => {
    if (portal) { portal.remove(); portal = null; }
    control.setAttribute('aria-expanded', 'false');
  };
  const open = () => {
    if (portal) return;
    portal = document.createElement('div');
    portal.className = 'portal';
    const menu = document.createElement('div');
    menu.setAttribute('role', 'listbox');
    ['I am a veteran', 'I am not a veteran', 'I don\\'t wish to answer'].forEach((text) => {
      const option = document.createElement('div');
      option.setAttribute('role', 'option');
      option.textContent = text;
      option.addEventListener('click', () => {
        values.innerHTML = '<div class="select__single-value">' + text + '</div>';
        input.value = '';
        close();
      });
      menu.appendChild(option);
    });
    portal.appendChild(menu);
    document.body.appendChild(portal);  // NOT inside .select
    control.setAttribute('aria-expanded', 'true');
  };
  control.addEventListener('mousedown', open);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') open();
    if (e.key === 'Escape') close();
  });
</script>
</body></html>
"""


def test_a_nonfiltering_select_is_filled_from_its_open_menu(page: Any) -> None:
    """RUN 81 (Webflow). Typing can NEVER fill this widget — every token
    filters to "No options". The engine must fill it anyway, by reading the
    open menu and clicking the option."""
    driver = driver_for(page, NONFILTER_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Gender")["ref"]

    result = driver.type(ref, "Man")

    assert result["selected"] == "Man"
    assert page.eval_on_selector(".select__single-value", "el => el.textContent") == "Man"
    # and the field reads back filled, so the verify agrees
    assert field_by_label(driver.snapshot(), "Gender")["value"] == "Man"


def test_a_portal_mounted_menu_is_scoped_to_not_the_page(page: Any) -> None:
    """RUN 81. The open menu lives in a body-level portal; the option scan must
    find IT — not offer the page's "Back to jobs" / "Apply" as the options."""
    driver = driver_for(page, PORTAL_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Veteran Status")["ref"]

    result = driver.type(ref, "I am not a veteran")

    assert result["selected"] == "I am not a veteran"
    assert page.eval_on_selector(".select__single-value", "el => el.textContent") == (
        "I am not a veteran"
    )


def test_harvest_options_reads_a_combo_menu_and_leaves_it_closed(page: Any) -> None:
    """Options-upfront: the loop pre-reads every closed dropdown so the model
    plans with the widget's REAL choices. Harvesting commits nothing — the menu
    is shut again and no value lands."""
    driver = driver_for(page, PORTAL_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Veteran Status")["ref"]

    options = driver.harvest_options(ref)

    assert options == ["I am a veteran", "I am not a veteran", "I don't wish to answer"]
    assert page.locator(".select__single-value").count() == 0  # nothing committed
    assert page.eval_on_selector(".select__control", 'el => el.getAttribute("aria-expanded")') == (
        "false"
    )  # menu closed again


def test_harvest_options_reads_a_native_select_without_opening_it(page: Any) -> None:
    driver = driver_for(page, NATIVE_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Do you identify as transgender?")["ref"]

    options = driver.harvest_options(ref)

    assert options == ["Yes", "No", "I prefer not to disclose"]  # no "Select…" placeholder


def test_a_menu_that_renders_late_is_re_read_not_declared_empty(page: Any) -> None:
    """RUN 79 f13. The search was "Man" — a real option — and the scan still
    reported "no option matching": it read the menu before the option list had
    been built. An empty scope is a RACE, not an answer; the driver waits and
    re-reads once before giving up."""
    driver = driver_for(page, SLOW_MENU_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Gender")["ref"]

    result = driver.type(ref, "Man")

    assert result["selected"] == "Man"
    assert result["value"] == "Man"
    assert page.eval_on_selector(".select__single-value", "el => el.textContent") == "Man"


def test_a_checkbox_reports_checked_not_its_value(page: Any) -> None:
    """RUN 79 f9: a consent box was ticked for real and the confirmation read
    "" — it read `.value`, which is "on"/"" for every checkbox, ticked or not.
    The state is `.checked`, and the box may be a 1px input inside the label the
    ref names."""
    driver = driver_for(page, HIDDEN_CONSENT_FORM)
    ref = field_by_label(driver.snapshot(), "I consent to the retention of my personal data")["ref"]

    assert driver.checkbox_state(ref) == "false"
    assert driver.click(ref)["ok"] is True
    assert page.eval_on_selector("#hidden-consent", "el => el.checked") is True
    assert driver.checkbox_state(ref) == "true"
    # the wrapper the ref can land on answers for the box inside it
    assert driver.checkbox_state("label#consent-label") == "true"
    assert driver.checkbox_state("#consent-field") == "true"
    # a value read would have said nothing at all about the tick
    assert page.eval_on_selector("#hidden-consent", "el => el.value") == "on"
    # not a checkbox, and no element: no opinion
    assert driver.checkbox_state("form") == ""
    assert driver.checkbox_state("f99") == ""


def test_click_refuses_a_disabled_or_missing_target(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)
    locked = field_by_label(driver.snapshot(), "Locked field")["ref"]

    assert driver.click(locked)["ok"] is False
    assert driver.click("f99")["ok"] is False
    assert "bad selector" in driver.click("div[[")["note"]


# ---------------------------------------------------------------------- upload


def test_upload_attaches_the_resume_to_the_input(page: Any, tmp_path: Path) -> None:
    """Renamed 2026-08-24: this form renders no chip, so the page never confirmed
    anything — the old name ("...and the page confirms it") described a check
    `upload` was not doing. It attaches, and that is now said precisely."""
    resume = tmp_path / "mira-halloway-resume.docx"
    resume.write_bytes(b"PK\x03\x04 docx bytes")
    driver = driver_for(page, PLAIN_FORM, resume_path=resume)
    driver.UPLOAD_CONFIRM_TIMEOUT_MS = 0
    ref = next(f["ref"] for f in driver.snapshot()["fields"] if f["type"] == "file")

    result = driver.upload(ref)

    assert result["ok"] is True and result["verified"] == "input-only"
    assert "attached mira-halloway-resume.docx" in result["note"]
    # no claim without verification — read the attachment back off the page
    attached = page.evaluate(local_driver.ATTACHED_JS, {"target": ref})
    assert attached == {"ok": True, "note": "attached mira-halloway-resume.docx"}


def test_upload_without_a_file_is_a_refusal_not_a_crash(page: Any, tmp_path: Path) -> None:
    driver = driver_for(page, PLAIN_FORM)
    assert driver.upload("f0")["note"] == "no resume file was configured for this run"

    gone = driver_for(page, PLAIN_FORM, resume_path=tmp_path / "nope.docx")
    assert "resume file not found" in gone.upload("f0")["note"]


def test_attached_script_reports_an_empty_input_honestly(page: Any) -> None:
    driver = driver_for(page, PLAIN_FORM)
    ref = next(f["ref"] for f in driver.snapshot()["fields"] if f["type"] == "file")

    assert page.evaluate(local_driver.ATTACHED_JS, {"target": ref}) == {
        "ok": False,
        "note": "file input stayed empty after set_input_files",
    }
    not_a_file = page.evaluate(local_driver.ATTACHED_JS, {"target": "f0"})
    assert not_a_file == {"ok": False, "note": "target is not a file input"}


# ------------------------------------------------------------------ screenshot


def test_screenshot_returns_base64_jpeg(page: Any) -> None:
    import base64

    shot = driver_for(page, PLAIN_FORM).screenshot()

    assert isinstance(shot, str) and shot
    assert base64.b64decode(shot)[:2] == b"\xff\xd8"  # JPEG SOI


# ------------------------------------------------------- native <select> route


def test_type_selects_a_native_select_wearing_combo_clothes(page: Any) -> None:
    """Run 81's killer: a real <select> with `select__input`/role=combobox. The
    combo path would send keystrokes a <select> ignores — the native route has
    to win, and it has to win WITHOUT typing."""
    driver = driver_for(page, NATIVE_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Do you identify as transgender?")["ref"]

    result = driver.type(ref, "I prefer not to disclose")

    assert result["ok"] is True
    assert result["selected"] == "I prefer not to disclose"
    assert result["value"] == "3"  # the wire value, as the old select branch reported
    assert "native select" in result["note"]
    assert page.eval_on_selector("#trans", "el => el.value") == "3"
    assert page.evaluate("() => window.__changes") == ["3"]
    assert page.evaluate("() => window.__keys") == []  # never typed at
    assert field_by_label(driver.snapshot(), "Do you identify as transgender?")["value"] == (
        "I prefer not to disclose"
    )


def test_native_select_matches_on_the_leading_token(page: Any) -> None:
    """A declared "White / Caucasian" against an option that reads "White - A
    person having origins…" — the engine's one matcher, on the native path."""
    driver = driver_for(page, NATIVE_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Race / Ethnicity")["ref"]

    result = driver.type(ref, "White / Caucasian")

    assert result["ok"] is True and result["value"] == "w"
    assert result["selected"].startswith("White - A person")


def test_native_select_refuses_a_miss_and_never_picks_the_placeholder(page: Any) -> None:
    driver = driver_for(page, NATIVE_SELECT_FORM)
    ref = field_by_label(driver.snapshot(), "Race / Ethnicity")["ref"]

    miss = driver.type(ref, "Klingon")

    assert miss["ok"] is False and miss["selected"] is None
    assert 'no option matching "Klingon"' in miss["note"]
    assert page.eval_on_selector("#race", "el => el.value") == ""
    # "Select…" is a placeholder, not an answer — it must not be matchable
    assert driver.type(ref, "Select…")["ok"] is False


def test_select_option_is_the_same_route_under_its_own_name(page: Any) -> None:
    driver = driver_for(page, NATIVE_SELECT_FORM)
    state = driver.snapshot()
    ref = field_by_label(state, "Do you identify as transgender?")["ref"]

    assert driver.select_option(ref, "No")["selected"] == "No"
    assert page.eval_on_selector("#trans", "el => el.value") == "2"
    # a disabled select is a refusal, not a crash
    dead = field_by_label(state, "Locked question")["ref"]
    assert driver.select_option(dead, "Yes")["ok"] is False


def test_native_detection_ignores_everything_that_is_not_a_select(page: Any) -> None:
    """The probe must not claim a text input or a react-select combo — those
    still type."""
    driver = driver_for(page, PLAIN_FORM)
    ref = field_by_label(driver.snapshot(), "First name *")["ref"]

    assert page.evaluate(local_driver.NATIVE_SELECT_JS, {"target": ref}) is None
    assert page.evaluate(local_driver.NATIVE_SELECT_JS, {"target": "#nope"}) is None

    combo = driver_for(page, REACT_SELECT_FORM)
    combo_ref = field_by_label(combo.snapshot(), "Are you legally authorized to work?")["ref"]
    assert page.evaluate(local_driver.NATIVE_SELECT_JS, {"target": combo_ref}) is None


# ------------------------------------------------------ the human-audit window


def test_wait_for_human_holds_the_window_until_the_human_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit_pending with no tty used to return instantly and the window shut in
    Leo's face. With no Enter to wait for, the CLOSE is the signal."""

    class FakePage:
        def __init__(self) -> None:
            self.polls = 0

        def is_closed(self) -> bool:
            self.polls += 1
            return self.polls >= 3

    monkeypatch.setattr(local_driver, "AUDIT_POLL_MS", 0.0)
    monkeypatch.setattr(local_driver.sys, "stdin", None)
    page = FakePage()

    local_driver.wait_for_human(page=page)

    assert page.polls == 3


def test_wait_for_human_prompts_a_tty_and_never_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty:
        def isatty(self) -> bool:
            return True

    prompts: list[str] = []
    monkeypatch.setattr(local_driver, "AUDIT_POLL_MS", 0.0)
    monkeypatch.setattr(local_driver.sys, "stdin", Tty())
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt))

    local_driver.wait_for_human(page=object())  # no is_closed → would crash if polled

    assert prompts and "still open" in prompts[0]
    # no tty AND no page: nothing to wait for, and no hang
    monkeypatch.setattr(local_driver.sys, "stdin", None)
    local_driver.wait_for_human()


def test_launch_holds_the_window_open_on_the_audit_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam itself: keep_open() true → pause runs BEFORE anything closes,
    and the close order is context then browser."""
    events: list[str] = []

    class FakePage:
        url = "https://boards.example.test/apply"

        def goto(self, *_a: Any, **_k: Any) -> None:
            events.append("goto")

    class FakeContext:
        def new_page(self) -> FakePage:
            return FakePage()

        def close(self) -> None:
            events.append("context.close")

    class FakeBrowser:
        def new_context(self, **_k: Any) -> FakeContext:
            return FakeContext()

        def close(self) -> None:
            events.append("browser.close")

    class FakePW:
        chromium = type("C", (), {"launch": staticmethod(lambda **_k: FakeBrowser())})()

        def __enter__(self) -> "FakePW":
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

    import playwright.sync_api as sync_api

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: FakePW())
    monkeypatch.setattr(local_driver, "sleep_ms", lambda _ms: None)

    with local_driver.launch(
        "https://boards.example.test/apply",
        headless=False,
        keep_open=lambda: True,
        pause=lambda: events.append("paused for the human"),
    ) as driver:
        events.append("body")
        assert isinstance(driver, LocalDriver)

    assert events == ["goto", "body", "paused for the human", "context.close", "browser.close"]


def test_a_held_tab_run_exits_at_once_and_leaves_its_tab_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tab mode + audit seam: the run used to block in wait_for_human until a
    human closed the tab, so the ledger row was never written and the next
    queued run starved. The tab-host outlives the process — teardown must NOT
    pause, must NOT close the held tab, and must only disconnect."""
    events: list[str] = []

    class FakePage:
        url = "https://boards.example.test/apply"

        def goto(self, *_a: Any, **_k: Any) -> None:
            events.append("goto")

        def close(self) -> None:
            events.append("page.close")

    class FakeContext:
        def new_page(self) -> FakePage:
            return FakePage()

    class FakeBrowser:
        contexts = [FakeContext()]

        def close(self) -> None:
            events.append("browser.close")

    class FakePW:
        chromium = type(
            "C", (), {"connect_over_cdp": staticmethod(lambda _url: FakeBrowser())}
        )()

        def __enter__(self) -> "FakePW":
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

    import playwright.sync_api as sync_api

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: FakePW())
    monkeypatch.setattr(local_driver, "sleep_ms", lambda _ms: None)
    monkeypatch.setattr(
        local_driver,
        "wait_for_human",
        lambda *a, **k: events.append("waited for the human"),
    )

    with local_driver.launch(
        "https://boards.example.test/apply",
        cdp_url="http://127.0.0.1:9",
        keep_open=lambda: True,
        pause=lambda: events.append("paused for the human"),
    ) as driver:
        events.append("body")
        assert isinstance(driver, LocalDriver)

    # no pause, no wait, tab left open — only the CDP disconnect
    assert events == ["goto", "body", "browser.close"]


def test_a_finished_tab_run_still_closes_its_own_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tab mode without a hold (submitted/failed): the run's tab is litter —
    teardown closes it, then disconnects, and the shared browser lives on."""
    events: list[str] = []

    class FakePage:
        url = "https://boards.example.test/apply"

        def goto(self, *_a: Any, **_k: Any) -> None:
            events.append("goto")

        def close(self) -> None:
            events.append("page.close")

    class FakeContext:
        def new_page(self) -> FakePage:
            return FakePage()

    class FakeBrowser:
        contexts = [FakeContext()]

        def close(self) -> None:
            events.append("browser.close")

    class FakePW:
        chromium = type(
            "C", (), {"connect_over_cdp": staticmethod(lambda _url: FakeBrowser())}
        )()

        def __enter__(self) -> "FakePW":
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

    import playwright.sync_api as sync_api

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: FakePW())
    monkeypatch.setattr(local_driver, "sleep_ms", lambda _ms: None)

    with local_driver.launch(
        "https://boards.example.test/apply",
        cdp_url="http://127.0.0.1:9",
        keep_open=lambda: False,
    ) as driver:
        events.append("body")
        assert isinstance(driver, LocalDriver)

    assert events == ["goto", "body", "page.close", "browser.close"]


# ----------------------------------------------------------------- no browser


def test_selector_for_maps_refs_and_passes_selectors_through() -> None:
    assert local_driver.selector_for("f3") == '[data-weaver-ref="f3"]'
    assert local_driver.selector_for(" b12 ") == '[data-weaver-ref="b12"]'
    assert local_driver.selector_for("#resume") == "#resume"
    assert local_driver.selector_for("f3a") == "f3a"


# The two upload widgets exactly as Greenhouse renders them: a bare "Attach"
# button per section, the real question in a heading ABOVE the section (run 83
# attached the resume into the Cover Letter box because both inputs snapshot
# as "Attach").
FILE_SECTIONS_FORM = """
<!doctype html><html><body>
  <form>
    <div class="section">
      <div class="heading">Resume/CV *</div>
      <div class="upload">
        <button type="button">Attach</button>
        <input name="resume_file" type="file" style="display:none">
      </div>
    </div>
    <div class="section">
      <div class="heading">Cover Letter</div>
      <div class="upload">
        <button type="button">Attach</button>
        <input name="cover_file" type="file" style="display:none">
      </div>
    </div>
  </form>
</body></html>
"""


def _file_fields(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in state["fields"] if f["type"] == "file"}


def test_file_inputs_are_labelled_by_their_section_heading(page: Any) -> None:
    driver = driver_for(page, FILE_SECTIONS_FORM)
    files = _file_fields(driver.snapshot())

    assert "Resume/CV" in files["resume_file"]["label"]
    assert "Cover Letter" in files["cover_file"]["label"]


def test_upload_attaches_the_cover_letter_to_the_cover_input(page: Any, tmp_path: Path) -> None:
    resume = tmp_path / "resume.docx"
    cover = tmp_path / "cover.docx"
    resume.write_bytes(b"resume")
    cover.write_bytes(b"cover")
    driver = driver_for(page, FILE_SECTIONS_FORM, resume_path=resume)
    driver.cover_path = str(cover)
    files = _file_fields(driver.snapshot())

    result = driver.upload(files["cover_file"]["ref"])

    assert result["ok"] is True
    assert "cover.docx" in result["note"] and "(cover letter)" in result["note"]
    attached = page.eval_on_selector('input[name="cover_file"]', "el => el.files[0] ? el.files[0].name : ''")
    assert attached == "cover.docx"
    # and the resume input stayed empty
    assert page.eval_on_selector('input[name="resume_file"]', "el => el.files.length") == 0


def test_upload_never_puts_the_resume_into_a_cover_input(page: Any, tmp_path: Path) -> None:
    """RUN 83. No cover file configured: the cover input is a refusal (it is
    optional — empty beats a wrong document), never a resume fall-through."""
    resume = tmp_path / "resume.docx"
    resume.write_bytes(b"resume")
    driver = driver_for(page, FILE_SECTIONS_FORM, resume_path=resume)
    files = _file_fields(driver.snapshot())

    result = driver.upload(files["cover_file"]["ref"])

    assert result["ok"] is False
    assert "COVER LETTER" in result["note"] and "leave it empty" in result["note"]
    assert page.eval_on_selector('input[name="cover_file"]', "el => el.files.length") == 0

    landed = driver.upload(files["resume_file"]["ref"])
    assert landed["ok"] is True and "resume.docx" in landed["note"]


# Ashby-style segmented Yes/No pairs and radio cards (runs 85/87): bare
# adjacent buttons whose only question lives in a text block above the group.
# Faithfully hostile: the buttons carry NO type attribute (inside a form that
# defaults them to type=submit — run 87's hold gate blocked every answer for
# it) and the radio inputs hold value="on" with their words in the <label>.
SEGMENTED_FORM = """
<!doctype html><html><body>
  <form>
    <div class="question">
      <div class="prompt">Are you legally authorized to work in the United States?*</div>
      <div class="pair">
        <button>Yes</button>
        <button>No</button>
      </div>
    </div>
    <div class="question">
      <div class="prompt">Will you now or in the future require sponsorship to work within the United States?*</div>
      <div class="pair">
        <button>Yes</button>
        <button>No</button>
      </div>
    </div>
    <div class="question">
      <div class="prompt">Are you comfortable working in-person at our office at least 2 days/week?*</div>
      <div>
        <label><input type="radio" name="office" style="opacity:0;width:24px;height:24px">No | Would prefer to work remotely</label>
        <label><input type="radio" name="office" style="opacity:0;width:24px;height:24px">Yes | Currently located nearby</label>
      </div>
    </div>
    <button type="submit">Submit Application</button>
  </form>
<script>
  document.querySelectorAll('.pair').forEach((pair) => {
    pair.querySelectorAll('button').forEach((btn) => {
      btn.setAttribute('aria-pressed', 'false');
      btn.addEventListener('click', (e) => {
        e.preventDefault();  // what Ashby's handler does — answer, not submit
        pair.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', 'false'));
        btn.setAttribute('aria-pressed', 'true');
      });
    });
  });
</script>
</body></html>
"""


def test_segmented_answer_buttons_are_bound_to_their_questions(page: Any) -> None:
    driver = driver_for(page, SEGMENTED_FORM)
    state = driver.snapshot()
    pairs: dict[str, list[dict[str, Any]]] = {}
    for b in state["buttons"]:
        if b["text"] in ("Yes", "No"):
            pairs.setdefault(b.get("question") or "(none)", []).append(b)

    questions = sorted(pairs)
    assert any("legally authorized" in q for q in questions)
    assert any("sponsorship" in q for q in questions)
    # each pair holds exactly its own Yes and No
    for q, members in pairs.items():
        assert sorted(m["text"] for m in members) == ["No", "Yes"], q
    # the radio inputs land as FIELDS, labelled by their option text and
    # bound to their question (their value="on" is never shown)
    radio_fields = [f for f in state["fields"] if f["type"] == "radio"]
    assert any("prefer to work remotely" in f["label"] for f in radio_fields)
    assert all("in-person" in (f.get("question") or "") for f in radio_fields)
    # the submit button is never dressed as an answer
    submit = next(b for b in state["buttons"] if b["text"] == "Submit Application")
    assert "question" not in submit


def test_clicking_a_segmented_button_reports_its_landed_state(page: Any) -> None:
    driver = driver_for(page, SEGMENTED_FORM)
    state = driver.snapshot()
    yes = next(
        b for b in state["buttons"]
        if b["text"] == "Yes" and "legally authorized" in (b.get("question") or "")
    )

    result = driver.click(yes["ref"])

    assert result["ok"] is True
    assert result.get("pressed") == "true"
    assert "state: true" in result["note"]
    assert page.eval_on_selector(
        ".pair button", 'el => el.getAttribute("aria-pressed")'
    ) == "true"


def test_cdp_attach_opens_a_tab_and_leaves_the_shared_browser_alive(tmp_path: Path) -> None:
    """Tab mode: `launch(cdp_url=...)` attaches to a running browser, works in
    a TAB of its window, and teardown disconnects without killing the shared
    browser — a second run must still be able to attach afterwards.

    Runs in its own THREAD: the module's browser fixture holds a sync
    playwright open on the main thread, and the sync API allows one per
    thread. Production tab runs are separate processes, same isolation.
    """
    import socket
    import subprocess
    import sys
    import threading
    import time as _time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    chromium_path = subprocess.run(
        [
            sys.executable,
            "-c",
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as pw: print(pw.chromium.executable_path)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    if not chromium_path:
        pytest.skip("could not resolve the chromium executable")

    host = subprocess.Popen(
        [
            chromium_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--headless=new",
            "--no-first-run",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[BaseException] = []

    def body() -> None:
        cdp = f"http://127.0.0.1:{port}"
        deadline = _time.monotonic() + 20
        while _time.monotonic() < deadline:
            with socket.socket() as ping:
                ping.settimeout(0.3)
                try:
                    ping.connect(("127.0.0.1", port))
                    break
                except OSError:
                    _time.sleep(0.3)
        with local_driver.launch(
            "data:text/html,<form><input aria-label='Email'></form>", cdp_url=cdp
        ) as driver:
            state = driver.snapshot()
            assert any(f["label"] == "Email" for f in state["fields"])
        # the shared browser survived the first run's teardown
        with local_driver.launch(
            "data:text/html,<form><input aria-label='Phone'></form>", cdp_url=cdp
        ) as driver:
            assert any(f["label"] == "Phone" for f in driver.snapshot()["fields"])

    try:
        def run_body() -> None:
            try:
                body()
            except BaseException as exc:  # noqa: BLE001 — repropagated below
                failures.append(exc)

        worker = threading.Thread(target=run_body)
        worker.start()
        worker.join(timeout=120)
        assert not worker.is_alive(), "cdp test body hung"
        if failures:
            raise failures[0]
    finally:
        host.terminate()
        host.wait(timeout=10)


def test_ensure_tab_host_attaches_to_an_already_listening_port(tmp_path: Path) -> None:
    """`apply --tab` must reuse a live host, never stack a second browser."""
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        url = local_driver.ensure_tab_host(tmp_path, port=port, wait_s=1)
        assert url == f"http://127.0.0.1:{port}"
        assert not (tmp_path / "tab-host.log").exists()  # nothing was spawned
    finally:
        server.close()


# --------------------------------------------------------------------
# IFRAME / cross-origin embedded application form (Greenhouse, Lever, ...)
# --------------------------------------------------------------------


# The application form lives in a child frame's OWN document. Under the
# Same-Origin Policy the TOP document (and the old snapshot script, which ran
# only in it) can never see these controls — the driver must enumerate the
# page's frames, snapshot the form frame inside ITS document, and route every
# fill/click into that frame. The iframe is hosted by `srcdoc`, whose document
# is a separate frame equally invisible to a top-document querySelectorAll.
IFRAME_FORM = """
<!doctype html><html><head><meta charset="utf-8"></head><body>
  <h1>Pinterest &middot; Visual Designer, Flagship</h1>
  <p>The application lives in the embedded frame below.</p>
  <iframe id="gh" title="application" srcdoc='<!doctype html><html><body><form autocomplete="off"><h2>Application</h2><label for="em">Email *</label><input id="em" name="email" type="email" required><label for="fn">First name *</label><input id="fn" name="first_name" required><label for="st">What U.S. state do you currently reside in? *</label><select id="st" name="state"><option value="">Select&hellip;</option><option value="CA">California</option><option value="WA">Washington</option><option value="OR">Oregon</option></select><label><input id="re" type="checkbox" name="relocate"> Willing to relocate *</label><button type="submit">Submit Application</button></form></body></html>'></iframe>
</body></html>
"""


def test_an_iframe_embedded_application_form_is_driven_inside_its_frame(
    page: Any, tmp_path: Path
) -> None:
    """IFRAME / cross-origin embed: the form is in a child frame's document,
    unreachable from the top page (SOP). The driver must enumerate frames,
    snapshot the form frame, stamp globally-unique refs, and route every fill
    and click into that frame — never the top document."""
    driver = driver_for(page, IFRAME_FORM)

    state = driver.snapshot()
    # Every field below is inside the iframe's document — a top-document-only
    # snapshot could never have seen them (the pre-frame bug this guards).
    email = field_by_label(state, "Email *")
    first = field_by_label(state, "First name *")
    state_q = field_by_label(state, "What U.S. state do you currently reside in? *")
    relocate = field_by_label(state, "Willing to relocate *")

    # Refs are stamped GLOBALLY unique across every frame (so an iframe field
    # can never collide with a top-page twin).
    assert email["ref"] != first["ref"]
    # …and each iframe field resolves to the CHILD frame, not the top page.
    for field in (email, first, state_q, relocate):
        assert driver._ref_frame[field["ref"]] is not page.main_frame

    # type() routes into the iframe's document and the value is re-read there.
    assert driver.type(email["ref"], "leo@example.com")["ok"] is True
    assert field_by_label(driver.snapshot(), "Email *")["value"] == "leo@example.com"
    assert driver.type(first["ref"], "Mira")["ok"] is True
    assert field_by_label(driver.snapshot(), "First name *")["value"] == "Mira"

    # A native <select> inside the frame is SELECTED and verified inside the frame.
    assert driver.type(state_q["ref"], "Washington")["ok"] is True
    assert field_by_label(
        driver.snapshot(), "What U.S. state do you currently reside in? *"
    )["value"] == "Washington"

    # click() routes into the frame's checkbox and state is read back in-frame.
    assert driver.click(relocate["ref"])["ok"] is True
    assert driver.checkbox_state(relocate["ref"]) == "true"

    # Buttons inside the frame are surfaced to the model, not hidden by SOP.
    assert any(b["text"] == "Submit Application" for b in driver.snapshot()["buttons"])


# ------------------------------------------- upload verification (hard rule 2)

#: An ATS that ACCEPTS the file names it back — greenhouse/lever/ashby all
#: render a chip from their change handler. This is the only signal that says
#: the ATS (not merely the DOM) took the file.
CHIP_FORM = """
<!doctype html><html><body>
  <form>
    <label>Resume/CV</label>
    <div class="upload">
      <input id="resume" name="resume" type="file">
      <div id="chip"></div>
    </div>
  </form>
  <script>
    document.querySelector('#resume').addEventListener('change', (ev) => {
      const f = ev.target.files && ev.target.files[0];
      document.querySelector('#chip').textContent = f ? ('Attached: ' + f.name) : '';
    });
  </script>
</body></html>
"""

#: The run-145 shape: the input takes the file, the page never acknowledges it.
#: A dropzone whose handler is broken/blocked looks EXACTLY like this.
SILENT_FORM = """
<!doctype html><html><body>
  <form>
    <label>Resume/CV</label>
    <div class="upload"><input id="resume" name="resume" type="file"></div>
  </form>
</body></html>
"""


def _file_ref(driver: LocalDriver) -> str:
    return next(f["ref"] for f in driver.snapshot()["fields"] if f["type"] == "file")


def test_upload_confirmed_by_the_page_is_marked_rendered(page: Any, tmp_path: Path) -> None:
    resume = tmp_path / "mira-halloway-resume.docx"
    resume.write_bytes(b"PK\x03\x04 docx bytes")
    driver = driver_for(page, CHIP_FORM, resume_path=resume)

    result = driver.upload(_file_ref(driver))

    assert result["ok"] is True
    assert result["verified"] == "rendered"
    assert "mira-halloway-resume.docx" in result["note"]


def test_upload_without_page_confirmation_is_input_only(page: Any, tmp_path: Path) -> None:
    """Run 145's real state. It stays ok (plenty of ATSes render no chip) but it
    must NOT be indistinguishable from a confirmed attach."""
    resume = tmp_path / "mira-halloway-resume.docx"
    resume.write_bytes(b"PK\x03\x04 docx bytes")
    driver = driver_for(page, SILENT_FORM, resume_path=resume)
    driver.UPLOAD_CONFIRM_TIMEOUT_MS = 0  # do not spend the real poll in tests

    result = driver.upload(_file_ref(driver))

    assert result["ok"] is True
    assert result["verified"] == "input-only"
    assert "never named it back" in result["note"]


def test_upload_that_does_not_stick_is_not_ok(page: Any, tmp_path: Path) -> None:
    """Run 145's LIE: set_input_files returned without raising and the engine
    reported `ok=true — attached …docx` on a form that had no resume. A
    non-raising attach is not evidence; the input holding the file is."""
    resume = tmp_path / "mira-halloway-resume.docx"
    resume.write_bytes(b"PK\x03\x04 docx bytes")
    driver = driver_for(page, SILENT_FORM, resume_path=resume)
    ref = _file_ref(driver)

    real_frame_for = driver._frame_for

    class _LyingFrame:
        """Accepts the attach, drops the file — a wall/challenge mid-attach."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def set_input_files(self, *_a: Any, **_k: Any) -> None:
            return None  # "succeeded", attached nothing

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    driver._frame_for = lambda target: _LyingFrame(real_frame_for(target))  # type: ignore[method-assign]
    result = driver.upload(ref)

    assert result["ok"] is False
    assert "did not stick" in result["note"]
    assert "verified" not in result


def test_the_inputs_own_fakepath_value_is_not_page_confirmation(
    page: Any, tmp_path: Path
) -> None:
    """Every successful attach leaves the basename in the input's OWN value —
    Chrome sets it to "C:\\fakepath\\<name>". A confirmation probe that scanned
    input values would therefore call every silent form confirmed, which is the
    exact false-success this patch exists to kill. Only text the PAGE rendered
    counts."""
    resume = tmp_path / "mira-halloway-resume.docx"
    resume.write_bytes(b"PK\x03\x04 docx bytes")
    driver = driver_for(page, SILENT_FORM, resume_path=resume)
    ref = _file_ref(driver)
    driver.UPLOAD_CONFIRM_TIMEOUT_MS = 0
    driver.upload(ref)

    # the vector is real — guard against the premise rotting
    value = page.evaluate("() => document.querySelector('#resume').value")
    assert "mira-halloway-resume.docx" in value
    # ...and the probe must still report no page-level confirmation
    echo = page.evaluate(
        local_driver.UPLOAD_CONFIRMED_JS,
        {"target": ref, "name": "mira-halloway-resume.docx"},
    )
    assert not echo


def test_a_filename_stem_matching_the_forms_own_label_is_not_confirmation(
    page: Any, tmp_path: Path
) -> None:
    """Caught while building this patch: matching the filename STEM made
    "cover.docx" confirm against the page's own "Cover letter" label, and
    "resume.docx" would confirm against "Resume/CV". That is a false success on
    every form, which is precisely what upload verification exists to prevent.
    Only the full filename counts."""
    cover = tmp_path / "cover.docx"
    cover.write_bytes(b"cover")
    page.set_content(
        '<form><div><label>Cover letter</label>'
        '<input id="c" name="c" type="file"></div></form>'
    )
    page.set_input_files("#c", str(cover))

    echo = page.evaluate(
        local_driver.UPLOAD_CONFIRMED_JS, {"target": "#c", "name": "cover.docx"}
    )
    assert not echo, f"the label matched the stem and posed as confirmation: {echo!r}"

    # ...while a real chip carrying the full filename still confirms
    page.evaluate(
        "() => { const d = document.createElement('div');"
        " d.textContent = 'Attached: cover.docx';"
        " document.querySelector('form div').appendChild(d); }"
    )
    assert page.evaluate(
        local_driver.UPLOAD_CONFIRMED_JS, {"target": "#c", "name": "cover.docx"}
    )
