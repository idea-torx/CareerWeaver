"""`weaver init` writes a blank profile template — never anyone's real data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weaver import cli, config as cfg


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code = cli.main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_init_writes_a_blank_profile_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    code, payload = run(capsys, "--data-dir", str(data), "init")

    assert code == 0
    assert payload["profile_fields_set"] == []
    profile = json.loads((data / "config.json").read_text(encoding="utf-8"))["profile"]

    for key in ("first_name", "last_name", "email", "phone", "location", "resume_path"):
        assert profile[key] == "", key
    for key in ("links", "target_roles", "target_skills"):
        assert profile[key] == [], key
    for key in ("consent_ats_data_retention", "how_did_you_hear", "authorized_to_work"):
        assert profile[key] == "", key
    for key in ("pronouns", "gender", "date_of_birth", "disability_status",
                "race_ethnicity", "veteran_status"):
        assert profile[key] == "", key

    # No shipped default is ever a real value.
    assert all(not v for v in cfg.DEFAULT_PROFILE.values())


def test_init_accepts_profile_flags_and_splits_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    code, payload = run(
        capsys,
        "--data-dir",
        str(data),
        "init",
        "--first-name",
        "Mira",
        "--last-name",
        "Halloway",
        "--email",
        "mira@halloway.example",
        "--links",
        "linkedin.com/in/mira, mira.example",
        "--target-roles",
        "Design Engineer,Full Stack Engineer",
    )

    assert code == 0
    profile = payload["profile"]
    assert profile["name"] == "Mira Halloway"  # derived from first+last
    assert profile["links"] == ["linkedin.com/in/mira", "mira.example"]
    assert profile["target_roles"] == ["Design Engineer", "Full Stack Engineer"]
    assert set(payload["profile_fields_set"]) >= {"first_name", "email", "links"}


def test_init_reads_profile_env_vars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEAVER_PROFILE_EMAIL", "env@example.test")
    monkeypatch.setenv("WEAVER_PROFILE_LOCATION", "Portland, OR")
    data = tmp_path / "data"
    code, payload = run(capsys, "--data-dir", str(data), "init")

    assert code == 0
    assert payload["profile"]["email"] == "env@example.test"
    assert payload["profile"]["location"] == "Portland, OR"


def test_flags_beat_env_and_rerun_never_blanks_a_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEAVER_PROFILE_EMAIL", "env@example.test")
    data = tmp_path / "data"
    code, payload = run(
        capsys, "--data-dir", str(data), "init", "--email", "flag@example.test"
    )
    assert code == 0 and payload["profile"]["email"] == "flag@example.test"

    monkeypatch.delenv("WEAVER_PROFILE_EMAIL", raising=False)
    code, again = run(capsys, "--data-dir", str(data), "init")
    assert code == 0
    assert again["already_existed"] is True
    assert again["profile"]["email"] == "flag@example.test"


def test_interactive_prompts_stay_off_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`weaver init -i --json | jq` must still see clean JSON."""
    answers = ["Mira", "Halloway", "mira@halloway.example"]
    monkeypatch.setattr("builtins.input", lambda *_: answers.pop(0) if answers else "")

    data = tmp_path / "data"
    code = cli.main(["--data-dir", str(data), "init", "--interactive", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["profile"]["name"] == "Mira Halloway"
    assert payload["profile"]["email"] == "mira@halloway.example"
    assert "First name" in captured.err  # prompts never pollute the JSON


def test_normalize_profile_derives_first_last_from_name() -> None:
    profile = cfg.normalize_profile({"name": "Mira Q Halloway", "links": "a.example, b.example"})
    assert profile["first_name"] == "Mira"
    assert profile["last_name"] == "Q Halloway"
    assert profile["links"] == ["a.example", "b.example"]
