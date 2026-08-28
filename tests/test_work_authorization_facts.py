"""The applicant's US work-authorization answers must land on the right option.

Run 151 (Wispr Flow) answered "Are you legally authorized to work in the US?"
with Yes, because `authorized_to_work` read "Yes — Canada (…)" and match_option
picks on the LEADING TOKEN. The fact was Canada-scoped; the question was
US-scoped. Leo needs an H-1B, so the honest answers are No / sponsorship-yes.

This pins the leading token of each fact, which is what the matcher actually
keys on — reword the phrases freely, but the option they select must not move.
"""
import json
import pathlib

import pytest

from weaver.local_driver import match_option

CONFIG = pathlib.Path(__file__).resolve().parents[1] / "data" / "config.json"

pytestmark = pytest.mark.skipif(
    not CONFIG.exists(), reason="local-only applicant record (gitignored)"
)


def profile():
    return json.loads(CONFIG.read_text())["profile"]


def pick(fact: str, options: list[str]) -> str | None:
    hit = match_option([{"text": o} for o in options], profile().get(fact) or "")
    return (hit or {}).get("text")


@pytest.mark.parametrize(
    "fact, expected",
    [
        ("authorized_to_work", "No"),
        ("visa_sponsorship_required", "Yes"),
        ("work_preference", "Yes"),
    ],
)
def test_yes_no_answer(fact, expected):
    assert pick(fact, ["Yes", "No"]) == expected


def test_never_claims_us_authorization():
    """The dangerous direction: overstating authorization to a US employer."""
    assert pick("authorized_to_work", ["Yes", "No"]) != "Yes"
    assert pick("visa_sponsorship_required", ["Yes", "No"]) != "No"


def test_remote_preference_still_reachable():
    """Leading with "Yes" (for relocation) must not cost the Remote option."""
    assert pick("work_preference", ["Remote", "Hybrid", "On-site"]) == "Remote"
