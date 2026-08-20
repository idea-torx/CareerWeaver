"""Data-dir resolution and the on-disk config (profile / contact block)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_NAME = "config.json"
DB_NAME = "weaver.db"

#: The blank profile template `weaver init` writes. Every value is empty — no
#: real person's details ship in this repo. Fill it with `weaver init
#: --interactive`, with `weaver init --email ... --phone ...`, or by editing
#: `<data-dir>/config.json` by hand.
DEFAULT_PROFILE: dict[str, Any] = {
    # identity
    "first_name": "",
    "last_name": "",
    "name": "",
    "title": "",
    "email": "",
    "phone": "",
    "location": "",
    "links": [],
    # what you are aiming at — used to pick lenses and score postings
    "target_roles": [],
    "target_skills": [],
    # do-not-mention list: topics, employers or keywords that must NEVER appear
    # in anything the model writes for you (resume remixes, essays, "why this
    # role", cover letters, open fields). Enforced in code, not just in prompts.
    "avoid": [],
    # the resume file applications upload (local path or public http(s) URL)
    "resume_path": "",
    # answers to the questions every ATS asks
    "authorized_to_work": "",
    "visa_sponsorship_required": "",
    "consent_ats_data_retention": "",
    "how_did_you_hear": "",
    # earliest start date, e.g. "Immediately" or "Two weeks' notice"
    "availability": "",
    # Voluntary self-ID — leave empty and the agent answers "I do not wish to
    # disclose". Only fill in what you actively want submitted.
    "pronouns": "",
    "gender": "",
    "date_of_birth": "",
    "disability_status": "",
    "race_ethnicity": "",
    "veteran_status": "",
}

#: (key, prompt) for `weaver init --interactive`, in asking order.
PROFILE_PROMPTS: list[tuple[str, str]] = [
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("location", "Location (city, region, country)"),
    ("title", "Current or desired title"),
    ("links", "Links (comma separated: LinkedIn, portfolio, GitHub…)"),
    ("target_roles", "Target roles (comma separated)"),
    ("target_skills", "Target skills (comma separated)"),
    ("resume_path", "Resume file path or public URL"),
    ("authorized_to_work", "Authorized to work (e.g. 'Yes — US citizen')"),
    ("visa_sponsorship_required", "Visa sponsorship required (Yes/No)"),
    ("consent_ats_data_retention", "Consent to ATS data retention (Yes/No)"),
    ("how_did_you_hear", "How did you hear about us (default answer)"),
    ("availability", "Availability / earliest start date (e.g. 'Immediately')"),
]

#: Profile keys that hold lists; CLI/prompt input is comma separated.
LIST_KEYS = frozenset({"links", "target_roles", "target_skills", "avoid"})

#: Lane 1 (`weaver find`) defaults. Target roles/skills come from the profile
#: (or a `find` override); only board config and scoring knobs live here. The
#: code never hardcodes a specific user's targets.
DEFAULT_FIND: dict[str, Any] = {
    "orgs": {"webflow": "greenhouse"},
    "seniority": ["staff", "senior", "principal"],
    "locations": [],
}


def normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Fill in `name` from first/last (or vice versa) and coerce the list keys."""
    out = dict(DEFAULT_PROFILE)
    out.update(profile or {})
    for key in LIST_KEYS:
        value = out.get(key)
        if isinstance(value, str):
            out[key] = [part.strip() for part in value.split(",") if part.strip()]
        elif value is None:
            out[key] = []
        else:
            out[key] = list(value)
    first, last = (out.get("first_name") or "").strip(), (out.get("last_name") or "").strip()
    full = (out.get("name") or "").strip()
    if not full and (first or last):
        out["name"] = " ".join(part for part in (first, last) if part)
    elif full and not (first or last):
        parts = full.split()
        out["first_name"] = parts[0]
        out["last_name"] = " ".join(parts[1:])
    return out


def resolve_data_dir(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """--data-dir > $WEAVER_DATA_DIR > ./data (relative to cwd)."""
    if data_dir:
        return Path(data_dir).expanduser().resolve()
    env = os.environ.get("WEAVER_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "data").resolve()


def db_path(data_dir: Path) -> Path:
    return data_dir / DB_NAME


def config_path(data_dir: Path) -> Path:
    return data_dir / CONFIG_NAME


def load_config(data_dir: Path) -> dict[str, Any]:
    path = config_path(data_dir)
    if not path.exists():
        return default_config()
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default_config()
    base = default_config()
    base.update(cfg)
    base["profile"] = normalize_profile(base.get("profile") or {})
    find = dict(DEFAULT_FIND)
    find.update(base.get("find") or {})
    base["find"] = find
    return base


def save_config(data_dir: Path, config: dict[str, Any]) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = config_path(data_dir)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "profile": dict(DEFAULT_PROFILE),
        "find": dict(DEFAULT_FIND),
        "render": {"font": "Arial", "accent": "333333"},
        "llm": {
            "key_env": "WEAVER_API_KEY",
            "model_env": "WEAVER_MODEL",
            "base_url_env": "WEAVER_BASE_URL",
            "note": (
                "Any OpenAI-compatible endpoint. No key -> deterministic "
                "fallback. Never crashes."
            ),
        },
    }


def out_dir(data_dir: Path) -> Path:
    d = data_dir / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_env_file(data_dir: Path) -> list[str]:
    """`<data-dir>/env` -> os.environ. Existing environment always wins.

    Cron, launchd, and gated agent harnesses run commands with a bare
    environment and no shell to `source` anything — which is how WEAVER_API_KEY
    never arrived and runs fell back to the deterministic path. One KEY=VALUE
    file inside the (gitignored) data dir makes a bare
    `<repo>/.venv/bin/weaver batch …` argv fully self-sufficient.

    Lines: `KEY=VALUE`, `#` comments and blanks ignored, optional surrounding
    quotes on the value stripped, `export ` prefix tolerated so an existing
    shell env file can be reused as-is. Returns the KEYS it set (never values).
    """
    path = Path(data_dir) / "env"
    if not path.is_file():
        return []
    loaded: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
