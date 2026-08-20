"""Ship-ready checks: the CLI entry point, a provider nobody has to be, and no
committed file carrying a real person's details.

These are the tests that keep a fresh clone usable by someone who is not the
author. They read the repo as it would be published.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

from weaver import config as cfg, llm
from weaver import local_agent
from weaver import payload as payload_lib

REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------- packaging


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_console_script_points_at_the_cli(pyproject: dict) -> None:
    assert pyproject["project"]["scripts"]["weaver"] == "weaver.cli:main"


def test_project_metadata_is_publishable(pyproject: dict) -> None:
    project = pyproject["project"]
    assert project["name"] and project["version"] and project["description"]
    assert "MIT" in project["license"]["text"]
    urls = project["urls"]
    assert urls["Homepage"].startswith("https://")
    assert urls["Repository"].startswith("https://")
    assert project["readme"] == "README.md"


def test_dependencies_stay_light(pyproject: dict) -> None:
    """No heavy runtime deps — the CLI is argparse (stdlib) + a browser + parsers."""
    names = {re.split(r"[<>=!\[ ]", d)[0].lower() for d in pyproject["project"]["dependencies"]}
    assert names == {"python-docx", "pypdf", "markdown-it-py", "playwright"}


def test_readme_covers_install_provider_init_and_privacy() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()
    for needle in (
        "uv sync",
        "playwright install chromium",
        "pip install -e .",
        "weaver_base_url",
        "weaver_model",
        "weaver init",
        "weaver tailor",
        "weaver apply",
        "--visible",
        "privacy",
        "mit",
    ):
        assert needle in readme, needle


# ------------------------------------------------------- depersonalization grep

#: Every file a fresh clone would receive. Vendored deps are not ours to clean.
COMMITTED_GLOBS = ("src/weaver/*.py", "tests/*.py", "worker/**/*.js", "worker/**/*.ts")
SHIPPED_FILES = ("pyproject.toml", "README.md", "AGENTS.md", ".gitignore")
EXCLUDED_DIRS = frozenset({"node_modules", ".venv", "__pycache__", "dist", "build", ".wrangler"})

#: The author's details must not appear anywhere in committed code. Test files
#: and this test itself are checked too — the fixtures use `*.example` names.
PERSONAL_PATTERNS = {
    "keychain account": re.compile(r"\bleofelix\b", re.I),
    "personal domain": re.compile(r"\b(ideatorx|matteblack|vibecinema)\b", re.I),
    "home directory": re.compile(r"/Users/[a-z0-9._-]+/", re.I),
    "hardcoded repo path": re.compile(r"~?/?Documents/(ResumeWeaver|CareerWeaver)", re.I),
    "real mailbox": re.compile(
        r"[a-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|yahoo|proton(?:mail)?|icloud)\.[a-z]+",
        re.I,
    ),
}

#: `local_driver.py` / `local_agent.py` are out of scope for this pass; their
#: place names live in test-fixture HTML and prompt commentary, not in config.
EXEMPT = {"local_driver.py", "local_agent.py", "test_local_driver.py", "test_local_agent.py"}


def committed_sources() -> list[Path]:
    paths: list[Path] = []
    for pattern in COMMITTED_GLOBS:
        paths.extend(
            p
            for p in REPO_ROOT.glob(pattern)
            if p.is_file() and EXCLUDED_DIRS.isdisjoint(p.relative_to(REPO_ROOT).parts)
        )
    paths.extend(REPO_ROOT / name for name in SHIPPED_FILES)
    return [p for p in paths if p.exists() and p.name not in EXEMPT]


@pytest.mark.parametrize("label", sorted(PERSONAL_PATTERNS))
def test_no_hardcoded_user_details_in_committed_code(label: str) -> None:
    pattern = PERSONAL_PATTERNS[label]
    hits = []
    for path in committed_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.name == Path(__file__).name:  # the patterns themselves
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:100]}")
    assert not hits, f"{label} found in committed code:\n" + "\n".join(hits)


def test_sample_resume_is_fictional() -> None:
    """The one allowed placeholder identity — reserved `.example` names only."""
    text = (REPO_ROOT / "samples" / "sample-resume.md").read_text(encoding="utf-8")
    for address in re.findall(r"[\w.%+-]+@[\w.-]+", text):
        assert address.endswith(".example"), address
    assert "555-01" in text  # reserved fictional phone range


# ------------------------------------------------------------------- providers


def test_default_model_and_base_url_are_a_matched_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEAVER_MODEL", raising=False)
    monkeypatch.delenv("WEAVER_BASE_URL", raising=False)
    assert llm.base_url() == llm.DEFAULT_BASE_URL == "https://api.openai.com/v1"
    assert llm.model() == llm.DEFAULT_MODEL
    assert llm.config_error() is None


def test_custom_base_url_without_a_model_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WEAVER_BASE_URL", "https://relay.example/v1")
    monkeypatch.delenv("WEAVER_MODEL", raising=False)

    problem = llm.config_error()
    assert problem and "WEAVER_MODEL" in problem
    assert llm.describe()["config_error"] == problem

    monkeypatch.setenv("WEAVER_API_KEY", "sk-test")
    result = llm.complete_json("s", "u")
    assert result["_fallback"] is True
    assert "WEAVER_MODEL" in result["_reason"]
    assert "llm config error" in capsys.readouterr().err  # never a network call


def test_matching_custom_provider_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEAVER_BASE_URL", "https://relay.example/v1/")
    monkeypatch.setenv("WEAVER_MODEL", "their-model")
    assert llm.config_error() is None
    assert llm.base_url() == "https://relay.example/v1"
    assert llm.model() == "their-model"


def test_no_provider_is_hardcoded_beyond_the_default_pair() -> None:
    source = (REPO_ROOT / "src" / "weaver" / "llm.py").read_text(encoding="utf-8").lower()
    for vendor in ("openrouter", "zen", "deepseek", "anthropic.com"):
        assert vendor not in source, vendor


# -------------------------------------------------------------------- profiles


def test_portfolio_link_is_the_first_non_social_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEAVER_PORTFOLIO_HOSTS", raising=False)
    links = ["https://linkedin.com/in/you", "https://github.com/you", "https://you.example"]
    applicant = payload_lib.applicant_from_profile({}, links)
    assert applicant["portfolio_url"] == "https://you.example"
    assert applicant["linkedin_url"] == "https://linkedin.com/in/you"


def test_availability_reaches_the_applicant_data() -> None:
    """Run 137 (Plane) burned six actions on "by when can you join?" because the
    payload whitelist dropped the fact, so the typed-value guard refused every
    answer the model tried. Declared in the profile, it must reach the engine."""
    applicant = payload_lib.applicant_from_profile({"availability": "Immediately"}, [])
    assert applicant["availability"] == "Immediately"
    assert "immediately" in local_agent.applicant_values(applicant)
    assert local_agent.typed_text_allowed("Immediately", applicant)
    # ...and the same answer is still refused when the fact is not declared.
    assert not local_agent.typed_text_allowed(
        "Immediately", payload_lib.applicant_from_profile({}, [])
    )


def test_portfolio_hosts_env_biases_the_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEAVER_PORTFOLIO_HOSTS", "dribbble.")
    links = ["https://you.example", "https://dribbble.com/you"]
    assert payload_lib._portfolio(links) == "https://dribbble.com/you"


def test_profile_template_covers_what_a_form_asks() -> None:
    prompted = {key for key, _ in cfg.PROFILE_PROMPTS}
    assert prompted <= set(cfg.DEFAULT_PROFILE)
    for key in ("email", "phone", "location", "links", "target_roles",
                "consent_ats_data_retention", "how_did_you_hear", "resume_path"):
        assert key in prompted, key


def test_apply_max_actions_help_matches_the_real_defaults() -> None:
    from weaver import cli, local_agent

    parser = cli.build_parser()
    apply_parser = parser._subparsers._group_actions[0].choices["apply"]  # type: ignore[union-attr]
    action = next(a for a in apply_parser._actions if a.dest == "max_actions")
    assert str(local_agent.DEFAULT_MAX_ACTIONS) in action.help
