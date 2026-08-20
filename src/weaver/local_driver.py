"""Local Playwright driver — the same page scripts, a real Chromium on this Mac.

The worker drives Cloudflare Browser Rendering over a websocket; this module
drives a local headless Chromium instead. Everything else is identical on
purpose: the JS below is the verbatim body of `worker/src/page-scripts.ts`
(types stripped, nothing else), so a snapshot taken here and a snapshot taken
in the worker describe the page the same way — same refs, same labels, same
confirmation heuristic.

The driver never decides anything. It evaluates a script and reports what the
page said, including "no" (`{ok: False, note: ...}`). `local_agent.py` owns the
loop.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

#: Confirmation phrases — verbatim from page-scripts.ts CONFIRM_PATTERNS.
CONFIRM_PATTERNS: list[str] = [
    "application received",
    "application submitted",
    "application complete",
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "thank you for your interest in",
    "we have received your application",
    "we've received your application",
    "your application has been",
    "submitted successfully",
    "successfully submitted",
    "application confirmation",
]

MAX_TEXT = 3000
MAX_FIELDS = 80
#: Give SPA forms a beat to re-render after a click.
SETTLE_MS = 1200
TYPE_SETTLE_MS = 120
#: react-select renders the filtered menu a frame or two after the keystrokes,
#: and commits the pick into `.select__single-value` a frame or two after the
#: option click. Both reads happen after their own settle or they read a widget
#: mid-render and call a landed value empty.
#:
#: Run 79 f13 lost a REAL option this way: the search text was "Man", the menu
#: held "Man", and the scan still reported "no option matching" — it scanned
#: before the menu existed. One beat is not enough on a loaded page; the option
#: scan also RE-READS once (see `_select_typed_option`) when it comes back empty.
COMBO_MENU_MS = 700
#: A real Chrome UA — headless Chromium's default UA gets bot-walled.
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1440, "height": 900}
NAV_TIMEOUT_MS = 45_000

REF_PATTERN = re.compile(r"^[fb]\d+$")


def sleep_ms(ms: float) -> None:
    time.sleep(max(0.0, ms) / 1000.0)


def norm(value: Any) -> str:
    return re.sub(r"[*:•]+", "", re.sub(r"[\s ]+", " ", str(value or "").lower())).strip()


def match_option(buttons: Iterable[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """The option that says what the applicant said. Exact, then substring.

    Lives here (rather than in `local_agent`) because `type()` now runs the same
    match itself: typing into a combo has to SELECT, and both callers must agree
    on what "Yes" matches. `local_agent.match_option` delegates to this.

    Substring matching is word-bounded on purpose: this can scan every button on
    the page, not a closed <option> list, and "No" must not select "Not now" or
    a stray "Notify me".
    """
    want = norm(wanted)
    if not want:
        return None
    buttons = list(buttons)
    for button in buttons:
        if norm(button.get("text")) == want:
            return button
    boundary = re.compile(rf"(?<!\w){re.escape(want)}(?!\w)")
    for button in buttons:
        text = norm(button.get("text"))
        if not text:
            continue
        if boundary.search(text) or re.search(rf"(?<!\w){re.escape(text)}(?!\w)", want):
            return button
    # Guardrail answers are phrases ("Yes — Canada (Vancouver, BC based)",
    # "Vancouver, BC, Canada", "White / Caucasian") while the options read
    # differently ("Yes, I am legally authorized…", "Vancouver, British
    # Columbia, Canada", "White - A person having origins…"). Match on the
    # leading token (3+ chars so "a"/"an"/"i" can't false-hit).
    first = want.split()[0] if want.split() else ""
    if len(first) >= 3:
        for button in buttons:
            text = norm(button.get("text"))
            if text and (text == first or text.startswith(first + " ")):
                return button
    return None


def combo_search_text(text: str) -> str:
    """What to TYPE into a react-select's search box to surface `text`.

    A combo filters its options by SUBSTRING of what is typed. The applicant's
    declared answers are phrases the option list never repeats verbatim — run 79
    typed "White / Caucasian" at a race question whose option reads "White - A
    person having origins…", so the filter emptied the menu and a field the model
    had answered came back "no option matching". Search the LEADING token
    ("White"); the caller keeps the full phrase as the wanted for matching and
    verification, so the widest search still picks the right option.

    A one-token lead carries no signal ("I", "A") — take two tokens then.
    """
    tokens = str(text).split()
    if len(tokens) <= 1:
        return str(text)
    if len(re.sub(r"[^0-9a-z]", "", tokens[0].lower())) < 2:
        return " ".join(tokens[:2])
    return tokens[0]


def selector_for(target: str) -> str:
    """A weaver ref (f3 / b1) becomes its stamped attribute; anything else is
    handed to `document.querySelector` as-is."""
    target = (target or "").strip()
    return f'[data-weaver-ref="{target}"]' if REF_PATTERN.match(target) else target


# --------------------------------------------------------------------------
# Page scripts. These do NOT run in python — playwright serializes them and
# evaluates them inside the browser page. Self-contained, JSON in / JSON out.
# --------------------------------------------------------------------------

SNAPSHOT_JS = r"""(options) => {
  const w = globalThis;
  const doc = w.document;
  const state = {
    url: w.location ? String(w.location.href) : '',
    title: doc && doc.title ? String(doc.title) : '',
    fields: [],
    buttons: [],
    text: '',
    confirmation: { detected: false, matched: [], snippet: '' },
  };
  if (!doc || !doc.body) return state;

  // Stale stamps are ref DRIFT: an element that is invisible during a re-stamp
  // keeps its old ref, and once it reappears TWO elements answer to the same
  // ref (run 82: the gender-identity and transgender inputs both held "f17",
  // so every verify read the wrong twin and re-filled a landed field). A
  // snapshot's stamps are valid only until the next snapshot — wipe them all
  // first so a ref names exactly one element, always.
  doc.querySelectorAll('[data-weaver-ref]').forEach((n) => n.removeAttribute('data-weaver-ref'));

  const clean = (value) =>
    String(value == null ? '' : value)
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 200);

  const visible = (el) => {
    if (!el) return false;
    if (el.type === 'hidden') return false;
    const style = w.getComputedStyle ? w.getComputedStyle(el) : null;
    if (style && (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')) {
      // A file input is routinely hidden behind a styled button — keep it.
      // A react-select search input goes opacity:0 the moment a value lands
      // (the library's hidden-search state): the FIELD is still on the form,
      // and dropping it shifts every later ref by one, so the whole page
      // drifts each time a select commits (run 82). Keep it too.
      const selectInput = style.opacity === '0' && el.closest
        && el.closest('.select__control, .select__input-container');
      // Custom-styled radios/checkboxes hide the real input at opacity 0 and
      // paint their own dot on top (Ashby's are 24×24 and fully transparent —
      // run 88 dropped them from the field list and the model never saw the
      // question's options at all). A transparent box with real geometry is a
      // styled CONTROL, not an invisible field.
      const styledBox = style.opacity === '0'
        && /^(radio|checkbox)$/i.test(String(el.type || ''))
        && (() => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })();
      if (el.type !== 'file' && !selectInput && !styledBox) return false;
      if (selectInput) {
        const ctrl = el.closest('.select__control') || el.closest('.select__input-container');
        const r = ctrl.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }
    }
    if (el.type === 'file') return true;
    const rects = el.getClientRects ? el.getClientRects() : [];
    return rects.length > 0 || el.offsetParent !== null;
  };

  const labelledByText = (el) => {
    const labelledBy = el && el.getAttribute && el.getAttribute('aria-labelledby');
    if (!labelledBy) return '';
    const parts = String(labelledBy)
      .split(/\s+/)
      .map((id) => {
        const node = doc.getElementById(id);
        return node ? clean(node.textContent) : '';
      })
      .filter(Boolean);
    return parts.length ? clean(parts.join(' ')) : '';
  };

  // react-select: the input's only visible "label" is the PLACEHOLDER, and the
  // placeholder is destroyed the moment a value lands (.select__single-value
  // takes its slot). A label read from it flips to '' — or to the answer —
  // after selection, the field goes anonymous, and the post-click lookup that
  // verifies the write can no longer find it. Resolve from sources that
  // survive a selection instead, so the label is identical before and after.
  const comboLabelFor = (el) => {
    const ctrl = el.closest ? el.closest('.select__control, .select__input-container') : null;
    if (!ctrl) return '';
    const control = (el.closest && el.closest('.select__control')) || ctrl;
    const viaControl = labelledByText(control) || labelledByText(ctrl);
    if (viaControl) return viaControl;
    if (el.id) {
      const explicit = doc.querySelector(`label[for="${String(el.id).replace(/"/g, '\\"')}"]`);
      const text = explicit ? clean(explicit.textContent) : '';
      if (text) return text;
    }
    // The question container (item-editor / field wrapper): its prose is the
    // question, which persists. Read it with the widget itself subtracted so
    // the chosen value can never leak into the label.
    let node = control.parentElement;
    let hops = 0;
    while (node && hops < 4) {
      const copy = node.cloneNode ? node.cloneNode(true) : null;
      if (copy && copy.querySelectorAll) {
        copy.querySelectorAll('.select__control, .select__menu, input, textarea, select').forEach(
          (n) => n.remove(),
        );
        const text = clean(copy.textContent);
        if (text && text.length < 120) return text;
      }
      node = node.parentElement;
      hops++;
    }
    // Last resort only — both are transient-ish, but better than anonymous.
    return clean(el.name) || clean(el.getAttribute && el.getAttribute('placeholder'));
  };

  // Greenhouse renders BOTH upload widgets as a bare "Attach" button; the real
  // question ("Resume/CV", "Cover Letter") is a heading above the section. A
  // file input whose label came out generic is re-labelled from that heading —
  // run 83 attached the resume into the cover-letter box because the two
  // inputs were indistinguishable.
  const sectionHeadingFor = (el) => {
    let node = el.parentElement;
    let hops = 0;
    while (node && hops < 5) {
      let sib = node.previousElementSibling;
      let looks = 0;
      while (sib && looks < 3) {
        const text = clean(sib.textContent);
        if (text && text.length < 60) return text;
        sib = sib.previousElementSibling;
        looks++;
      }
      node = node.parentElement;
      hops++;
    }
    return '';
  };

  const labelFor = (el) => {
    const aria = clean(el.getAttribute && el.getAttribute('aria-label'));
    if (String(el.type || '').toLowerCase() === 'file') {
      const generic = (t) => !t || /^(attach|upload|choose|browse|select)\b/i.test(t) || t.length < 4;
      if (!generic(aria)) return aria;
      const heading = sectionHeadingFor(el);
      if (heading) return heading;
    }
    if (aria) return aria;
    const labelledBy = labelledByText(el);
    if (labelledBy) return labelledBy;
    const combo = comboLabelFor(el);
    if (combo) return combo;
    if (el.labels && el.labels.length) {
      const text = clean(el.labels[0].textContent);
      if (text) return text;
    }
    if (el.id) {
      const explicit = doc.querySelector(`label[for="${String(el.id).replace(/"/g, '\\"')}"]`);
      if (explicit) {
        const text = clean(explicit.textContent);
        if (text) return text;
      }
    }
    const wrapping = el.closest ? el.closest('label') : null;
    if (wrapping) {
      const text = clean(wrapping.textContent);
      if (text) return text;
    }
    // Greenhouse/Lever style: a <div>/<span> label sitting above the control.
    let node = el.previousElementSibling;
    let hops = 0;
    while (node && hops < 3) {
      const text = clean(node.textContent);
      if (text && text.length < 120) return text;
      node = node.previousElementSibling;
      hops++;
    }
    const parent = el.parentElement;
    if (parent) {
      const text = clean(parent.textContent);
      if (text && text.length < 120) return text;
    }
    return '';
  };

  // An answer BUTTON is anonymous by itself: Ashby renders Yes/No question
  // pairs as bare adjacent <button>s and its radio cards as role=radio divs,
  // so a page carries several identical "Yes"/"No" texts and the model cannot
  // bind any of them to a question (run 85: every segmented pair went
  // unfilled). Attach the question: the nearest preceding text block above
  // the option's container.
  const questionFor = (el) => {
    let node = el.parentElement;
    let hops = 0;
    while (node && hops < 5) {
      let sib = node.previousElementSibling;
      let looks = 0;
      while (sib && looks < 4) {
        // Skip sibling OPTIONS (previous radio cards) to reach the question.
        const sibRole = String(sib.getAttribute && sib.getAttribute('role') || '');
        const optionish = /radio|option|checkbox|button/i.test(sibRole)
          || /^(BUTTON|INPUT|SELECT|TEXTAREA)$/.test(sib.tagName)
          || sib.querySelector && sib.querySelector('input, button, select, textarea, [role="radio"], [role="option"]');
        if (!optionish) {
          const text = clean(sib.textContent);
          if (text && text.length >= 8 && text.length <= 200) return text;
        }
        sib = sib.previousElementSibling;
        looks++;
      }
      node = node.parentElement;
      hops++;
    }
    return '';
  };

  // Short answer-like texts get a question; long texts are their own context.
  const withQuestion = (el, text) => {
    if (text.length > 40) return '';
    const q = questionFor(el);
    return q && q !== text ? q : '';
  };


  // Refs are stamped GLOBALLY unique across every frame: each child frame runs
  // this same script and starts its index at the accumulated count, so an
  // iframe-embedded form's refs never collide with the top document's (SOP
  // keeps each frame's document apart, so `document` here IS this frame's doc).
  let fieldIndex = options.baseF || 0;
  const controls = doc.querySelectorAll('input, select, textarea, [contenteditable="true"]');
  for (let i = 0; i < controls.length; i++) {
    const el = controls[i];
    const tagName = String(el.tagName || '').toLowerCase();
    const type = String(el.type || (tagName === 'input' ? 'text' : '')).toLowerCase();
    if (type === 'submit' || type === 'button' || type === 'image' || type === 'reset') continue;
    if (!visible(el)) continue;
    if (state.fields.length >= options.maxFields) break;

    const ref = `f${fieldIndex++}`;
    el.setAttribute('data-weaver-ref', ref);
    const field = {
      ref,
      tag: tagName === 'input' || tagName === 'select' || tagName === 'textarea' ? tagName : 'contenteditable',
      type,
      label: labelFor(el),
      name: clean(el.name),
      id: clean(el.id),
      placeholder: clean(el.getAttribute && el.getAttribute('placeholder')),
      value: (() => {
        if (type === 'checkbox' || type === 'radio') return String(!!el.checked);
        if (tagName === 'select') {
          // Report what the PAGE shows, not the wire value: `options` lists
          // labels, so a value of "they" reads as unlanded against a
          // "They/Them" answer. The selected option's text is the landed value.
          const picked = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
          if (!picked || !picked.value) return '';  // the "Select…" placeholder
          return clean(picked.textContent) || clean(picked.value);
        }
        if (tagName === 'input' || tagName === 'textarea') {
          // react-select combos: the SELECTION lives in .select__single-value
          // (or multi-value chips) — the input only carries the search text.
          // Search text is NOT a landed value.
          const ctrl = el.closest('.select__control');
          if (ctrl) {
            const sv = ctrl.querySelector('.select__single-value');
            if (sv && sv.textContent) return clean(sv.textContent);
            const chips = ctrl.querySelectorAll('.select__multi-value__label');
            if (chips.length) return clean(Array.from(chips).map((c) => c.textContent).join(', '));
            return '';
          }
          return clean(el.value);
        }
        return clean(el.textContent);
      })(),
      required: !!(el.required || (el.getAttribute && el.getAttribute('aria-required') === 'true')),
      disabled: !!el.disabled,
      // Combo-like widgets keep their options hidden until opened — flag them
      // so the loop can harvest the option list upfront for the model.
      combo: (() => {
        if (tagName === 'select' || type === 'checkbox' || type === 'radio' || type === 'file') return false;
        const cls = String(el.className || '');
        const role = String(el.getAttribute && el.getAttribute('role') || '');
        const pop = String(el.getAttribute && el.getAttribute('aria-haspopup') || '');
        const auto = String(el.getAttribute && el.getAttribute('aria-autocomplete') || '');
        return /select__input/.test(cls) || /combobox/i.test(role)
          || /list|menu|true/i.test(pop) || /list|both/i.test(auto)
          || !!(el.closest && el.closest('.select__control, .select__input-container'));
      })(),
    };
    // A single-line <input maxlength="127"> CLIPS a longer answer inside the
    // browser: the write reports the full length, the page keeps a mid-sentence
    // fragment, and nothing in the loop can tell (live test 5 — Workable renders
    // its required essay questions as maxlength-127 inputs). The limit has to
    // reach the model, or it composes past it every time.
    const maxlen = (() => {
      if (type === 'checkbox' || type === 'radio' || type === 'file' || tagName === 'select') return 0;
      const raw = el.getAttribute && el.getAttribute('maxlength');
      const n = raw == null || raw === '' ? -1 : parseInt(raw, 10);
      return Number.isFinite(n) && n > 0 ? n : 0;
    })();
    if (maxlen > 0) field.maxlength = maxlen;
    if (type === 'radio' || type === 'checkbox') {
      // A radio's label is its OPTION text ("No | Would prefer to work
      // remotely") — the question lives above the group, and without it the
      // options are as orphaned as run 87's Yes/No buttons were.
      const q = questionFor(el);
      if (q && q !== field.label) field.question = q;
    }
    if (tagName === 'select' && el.options) {
      const options = [];
      for (let j = 0; j < el.options.length && j < 60; j++) {
        options.push(clean(el.options[j].textContent) || clean(el.options[j].value));
      }
      field.options = options;
    }
    state.fields.push(field);
  }

  function isVisible(el) {
    if (el.getAttribute('aria-hidden') === 'true') return false;
    if (el.closest('[hidden]')) return false;
    const rect = el.getBoundingClientRect();
    return rect.height > 0 && rect.width > 0;
  }

  let buttonIndex = options.baseB || 0;
  // Custom dropdowns (Greenhouse/Lever) render options as role=option/radio/
  // menuitem or bare <li>. Collect the SEMANTIC options FIRST so a page full of
  // static content (nav, salary lines) can't crowd them out of the cap.
  const optionSelector =
    '[role="option"], [role="radio"], [role="menuitem"], [role="checkbox"], input[type="radio"]:not([data-weaver-ref]), input[type="checkbox"]:not([data-weaver-ref])';
  const optionEls = doc.querySelectorAll(optionSelector);
  const seenRefs = new Set();
  for (let i = 0; i < optionEls.length; i++) {
    const el = optionEls[i];
    if (!visible(el)) continue;
    // A radio/checkbox INPUT's `.value` is wire junk ("on") — its words live
    // in the associated <label> (run 87: three radio options all read "on").
    let text = '';
    if (el.tagName === 'INPUT' && el.labels && el.labels.length) {
      text = clean(el.labels[0].textContent);
    }
    if (!text && el.tagName === 'INPUT' && el.closest && el.closest('label')) {
      text = clean(el.closest('label').textContent);
    }
    if (!text) {
      const rawValue = String(el.value == null ? '' : el.value);
      text = clean((rawValue === 'on' ? '' : rawValue) || el.textContent || (el.getAttribute && el.getAttribute('aria-label')));
    }
    // Option elements carry long labels ("White - A person having origins in
    // any of the original people of Europe...") — keep the leading part so the
    // first-word match still works.
    const maxLen = /option|menuitem|radio/.test(String(el.getAttribute && el.getAttribute('role') || '')) ? 200 : 120;
    if (!text || text.length > maxLen) continue;
    if (state.buttons.length >= 60) break;
    const ref = `b${buttonIndex++}`;
    el.setAttribute('data-weaver-ref', ref);
    seenRefs.add(el);
    const entry = { ref, text, type: String(el.type || el.tagName || '').toLowerCase() };
    const q = withQuestion(el, text);
    if (q) entry.question = q;
    state.buttons.push(entry);
  }
  const clickables = doc.querySelectorAll(
    'button, input[type="submit"], input[type="button"], [role="button"], a[href], li',
  );
  for (let i = 0; i < clickables.length; i++) {
    const el = clickables[i];
    if (seenRefs.has(el)) continue;
    if (!visible(el)) continue;
    if (state.buttons.length >= 60) break;
    const text = clean(el.value || el.textContent || (el.getAttribute && el.getAttribute('aria-label')));
    // <li> items are noisy (menus, nav); only keep short ones that look like
    // option labels.
    const role = String(el.getAttribute && el.getAttribute('role') || '').toLowerCase();
    if (el.tagName === 'LI' && !role && text.length > 80) continue;
    if (!text) continue;
    const ref = `b${buttonIndex++}`;
    el.setAttribute('data-weaver-ref', ref);
    const entry = { ref, text, type: String(el.type || el.tagName || '').toLowerCase() };
    // Only short answer-shaped texts ("Yes", "No", "Male") need binding.
    // Judged by TEXT, not by the type attribute: Ashby's Yes/No pairs are
    // <button>s inside a form, so they default to type=submit (run 87) — the
    // words on the button say whether it answers or submits.
    const controlish = /submit|apply|send|continue|next|back|cancel|attach|upload/i.test(text);
    if ((el.tagName === 'BUTTON' || role === 'button') && !controlish && text.length <= 40) {
      const q = withQuestion(el, text);
      if (q) entry.question = q;
    }
    state.buttons.push(entry);
  }

  const bodyText = String(doc.body.innerText || doc.body.textContent || '').replace(/\s+/g, ' ').trim();
  state.text = bodyText.slice(0, options.maxText);

  const haystack = bodyText.toLowerCase();
  const matched = [];
  for (let i = 0; i < options.confirmPatterns.length; i++) {
    if (haystack.indexOf(options.confirmPatterns[i]) !== -1) matched.push(options.confirmPatterns[i]);
  }
  if (matched.length) {
    const at = haystack.indexOf(matched[0]);
    state.confirmation = {
      detected: true,
      matched,
      snippet: bodyText.slice(Math.max(0, at - 80), at + 240).trim(),
    };
  }
  return state;
}"""

TYPE_JS = r"""(payload) => {
  const w = globalThis;
  const doc = w.document;
  const selector = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]`
    : payload.target;
  let el = null;
  try {
    el = doc.querySelector(selector);
  } catch (err) {
    return { ok: false, note: `bad selector ${selector}: ${String(err && err.message)}` };
  }
  if (!el) return { ok: false, note: `no element for ${selector}` };
  if (el.disabled) return { ok: false, note: 'element is disabled' };

  const tag = String(el.tagName || '').toLowerCase();
  const type = String(el.type || '').toLowerCase();
  const fire = (name) => {
    el.dispatchEvent(new w.Event(name, { bubbles: true }));
  };

  if (type === 'file') {
    return { ok: false, note: 'target is a file input — use the upload action' };
  }

  if (el.focus) el.focus();

  if (tag === 'select') {
    const wanted = String(payload.text || '').trim().toLowerCase();
    let index = -1;
    for (let i = 0; i < el.options.length; i++) {
      const option = el.options[i];
      const label = String(option.textContent || '').trim().toLowerCase();
      const value = String(option.value || '').trim().toLowerCase();
      if (label === wanted || value === wanted) {
        index = i;
        break;
      }
      if (index === -1 && wanted && (label.indexOf(wanted) !== -1 || wanted.indexOf(label) !== -1)) {
        index = i;
      }
    }
    if (index === -1) return { ok: false, note: `no option matching "${payload.text}"` };
    el.selectedIndex = index;
    fire('input');
    fire('change');
    return { ok: true, note: `selected "${el.options[index].textContent}"`, value: String(el.value) };
  }

  if (type === 'checkbox' || type === 'radio') {
    const truthy = !/^(false|no|0|off|unchecked)$/i.test(String(payload.text || 'true').trim());
    if (el.checked !== truthy) el.click();
    fire('change');
    // A real boolean, not "true"/"false": `_step` stringifies it python-side so
    // the value the loop logs and compares reads the same as every other
    // python-side boolean in a step record.
    return { ok: true, note: `${type} set to ${el.checked}`, value: !!el.checked };
  }

  if (tag !== 'input' && tag !== 'textarea') {
    if (el.isContentEditable) {
      el.textContent = payload.text;
      fire('input');
      return { ok: true, note: 'set contenteditable text', value: String(el.textContent || '') };
    }
    // Custom widgets (Greenhouse-style dropdown buttons, radio cards): typing
    // does nothing — the model must click the control, then click an option.
    return {
      ok: false,
      note: 'custom control — click it to open, then click the option (use click actions, not type)',
    };
  }

  // `maxlength` is enforced against USER input only — a programmatic .value set
  // sails straight past it and reports the full length, and the framework (a
  // controlled React input re-rendering from its own state) is what clips the
  // answer, mid-sentence, afterwards. Live test 5 lost 230 characters of a
  // Workable essay answer that way with the trace reading ok. Refuse the
  // over-length write instead of leaving a clipped sentence on the page: a
  // fragment reads as garbled text on a real application, and an empty required
  // field is at least visible to the verify pass and to the human.
  const limit = (() => {
    const raw = el.getAttribute && el.getAttribute('maxlength');
    const n = raw == null || raw === '' ? -1 : parseInt(raw, 10);
    return Number.isFinite(n) && n > 0 ? n : 0;
  })();
  const wanted = String(payload.text == null ? '' : payload.text);
  if (limit && wanted.length > limit) {
    return {
      ok: false,
      maxlength: limit,
      note: `field accepts at most ${limit} characters and the value is ${wanted.length} — `
        + `it would be cut off mid-sentence; write a COMPLETE answer within ${limit} characters`,
    };
  }

  const proto = tag === 'textarea' ? w.HTMLTextAreaElement.prototype : w.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (setter && setter.set) setter.set.call(el, payload.text);
  else el.value = payload.text;
  fire('input');
  fire('change');
  if (el.blur) el.blur();
  return { ok: true, note: `typed ${String(payload.text).length} chars`, value: String(el.value || '') };
}"""

#: Is this target a NATIVE <select>, and what does it offer? Returns null for
#: anything else, so the caller can route before it decides to type.
#:
#: Run 81 died here: the transgender and consent questions are real <select>
#: boxes, the model typed at them, the keystrokes went nowhere and every field
#: came back "no option matched". A native select is never typed into — it is
#: selected. `el.options` is the tell (tagName covers the rest).
NATIVE_SELECT_JS = r"""(payload) => {
  const doc = globalThis.document;
  const selector = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]` : payload.target;
  let el = null;
  try { el = doc.querySelector(selector); } catch (err) { return null; }
  if (!el) return null;
  const tag = String(el.tagName || '').toUpperCase();
  const opts = el.options;
  const native = tag === 'SELECT' || (opts && typeof opts.length === 'number');
  if (!native || !opts) return null;
  const options = [];
  for (let i = 0; i < opts.length; i++) {
    options.push({
      ref: String(i),
      text: String(opts[i].textContent || '').replace(/\s+/g, ' ').trim(),
      value: String(opts[i].value || ''),
      disabled: !!opts[i].disabled,
    });
  }
  return {
    disabled: !!el.disabled,
    multiple: !!el.multiple,
    value: String(el.value || ''),
    options: options,
  };
}"""

#: Commit a native <select> pick by index — the API the widget actually has.
NATIVE_SELECT_PICK_JS = r"""(payload) => {
  const w = globalThis;
  const doc = w.document;
  const selector = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]` : payload.target;
  let el = null;
  try { el = doc.querySelector(selector); } catch (err) {
    return { ok: false, note: `bad selector ${selector}` };
  }
  if (!el || !el.options) return { ok: false, note: `no select for ${selector}` };
  if (el.disabled) return { ok: false, note: 'element is disabled' };
  const index = Number(payload.index);
  if (!(index >= 0) || index >= el.options.length) {
    return { ok: false, note: `option index ${payload.index} is out of range` };
  }
  if (el.focus) el.focus();
  el.selectedIndex = index;
  el.dispatchEvent(new w.Event('input', { bubbles: true }));
  el.dispatchEvent(new w.Event('change', { bubbles: true }));
  const picked = el.options[index];
  const label = String(picked.textContent || '').replace(/\s+/g, ' ').trim();
  return {
    ok: true,
    note: `selected "${label}" (native select)`,
    // `value` stays the WIRE value (what the form submits, what the old type()
    // select branch reported); `selected` is what the page shows.
    selected: label || String(picked.value || ''),
    value: String(el.value || ''),
  };
}"""

CLICK_JS = r"""(payload) => {
  const w = globalThis;
  const doc = w.document;
  const selector = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]`
    : payload.target;
  let el = null;
  try {
    el = doc.querySelector(selector);
  } catch (err) {
    return { ok: false, note: `bad selector ${selector}: ${String(err && err.message)}` };
  }
  if (!el) return { ok: false, note: `no element for ${selector}` };
  if (el.disabled) return { ok: false, note: 'element is disabled' };
  const label = String(el.value || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  if (el.scrollIntoView) el.scrollIntoView({ block: 'center' });
  el.click();
  return { ok: true, note: `clicked "${label}"` };
}"""

#: Local runs hand the file to chromium through the CDP (set_input_files), so
#: the worker's DataTransfer shim is not needed — this only reads back what
#: actually got attached, because "no claim without verification" applies here
#: too.
ATTACHED_JS = r"""(payload) => {
  const w = globalThis;
  const doc = w.document;
  const selector = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]`
    : payload.target;
  let el = null;
  try {
    el = doc.querySelector(selector);
  } catch (err) {
    return { ok: false, note: `bad selector ${selector}: ${String(err && err.message)}` };
  }
  if (!el) return { ok: false, note: `no element for ${selector}` };
  if (String(el.type || '').toLowerCase() !== 'file') {
    return { ok: false, note: 'target is not a file input' };
  }
  const attached = el.files && el.files.length ? String(el.files[0].name) : '';
  if (!attached) return { ok: false, note: 'file input stayed empty after set_input_files' };
  return { ok: true, note: `attached ${attached}` };
}"""

#: Mark the combo input so the option scan survives the snapshot: snapshotting
#: RE-STAMPS every `data-weaver-ref`, so the ref the caller passed may name a
#: different element by the time the menu has rendered.
MARK_COMBO_JS = r"""(payload) => {
  const sel = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]` : payload.target;
  let el = null;
  try { el = document.querySelector(sel); } catch (err) { return false; }
  if (!el) return false;
  Array.from(document.querySelectorAll('[data-weaver-combo]')).forEach((n) =>
    n.removeAttribute('data-weaver-combo'));
  el.setAttribute('data-weaver-combo', '1');
  return true;
}"""

#: The OPEN menu of the marked combo, read in place: option DOM ids + texts.
#: No snapshot runs here — re-stamping refs mid-transaction while a menu is
#: open is what corrupted the stamps in run 82. The menu is found through
#: aria-controls/aria-owns first (react-select links it while open), then as
#: the page's single visible listbox (portal-mounted menus). Scope is the
#: LISTBOX ONLY — never the control wrap, whose "Toggle flyout" chevron used
#: to pollute the option list.
OPEN_MENU_OPTIONS_JS = r"""() => {
  const el = document.querySelector('[data-weaver-combo="1"]');
  if (!el) return null;
  const vis = (n) => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let menu = null;
  const owned = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  if (owned) {
    const linked = document.getElementById(owned);
    if (linked && vis(linked)) menu = linked;
  }
  if (!menu) {
    const menus = Array.from(document.querySelectorAll('[role="listbox"], .select__menu')).filter(vis);
    menu = menus.length === 1 ? menus[0]
      : (menus.length && menus.every((m) => m === menus[0] || menus[0].contains(m) || m.contains(menus[0]))
        ? menus[0] : null);
  }
  if (!menu) return null;
  let nodes = Array.from(menu.querySelectorAll('[role="option"]'));
  if (!nodes.length) nodes = Array.from(menu.querySelectorAll('.select__option, li'));
  const items = [];
  for (let i = 0; i < nodes.length && items.length < 100; i++) {
    const n = nodes[i];
    if (!vis(n)) continue;
    if (String(n.getAttribute('aria-disabled')) === 'true') continue;
    if (!n.id) n.id = 'weaver-opt-' + i;
    const text = String(n.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200);
    if (text) items.push({ id: n.id, text });
  }
  return items;
}"""

#: What the widget SHOWS — the same rule the snapshot uses: a react-select's
#: value is `.select__single-value` (or the chips), never the search input.
COMBO_VALUE_JS = r"""() => {
  const clean = (v) => String(v == null ? '' : v).replace(/\s+/g, ' ').trim().slice(0, 200);
  const el = document.querySelector('[data-weaver-combo="1"]');
  if (!el) return '';
  const ctrl = el.closest('.select__control');
  if (ctrl) {
    const sv = ctrl.querySelector('.select__single-value');
    if (sv && sv.textContent) return clean(sv.textContent);
    const chips = ctrl.querySelectorAll('.select__multi-value__label');
    if (chips.length) return clean(Array.from(chips).map((c) => c.textContent).join(', '));
    return '';
  }
  return clean(el.value);
}"""

#: A checkbox's STATE, never its `.value` (which is "on"/"" for every box, ticked
#: or not). The ref may name a wrapper — Greenhouse's consent box is a visually
#: hidden 1px input inside a styled label — so search the widget too.
CHECKBOX_STATE_JS = r"""(payload) => {
  const sel = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]` : payload.target;
  let el = null;
  try { el = document.querySelector(sel); } catch (err) { return ''; }
  if (!el) return '';
  const isBox = (n) => n && n.tagName === 'INPUT'
    && /^(checkbox|radio)$/i.test(String(n.type || ''));
  if (isBox(el)) return el.checked ? 'true' : 'false';
  // The question's own scope — never the whole form, which would answer for a
  // DIFFERENT question's box.
  const scope = el.closest && el.closest('label, [data-field], .field, .form-field, fieldset');
  const boxes = (node) => Array.from(node.querySelectorAll('input')).filter(isBox);
  let inner = scope ? boxes(scope) : [];
  if (!inner.length && !/^(FORM|BODY|HTML|MAIN)$/.test(el.tagName)) {
    // An unclassed wrapper still answers when it holds exactly one box.
    const own = boxes(el);
    if (own.length === 1) inner = own;
  }
  if (inner.length) return inner.some((n) => n.checked) ? 'true' : 'false';
  const marked = (scope || el).querySelector('[aria-checked]');
  const aria = el.getAttribute('aria-checked') || (marked && marked.getAttribute('aria-checked'));
  if (aria === 'true' || aria === 'false') return aria;
  return '';
}"""

UNMARK_COMBO_JS = r"""() => Array.from(document.querySelectorAll('[data-weaver-combo]'))
  .forEach((n) => n.removeAttribute('data-weaver-combo'))"""

#: A clicked answer button's committed state — aria first, then the class the
#: widget styles its selection with. "" when the element carries no state.
PRESSED_STATE_JS = r"""(payload) => {
  const sel = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]` : payload.target;
  let el = null;
  try { el = document.querySelector(sel); } catch (err) { return ''; }
  if (!el) return '';
  const aria = el.getAttribute('aria-pressed') || el.getAttribute('aria-checked')
    || el.getAttribute('data-state') || '';
  if (aria) return String(aria);
  if (/(^|[ _-])(selected|active|checked|pressed)([ _-]|$)/i.test(String(el.className || ''))) {
    return 'selected';
  }
  return '';
}"""

CONFIRMATION_JS = r"""(payload) => {
  const w = globalThis;
  const doc = w.document;
  if (!doc || !doc.body) return '';
  const text = String(doc.body.innerText || doc.body.textContent || '').replace(/\s+/g, ' ').trim();
  const haystack = text.toLowerCase();
  for (let i = 0; i < payload.confirmPatterns.length; i++) {
    const at = haystack.indexOf(payload.confirmPatterns[i]);
    if (at !== -1) return text.slice(Math.max(0, at - 120), at + 400).trim();
  }
  return '';
}"""


#: The question text around a file input — which file does THIS input want?
NEARBY_TEXT_JS = r"""(payload) => {
  const sel = /^[fb]\d+$/.test(payload.target)
    ? `[data-weaver-ref="${payload.target}"]` : payload.target;
  let el = null;
  try { el = document.querySelector(sel); } catch (err) { return ''; }
  if (!el) return '';
  const bits = [];
  let node = el.parentElement;
  let hops = 0;
  while (node && hops < 5) {
    let sib = node.previousElementSibling;
    let looks = 0;
    while (sib && looks < 3) {
      const text = String(sib.textContent || '').replace(/\s+/g, ' ').trim();
      if (text && text.length < 80) bits.push(text);
      sib = sib.previousElementSibling;
      looks++;
    }
    node = node.parentElement;
    hops++;
  }
  return bits.join(' | ').slice(0, 300);
}"""


class LocalDriver:
    """The Driver surface of `worker/src/types.ts`, backed by a playwright page."""

    def __init__(
        self,
        page: Any,
        resume_path: str | Path | None = None,
        cover_path: str | Path | None = None,
    ) -> None:
        self.page = page
        self.resume_path = str(resume_path) if resume_path else None
        self.cover_path = str(cover_path) if cover_path else None
        #: ref -> the playwright frame that stamped it (this snapshot window).
        #: Rebuilt every snapshot; fills/clicks route into the owning frame so
        #: an iframe-embedded application form is driven inside ITS document —
        #: top-document JS cannot see a cross-origin frame's elements (SOP).
        self._ref_frame: dict[str, Any] = {}
        self._combo_frame: Any = page

    # -- frame routing (IFRAME / cross-origin application forms) -------------
    def _frame_for(self, target: str | None) -> Any:
        """The frame that owns `target`.

        A weaver ref (f0/b3) goes to the frame that stamped it during the last
        snapshot. A target-less combo helper (open-meny/value/unmark) goes to
        the frame holding the marked combo. Anything else — a raw CSS selector,
        an unknown ref, or no target — goes to the main frame, exactly like the
        pre-frame driver.
        """
        target = (target or "").strip()
        if REF_PATTERN.match(target):
            frame = self._ref_frame.get(target)
            if frame is not None:
                return frame
        if not target:
            return getattr(self, "_combo_frame", self.page)
        return self.page

    def _eval(self, script: str, arg: Any = None, target: str | None = None) -> Any:
        """Evaluate `script` against the frame that owns `target`.

        Keeps the exact `page.evaluate(script, {target, ...})` contract every
        write path relies on — same JSON in / JSON out — but resolves the
        selector inside the owning frame's document instead of always the top
        document, which cannot reach a cross-origin frame (SOP).
        """
        if target is None and isinstance(arg, dict):
            target = arg.get("target")
        return self._frame_for(target).evaluate(script, arg)

    def _locator(self, target: str, sel: str | None = None) -> Any:
        """A locator scoped to the frame that stamped `target`."""
        return self._frame_for(target).locator(sel or selector_for(target))

    # -- reads ------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """A merged snapshot of the MAIN frame plus every application-relevant
        child frame (IFRAME / cross-origin embed: Greenhouse, Lever, etc.).

        Each frame runs the same SNAPSHOT_JS inside its OWN document (SOP means
        the top document can never see a cross-origin frame's fields), starting
        its ref index at the accumulated count so refs are globally unique.
        Refs are mapped back to their owning frame so the write paths can route
        fill/click into the right document. Only frames that reported form
        chrome (fields OR buttons) are merged — inert analytics/ad iframes add
        no fields and aren't worth driving.
        """
        state: dict[str, Any] = {
            "url": self.page.url,
            "title": "",
            "fields": [],
            "buttons": [],
            "text": "",
            "confirmation": {"detected": False, "matched": [], "snippet": ""},
        }
        self._ref_frame = {}
        base_f, base_b = 0, 0
        try:
            frames = list(self.page.frames)
        except Exception:  # pragma: no cover - depends on the live page
            frames = [self.page]
        main_frame = getattr(self.page, "main_frame", self.page)
        for frame in frames:
            try:
                found = frame.evaluate(
                    SNAPSHOT_JS,
                    {
                        "maxText": MAX_TEXT,
                        "maxFields": MAX_FIELDS,
                        "confirmPatterns": CONFIRM_PATTERNS,
                        "baseF": base_f,
                        "baseB": base_b,
                    },
                )
            except Exception:
                continue
            if not isinstance(found, dict):
                continue
            fields = found.get("fields") or []
            buttons = found.get("buttons") or []
            if frame != main_frame and not (fields or buttons):
                # An inert utility iframe hosts no form chrome — skip its noise.
                continue
            for field in fields:
                self._ref_frame[str(field.get("ref") or "")] = frame
            for button in buttons:
                self._ref_frame[str(button.get("ref") or "")] = frame
            if not state["title"] and found.get("title"):
                state["title"] = found["title"]
            if not state["text"] and found.get("text"):
                state["text"] = found["text"]
            state["fields"].extend(fields)
            state["buttons"].extend(buttons)
            conf = found.get("confirmation") or {}
            if conf.get("detected") and not state["confirmation"]["detected"]:
                state["confirmation"] = {
                    "detected": True,
                    "matched": conf.get("matched") or [],
                    "snippet": conf.get("snippet") or "",
                }
            base_f += len(fields)
            base_b += len(buttons)
        # The outer (job/page) URL — a frame's location may be an embed blob.
        if state["url"]:
            state["url"] = self.page.url
        return state

    def confirm_text(self) -> str:
        for frame in list(self.page.frames):
            try:
                text = frame.evaluate(CONFIRMATION_JS, {"confirmPatterns": CONFIRM_PATTERNS})
            except Exception:
                continue
            if text:
                return str(text)
        return ""

    # `confirmationText` is the worker's name for it; keep both so a port of
    # the loop can call either.
    confirmationText = confirm_text

    def screenshot(self) -> str | None:
        try:
            return base64.b64encode(self.page.screenshot(type="jpeg", quality=40)).decode("ascii")
        except Exception:
            return None

    # -- writes -----------------------------------------------------------
    def _probe(self, script: str, arg: Any = None) -> Any:
        """A read-only page question that must never raise: a malformed selector
        from the model is a refusal (the action scripts say so in their own
        note), not a failed run."""
        try:
            return self._eval(script, arg)
        except Exception:
            return None

    def type(self, target: str, text: str) -> dict[str, Any]:
        # A native <select> is SELECTED, never typed into — and it must be
        # decided FIRST: some native selects also carry combo-ish classes/roles,
        # and the combo path below sends keystrokes a <select> ignores (run 81:
        # the transgender and consent selects, "no option matched", every time).
        native = self._probe(NATIVE_SELECT_JS, {"target": target})
        if isinstance(native, dict) and isinstance(native.get("options"), list):
            return self._select_native(target, text, native)

        # Combo-box inputs (Greenhouse's select__input / aria-haspopup) are
        # search fields: React validates every programmatic .value set against
        # the option list and CLEARS it. Real keystrokes fire React's search,
        # which is what renders the options to click.
        combo = self._probe(
            """(payload) => {
              const sel = /^[fb]\\d+$/.test(payload.target)
                ? `[data-weaver-ref="${payload.target}"]` : payload.target;
              let el = null;
              try { el = document.querySelector(sel); } catch (err) { return false; }
              if (!el) return false;
              const cls = String(el.className || '');
              const attrs = [
                el.getAttribute('aria-haspopup'), el.getAttribute('role'),
                el.getAttribute('aria-autocomplete'),
              ];
              return /select__input|combobox/.test(cls)
                || attrs.some((a) => a && /list|combobox|true/i.test(String(a)));
            }""",
            {"target": target},
        )
        if combo:
            # NEVER click here — a click toggles, and the fixer (or the model)
            # already opened the menu. ArrowDown-open if closed, then focus +
            # keystrokes only.
            self._open_combo(target)
            # If it drops down, the pick comes from the submenu — no typing in
            # dropdowns. A rendered menu is FINAL: matched → clicked; unmatched
            # → reported with the real options. Only a menu that shows nothing
            # (async lists: country, location) falls through to keystrokes.
            selected, prelisted = self._select_listed_option(target, text)
            if selected is not None:
                return selected
            try:
                self._eval(
                    """(payload) => {
                      const sel = /^[fb]\\d+$/.test(payload.target)
                        ? `[data-weaver-ref="${payload.target}"]` : payload.target;
                      const el = document.querySelector(sel);
                      if (el) el.focus();
                    }""",
                    {"target": target},
                )
            except Exception:
                pass
            sleep_ms(120)
            # Clear any leftover text first (the model may have typed before).
            # Native select() is deterministic — keyboard shortcuts can land
            # on the wrong element after ArrowDown moves the caret.
            try:
                self._eval(
                    """(payload) => {
                      const sel = /^[fb]\\d+$/.test(payload.target)
                        ? `[data-weaver-ref="${payload.target}"]` : payload.target;
                      const el = document.querySelector(sel);
                      if (el && el.select) el.select();
                    }""",
                    {"target": target},
                )
                self.page.keyboard.press("Backspace")
                sleep_ms(60)
            except Exception:
                pass
            # Combo search: react-select filters on the LEADING term — searching
            # a full phrase ("Vancouver, BC, Canada", "White / Caucasian")
            # matches no option. Search the first token; the FULL text stays the
            # wanted for the match and the verify below.
            self.page.keyboard.type(combo_search_text(text), delay=25)
            sleep_ms(TYPE_SETTLE_MS)
            return self._select_typed_option(target, text, prelisted=prelisted)

        result = self._eval(TYPE_JS, {"target": target, "text": text})
        # Custom widgets: the ref can sit on a wrapper div whose real control is
        # an inner input/textarea (Greenhouse email fields do this). Drive the
        # inner control with the React-safe setter too, and prefer its result.
        inner = self._probe(
            """(payload) => {
              const sel = /^[fb]\\d+$/.test(payload.target)
                ? `[data-weaver-ref="${payload.target}"]` : payload.target;
              let root = null;
              try { root = document.querySelector(sel); } catch (err) { return null; }
              if (!root) return { ok: false, note: "no element" };
              const el = root.querySelector && root.querySelector("input, textarea");
              if (!el || el === root) return { ok: false, note: "no inner control" };
              if (el.disabled) return { ok: false, note: "inner disabled" };
              // Same clip the outer path refuses: the wrapper's real control
              // carries the limit, and a programmatic set ignores it.
              const raw = el.getAttribute && el.getAttribute("maxlength");
              const n = raw == null || raw === "" ? -1 : parseInt(raw, 10);
              const limit = Number.isFinite(n) && n > 0 ? n : 0;
              const wanted = String(payload.text == null ? "" : payload.text);
              if (limit && wanted.length > limit) {
                return {
                  ok: false,
                  maxlength: limit,
                  note: `field accepts at most ${limit} characters and the value is ${wanted.length} — `
                    + `it would be cut off mid-sentence; write a COMPLETE answer within ${limit} characters`,
                };
              }
              const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
              if (el.focus) el.focus();
              setter.call(el, payload.text);
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
              return { ok: true, note: "set inner control", value: String(el.value) };
            }""",
            {"target": target, "text": text},
        )
        # An over-length refusal from the inner control outranks the outer
        # result: when the ref sits on a wrapper div the outer path only says
        # "custom control — click it", which sends the model clicking instead of
        # shortening the answer it actually has to shorten.
        if inner and (inner.get("ok") or inner.get("maxlength")):
            result = inner
        sleep_ms(TYPE_SETTLE_MS)
        return _step(result)

    def _select_native(
        self, target: str, text: str, native: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Pick `text` in a native <select> with the select API.

        Matching is the engine's one matcher (`match_option`): exact label, then
        word-bounded substring, then the leading token — so a declared "White /
        Caucasian" still lands on "White - A person having origins…". Labels
        first, values second (some ATS selects label by code).

        Returns the combo path's shape ({ok, selected, value}) so the loop's
        `_selection_confirmed` treats a native pick as the settled answer it is.
        No match is a refusal, not a silent pass — the caller escalates.
        """
        if native is None:
            native = self._probe(NATIVE_SELECT_JS, {"target": target})
        if not isinstance(native, dict) or not isinstance(native.get("options"), list):
            return {"ok": False, "note": f"{target} is not a native select", "selected": None, "value": ""}
        current = str(native.get("value") or "")
        if native.get("disabled"):
            return {"ok": False, "note": "element is disabled", "selected": None, "value": current}
        options = [o for o in native["options"] if isinstance(o, dict) and not o.get("disabled")]
        # A placeholder ("Select…", value "") is not an answer — never match it.
        pickable = [o for o in options if str(o.get("value") or "").strip() or str(o.get("text") or "").strip()]
        by_label = [{"ref": o.get("ref"), "text": o.get("text")} for o in pickable if str(o.get("value") or "").strip()]
        hit = match_option(by_label, text)
        if hit is None:
            by_value = [{"ref": o.get("ref"), "text": o.get("value")} for o in pickable]
            hit = match_option(by_value, text)
        if hit is None:
            return {
                "ok": False,
                "note": f'no option matching "{text}" in the native select',
                "selected": None,
                "value": current,
            }
        result = self._eval(
            NATIVE_SELECT_PICK_JS, {"target": target, "index": int(str(hit.get("ref") or 0))}
        )
        sleep_ms(TYPE_SETTLE_MS)
        step = _step(result)
        step.setdefault("selected", None)
        step.setdefault("value", current)
        return step

    # The public name the fixer (and a port of the loop) can call directly.
    def select_option(self, target: str, text: str) -> dict[str, Any]:
        return self._select_native(target, text)

    def _open_menu_options(self) -> list[dict[str, Any]]:
        """The marked combo's OPEN menu as [{id, text}], read in place.

        Never snapshots: re-stamping refs while a menu is open leaves stale
        stamps on whatever the menu hid, and two elements answering one ref is
        the drift that made run 82's verifies read the wrong twin. Options are
        anchored by their own DOM ids for the lifetime of the open menu.
        """
        found = self._probe(OPEN_MENU_OPTIONS_JS)
        return found if isinstance(found, list) else []

    def _settle_combo_value(self, timeout_ms: float = SETTLE_MS) -> str:
        """The marked combo's committed value, polled instead of slept for.

        react-select commits into `.select__single-value` a frame or two after
        the option click — but usually within ~100ms, not the full settle. A
        fixed sleep made every one of a form's selects hesitate for 1.2s
        (Leo's run-83 audit note); polling returns the moment the value lands
        and only a genuinely slow widget waits the whole window.
        """
        waited = 0.0
        while True:
            value = str(self._probe(COMBO_VALUE_JS) or "")
            if value or waited >= timeout_ms:
                return value
            sleep_ms(120)
            waited += 120

    def _mark_combo(self, target: str) -> bool:
        """Stamp the combo input as THE combo for the menu/value helpers, and
        remember which frame it lives in so the target-less helpers
        (open-menu/value/unmark) run inside the same document."""
        frame = self._frame_for(target)
        self._combo_frame = frame
        try:
            return bool(frame.evaluate(MARK_COMBO_JS, {"target": target}))
        except Exception:
            return False

    def _click_menu_option(self, dom_id: str) -> bool:
        """REAL-click an open menu's option by its DOM id (in the combo's frame)."""
        if not dom_id:
            return False
        try:
            locator = self._combo_frame.locator(f'[id="{dom_id}"]').first
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=5000)
            return True
        except Exception:
            return False

    def _select_listed_option(
        self, target: str, text: str
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Fill a dropdown FROM ITS OWN MENU — typing is never the answer here.

        If it drops down, the pick comes from the submenu (Leo's rule, run 82):
        a menu that lists options is selected from, and a wanted value no listed
        option matches is a final no-match report carrying the real options —
        NOT a license to type into the search box (Webflow's selects filter to
        "No options" on any token). `(None, [])` — no menu, or an empty one —
        is the only path to the type-to-filter fallback (async lists: country,
        location).
        """
        marked = self._mark_combo(target)
        if not marked:
            return (None, [])
        try:
            items = self._open_menu_options()
            if not items:
                sleep_ms(COMBO_MENU_MS)
                items = self._open_menu_options()
            if not items:
                return (None, [])
            texts = [str(i.get("text") or "") for i in items]
            option = match_option(
                [{"ref": i.get("id"), "text": i.get("text")} for i in items], text
            )
            if option is None:
                self._clear_combo_search(target)
                value = str(self._probe(COMBO_VALUE_JS) or "")
                offers = "; ".join(t[:60] for t in texts[:15])
                return (
                    {
                        "ok": True,
                        "note": (
                            f'no option matching "{str(text)[:40]}" — the menu offers: '
                            f"{offers}. Answer with one of these exact labels"
                        ),
                        "selected": None,
                        "value": value,
                    },
                    texts,
                )
            label = str(option.get("text") or "")
            clicked = self._click_menu_option(str(option.get("ref") or ""))
            value = self._settle_combo_value()
            if not clicked:
                self._clear_combo_search(target)
                return (
                    {
                        "ok": False,
                        "note": f'option "{label[:60]}" is listed but could not be clicked',
                        "selected": None,
                        "value": value,
                    },
                    texts,
                )
            return (
                {
                    "ok": True,
                    "note": (
                        f'opened the menu and selected "{label[:60]}" '
                        f"from {len(texts)} option(s), no typing"
                    ),
                    "selected": label,
                    "value": value,
                },
                texts,
            )
        finally:
            self._probe(UNMARK_COMBO_JS)

    def _clear_combo_search(self, target: str) -> None:
        """Remove leftover search text and close the menu — stray text renders
        "No options" forever (run 81's "Not") and blocks every later open."""
        try:
            self._eval(
                """(payload) => {
                  const sel = /^[fb]\\d+$/.test(payload.target)
                    ? `[data-weaver-ref="${payload.target}"]` : payload.target;
                  const el = document.querySelector(sel);
                  if (el && el.focus) el.focus();
                  if (el && el.select) el.select();
                }""",
                {"target": target},
            )
            self.page.keyboard.press("Backspace")
            sleep_ms(60)
            self.page.keyboard.press("Escape")
            sleep_ms(60)
        except Exception:
            pass

    def harvest_options(self, target: str) -> list[str]:
        """The option texts this closed widget offers — open, read, close.

        Feeds the model's snapshot so every dropdown's REAL choices are known
        upfront (native selects already are; this covers combos). Commits
        nothing: the menu is closed again and no option is clicked.
        """
        native = self._probe(NATIVE_SELECT_JS, {"target": target})
        if isinstance(native, dict) and isinstance(native.get("options"), list):
            texts = [
                str(o.get("text") or o.get("value") or "").strip()
                for o in native["options"]
                if isinstance(o, dict) and str(o.get("value") or "").strip()
            ]
            return [t for t in texts if t]
        marked = self._mark_combo(target)
        if not marked:
            return []
        try:
            self._open_combo(target)
            items = self._open_menu_options()
            if not items:
                sleep_ms(COMBO_MENU_MS)
                items = self._open_menu_options()
            texts = [str(i.get("text") or "").strip() for i in items]
            return [t for t in texts if t]
        finally:
            self._probe(UNMARK_COMBO_JS)
            self._clear_combo_search(target)

    def _select_typed_option(
        self, target: str, text: str, prelisted: list[str] | None = None
    ) -> dict[str, Any]:
        """Finish what the keystrokes started: pick the option they filtered to.

        Typing into a react-select puts SEARCH TEXT in the input and selects
        NOTHING — a human then presses Enter or clicks the option. Without this
        the widget reads back "" forever, the verify (correctly) calls the field
        unfilled, and the run escalates over a field the model already answered.

        No match is not a silently parked failure: the search text is CLEARED
        (leftover text renders "No options" and blocks the widget — run 81) and
        the note reports the options the menu actually offered, so the model's
        next round chooses from reality instead of re-guessing.
        """
        marked = self._mark_combo(target)
        chars = f"typed {len(str(text))} chars (combo-box)"
        if not marked:
            return {"ok": True, "note": chars, "selected": None, "value": ""}
        try:
            # The menu renders a frame or two after the keystrokes; read it in
            # place (no snapshot — re-stamping refs mid-menu is the run-82
            # drift) once it is on the page.
            sleep_ms(COMBO_MENU_MS)
            items = self._open_menu_options()
            if not items:
                # An empty scope is the RACE, not an answer: the menu had not
                # rendered (or had re-rendered) when the scan ran. Give it one
                # more beat and re-read before declaring the option missing —
                # run 79 f13 searched "Man", a real option, and lost it here.
                sleep_ms(COMBO_MENU_MS)
                items = self._open_menu_options()
            option = match_option(
                [{"ref": i.get("id"), "text": i.get("text")} for i in items], text
            )
            if option is None:
                listed = [str(i.get("text") or "").strip() for i in items]
                listed = [t for t in listed if t] or list(prelisted or [])
                self._clear_combo_search(target)
                value = str(self._probe(COMBO_VALUE_JS) or "")
                offers = "; ".join(t[:60] for t in listed[:12]) or "(none rendered)"
                return {
                    "ok": True,
                    "note": (
                        f'{chars} — no option matching "{str(text)[:40]}"; search text '
                        f"cleared; the menu offers: {offers}"
                    ),
                    "selected": None,
                    "value": value,
                }
            label = str(option.get("text") or "")
            clicked = self._click_menu_option(str(option.get("ref") or ""))
            # react-select commits the pick into `.select__single-value` after
            # the click — poll until it lands rather than sleeping the window.
            value = self._settle_combo_value()
            if not clicked:
                return {
                    "ok": True,
                    "note": f'{chars} — option "{label[:60]}" could not be clicked',
                    "selected": None,
                    "value": value,
                }
            return {
                "ok": True,
                "note": f'{chars} and selected "{label[:60]}"',
                "selected": label,
                "value": value,
            }
        finally:
            self._probe(UNMARK_COMBO_JS)

    def checkbox_state(self, target: str) -> str:
        """"true"/"false" for a checkbox, "" when the target is not one.

        A checkbox's `.value` is the string "on" (or "") no matter what the box
        is doing — run 79 f9 ticked the consent box for real and the verify read
        "" back off `.value` and called it unchecked. The truth is `.checked`.
        The ref can name a wrapper (a 1px input hides inside a styled label), so
        fall back to a box found within the widget, then to aria-checked.
        """
        state = self._probe(CHECKBOX_STATE_JS, {"target": target})
        return str(state or "")

    def _real_click(self, target: str) -> bool:
        """A REAL mouse click — the only kind a react-select option honours
        reliably (and the only kind that fires mousedown). A synthetic
        `el.click()` is the fallback of last resort, not the path."""
        if not target:
            return False
        sel = selector_for(target)
        try:
            locator = self._locator(target, sel).first
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=5000)
            return True
        except Exception:
            pass
        box = self._probe(
            """(payload) => {
              const sel = /^[fb]\\d+$/.test(payload.target)
                ? `[data-weaver-ref="${payload.target}"]` : payload.target;
              let el = null;
              try { el = document.querySelector(sel); } catch (err) { return null; }
              if (!el) return null;
              if (el.scrollIntoView) el.scrollIntoView({ block: 'center' });
              const r = el.getBoundingClientRect();
              if (!r.width || !r.height) return null;
              return { x: r.left + r.width / 2, y: r.top + Math.min(r.height / 2, 20) };
            }""",
            {"target": target},
        )
        if not box:
            return False
        try:
            self.page.mouse.click(box["x"], box["y"])
            return True
        except Exception:
            return False

    def _open_combo(self, target: str) -> None:
        """Open a react-select menu the way keyboards do: focus + ArrowDown.
        Mouse clicks toggle and land on the input (focus-only when it has
        text) — ArrowDown is deterministic."""
        try:
            expanded = self._eval(
                """(payload) => {
                  const sel = /^[fb]\\d+$/.test(payload.target)
                    ? `[data-weaver-ref="${payload.target}"]` : payload.target;
                  const el = document.querySelector(sel);
                  if (!el) return 'missing';
                  const ctrl = el.closest('.select__control') || el;
                  return String(el.getAttribute('aria-expanded') || ctrl.getAttribute('aria-expanded') || '');
                }""",
                {"target": target},
            )
        except Exception:
            expanded = ""
        if expanded != "true":
            try:
                sel = selector_for(target)
                self._locator(target, sel).focus()
                self.page.keyboard.press("ArrowDown")
                sleep_ms(380)
            except Exception:
                pass

    def click(self, target: str) -> dict[str, Any]:
        # react-select combos open on MOUSEDOWN — a synthetic .click() never
        # fires it. Detect the widget and use a real mouse click instead.
        probe = self._eval(
            """(payload) => {
                try {
                    const sel = /^[fb]\\d+$/.test(payload.target)
                        ? `[data-weaver-ref="${payload.target}"]` : payload.target;
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    const ctrl = el.closest('.select__control, .select__input-container');
                    const hit = ctrl || el;
                    const r = hit.getBoundingClientRect();
                    return {
                        x: r.left + r.width / 2,
                        y: r.top + Math.min(r.height / 2, 20),
                        tag: hit.tagName,
                        cls: String(hit.className || '').slice(0, 50),
                        disabled: !!(el.disabled || hit.disabled),
                        // Only a real CONTROL is a combo. An OPTION (or the menu,
                        // or the single-value label) is a sibling of the control,
                        // so `closest` finds nothing and `hit` is the element
                        // itself — those must be clicked, not "opened".
                        combo: !!ctrl,
                    };
                } catch { return null; }
            }""",
            {"target": target},
        )
        # `"select__" in cls` used to be the test — but a react-select OPTION's
        # class is "select__option", which contains it. Every option click was
        # answered with "opened combo" and nothing was ever selected. The probe
        # now reports whether the element really is (or lives in) the control.
        # A disabled target is a refusal, whatever kind of widget it is. The
        # branches below bypass CLICK_JS (which does check `el.disabled`), so
        # without this a disabled input came back "real-clicked input", ok=True.
        if probe and probe.get("disabled"):
            return {"ok": False, "note": "element is disabled"}
        if probe and probe.get("combo"):
            # react-select: open via keyboard (deterministic), never mouse.
            self._open_combo(target)
            return {"ok": True, "note": f"opened combo ({probe.get('cls', '')[:24]})"}
        if probe and probe.get("tag") == "INPUT":
            # Real events + auto-scroll into view.
            sel = selector_for(target)
            try:
                self._locator(target, sel).scroll_into_view_if_needed()
                self._locator(target, sel).click()
            except Exception:
                self.page.mouse.click(probe["x"], probe["y"])
            sleep_ms(SETTLE_MS)
            return {"ok": True, "note": "real-clicked input"}
        result = _step(self._eval(CLICK_JS, {"target": target}))
        if result["ok"]:
            sleep_ms(SETTLE_MS)
            # A segmented answer button (Ashby's Yes/No pairs) holds its state
            # in aria-pressed/aria-checked/a selected class — read it back so
            # a landed answer is VERIFIED, not assumed (hard rule 2).
            pressed = str(self._probe(PRESSED_STATE_JS, {"target": target}) or "")
            if pressed:
                result["pressed"] = pressed
                result["note"] = f"{result.get('note') or 'clicked'} — state: {pressed}"
        return result

    def upload(self, target: str) -> dict[str, Any]:
        """Attach the file THIS input asks for — resume or cover letter.

        Run 83 attached the resume into the Cover Letter box: both widgets
        snapshot as "Attach", and the old candidate order tried the page's
        first file input regardless of the target. The question text around
        the input decides the file; a cover-letter input with no cover file is
        a refusal (it is optional — empty beats a wrong document), never a
        fall-through to the resume.
        """
        near = str(self._probe(NEARBY_TEXT_JS, {"target": target}) or "")
        is_cover = bool(re.search(r"cover\s*letter|\bcover\b", near, re.I))
        path = self.cover_path if is_cover else self.resume_path
        if is_cover and not path:
            return {
                "ok": False,
                "note": (
                    "this is the COVER LETTER input and no cover letter file is "
                    "configured — it is optional: leave it empty, never attach the resume here"
                ),
            }
        if not path:
            return {"ok": False, "note": "no resume file was configured for this run"}
        if not Path(path).exists():
            kind = "cover letter" if is_cover else "resume"
            return {"ok": False, "note": f"{kind} file not found: {path}"}
        # The target's own input FIRST — the page-wide fallbacks are for a
        # resume widget whose input renders late, and must never cross-attach
        # into a different section's input.
        candidates = (
            (selector_for(target),)
            if is_cover
            else (selector_for(target), "input#resume", "input[type='file']")
        )
        last = "no file input found"
        frame = self._frame_for(target)
        for attempt in range(3):
            for sel in candidates:
                try:
                    frame.set_input_files(sel, path, timeout=15000)
                    sleep_ms(TYPE_SETTLE_MS)
                    kind = " (cover letter)" if is_cover else ""
                    return {"ok": True, "note": f"attached {Path(path).name}{kind}"}
                except Exception as exc:
                    last = f"{type(exc).__name__}: {exc}"
            if attempt >= 2:
                break
            # The attach widget's file input may render only when active —
            # open it by clicking the attach/upload control (by text).
            # Escape first: an open react-select menu/panel can block the widget.
            try:
                self.page.keyboard.press("Escape")
                sleep_ms(300)
            except Exception:
                pass
            try:
                self._eval(
                    """() => {
                      const els = Array.from(document.querySelectorAll('button, a, [role="button"], label, li'));
                      const hit = els.find((el) => {
                        const t = String(el.textContent || '').trim().toLowerCase();
                        return /attach|upload|add a file|attach a file|resume|cv/.test(t) && t.length < 40;
                      });
                      if (hit) { hit.setAttribute('data-weaver-click', '1'); return true; }
                      return false;
                    }""",
                    target=target,
                )
                frame.locator('[data-weaver-click="1"]').first().click(timeout=5000)
                self._eval(
                    """() => Array.from(document.querySelectorAll('[data-weaver-click]')).forEach((el) => el.removeAttribute('data-weaver-click'))""",
                    target=target,
                )
            except Exception:
                pass
            sleep_ms(1200)
        return {"ok": False, "note": f"upload failed: {last}"}


def _step(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"ok": False, "note": f"page script returned {type(result).__name__}"}
    extra = {k: v for k, v in result.items() if k not in ("ok", "note")}
    # A JS boolean value ("was the box checked?") becomes its python spelling —
    # step records carry strings, and "true" vs "True" is a difference nobody
    # reading a transcript should have to reason about.
    if isinstance(extra.get("value"), bool):
        extra["value"] = str(extra["value"])
    return {"ok": bool(result.get("ok")), "note": str(result.get("note") or ""), **extra}


#: What the human sees when the run parks on the audit seam.
AUDIT_PROMPT = (
    "\n[weaver] human audit — the browser window is still open on the form.\n"
    "         Finish the field yourself, then press Enter here to close it. "
)


#: What the human sees when there is no tty to press Enter at.
AUDIT_HOLD = (
    "\n[weaver] human audit — the browser window is open on the form.\n"
    "         Finish the field yourself, then CLOSE the window to end the run. "
)

#: How often the hold asks the page whether the human closed it.
AUDIT_POLL_MS = 500.0


#: The shared tab-host browser: one visible window, every run a tab in it.
DEFAULT_CDP_PORT = 9777


def cdp_port() -> int:
    try:
        return int(os.environ.get("WEAVER_CDP_PORT") or DEFAULT_CDP_PORT)
    except ValueError:
        return DEFAULT_CDP_PORT


def _port_open(port: int, timeout: float = 0.3) -> bool:
    import socket

    with socket.socket() as probe:
        probe.settimeout(timeout)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ensure_tab_host(data_dir: str | Path, port: int | None = None, wait_s: float = 25.0) -> str:
    """The shared browser's CDP url — starting a host if none is listening.

    `weaver apply --tab` runs land as TABS of one window instead of stacking a
    window per run (the morning-of-Aug-16 flow opened one window per job
    because tab mode was env-gated and undocumented). The host survives the
    runs; closing a tab ends only that run.
    """
    import subprocess

    port = port or cdp_port()
    url = f"http://127.0.0.1:{port}"
    if _port_open(port):
        return url
    log_path = Path(data_dir) / "tab-host.log"
    with open(log_path, "a") as log:
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from weaver import cli; cli.main(['tab-host', '--data-dir', "
                f"{str(Path(data_dir))!r}, '--port', '{port}'])",
            ],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    waited = 0.0
    while waited < wait_s:
        if _port_open(port):
            return url
        sleep_ms(500)
        waited += 0.5
    raise RuntimeError(
        f"the tab host did not open port {port} within {wait_s:.0f}s — "
        f"see {log_path} (or run `weaver tab-host` in another terminal)"
    )


def run_tab_host(data_dir: str | Path, port: int | None = None) -> None:
    """The shared headed browser itself. Blocks until killed or window closed."""
    from playwright.sync_api import sync_playwright

    port = port or cdp_port()
    profile = Path(data_dir) / "tab-host-profile"
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            args=[f"--remote-debugging-port={port}", "--no-first-run"],
            user_agent=CHROME_UA,
            viewport=VIEWPORT,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            "data:text/html,<title>weaver tab host</title>"
            "<h2 style='font-family:sans-serif'>weaver tab host — apply runs open as tabs here</h2>"
        )
        print(f"tab host up on 127.0.0.1:{port} (close this window to stop)", file=sys.stderr)
        while True:
            with contextlib.suppress(Exception):
                if not context.pages:
                    return
            sleep_ms(2000)


def wait_for_human(prompt: str = AUDIT_PROMPT, page: Any = None) -> None:
    """Block until the human is done with the open window.

    A tty gets the Enter prompt. Without one there is nobody to press Enter —
    and returning immediately is exactly the bug this seam exists to prevent:
    the run parked on `audit_pending` and the window blinked shut before Leo
    could touch it. With no tty, wait for the HUMAN to close the window; that
    close IS the signal. Only a run with neither tty nor page falls through.
    """
    try:
        if sys.stdin and sys.stdin.isatty():
            input(prompt)
            return
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        return
    if page is None:
        return
    print(AUDIT_HOLD)
    while True:
        try:
            if page.is_closed():
                return
        except (KeyboardInterrupt, Exception):
            return
        try:
            sleep_ms(AUDIT_POLL_MS)
        except KeyboardInterrupt:
            return


@contextlib.contextmanager
def launch(
    job_url: str,
    resume_path: str | Path | None = None,
    headless: bool = True,
    nav_timeout_ms: int = NAV_TIMEOUT_MS,
    keep_open: Callable[[], bool] | None = None,
    pause: Callable[[], None] | None = None,
    cover_path: str | Path | None = None,
    cdp_url: str | None = None,
) -> Iterator[LocalDriver]:
    """Open a local chromium on `job_url` and yield a driver for it.

    `headless=False` (weaver apply --visible) shows a real Chrome window.
    `keep_open` is the human-audit seam: if it returns True when the body is
    done, the window is NOT closed. In an OWNED browser, `pause` (default:
    wait for Enter / tab close) runs first, because this process exiting would
    take the window with it. In a SHARED browser (tab mode) nothing waits:
    the tab-host outlives the run, so the held tab is simply left open and
    the run returns at once — completion is marked when the fill is done,
    never at tab close.

    `cdp_url` (or WEAVER_CDP_URL) attaches to an ALREADY-RUNNING chromium over
    CDP instead of launching one: the run opens as a TAB in that browser's
    window, so several concurrent runs share one window instead of stacking
    windows. Teardown closes only this run's tab (held tabs stay), never the
    shared browser.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "playwright is not installed — `uv sync` then `uv run playwright install chromium`"
        ) from exc

    cdp = (cdp_url or os.environ.get("WEAVER_CDP_URL") or "").strip()
    with sync_playwright() as pw:
        shared = bool(cdp)
        if shared:
            try:
                browser = pw.chromium.connect_over_cdp(cdp)
            except Exception as exc:
                raise RuntimeError(
                    f"could not attach to the shared browser at {cdp} ({exc}) — "
                    "is the tab host running?"
                ) from exc
            # The host's default context carries the UA/viewport; a fresh
            # context here would open a SECOND window, defeating the point.
            # It would also die with the CDP disconnect (connection-created
            # contexts do), so held tabs only survive under the host's own
            # default context — which a persistent-context host always has.
            context = browser.contexts[0] if browser.contexts else browser.new_context(
                user_agent=CHROME_UA, viewport=VIEWPORT
            )
        else:
            try:
                browser = pw.chromium.launch(headless=headless)
            except Exception as exc:
                raise RuntimeError(
                    f"could not start chromium ({exc}) — run `uv run playwright install chromium`"
                ) from exc
            context = browser.new_context(user_agent=CHROME_UA, viewport=VIEWPORT)
        page = context.new_page()
        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            sleep_ms(SETTLE_MS)
            yield LocalDriver(page, resume_path, cover_path=cover_path)
        finally:
            hold = False
            with contextlib.suppress(Exception):
                hold = bool(keep_open and keep_open())
            if shared:
                # The tab-host outlives this process, so a held run does NOT
                # wait for a human here: the run exits now (ledger row written,
                # next queued run unblocked) and its filled tab stays open in
                # the shared window for the human to review and send.
                if not hold:
                    # This run owns its TAB only — the browser (and its other
                    # tabs, possibly mid-run) belongs to the host.
                    with contextlib.suppress(Exception):
                        page.close()
                with contextlib.suppress(Exception):
                    browser.close()  # disconnects; the shared browser lives on
            else:
                # This process owns the browser: exiting kills the window, so
                # a held run must wait at the seam or the form vanishes.
                if hold:
                    (pause or (lambda: wait_for_human(page=page)))()
                with contextlib.suppress(Exception):
                    context.close()
                with contextlib.suppress(Exception):
                    browser.close()
