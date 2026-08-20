"""Pluggable LLM provider — bring your own OpenAI-compatible endpoint.

`complete_json(system, user)` returns a dict. With a key set it calls any
OpenAI-compatible /chat/completions endpoint. Without a key — or on any
failure — it returns a deterministic-fallback marker so callers can degrade
instead of crashing. Tests run with no keys.

Configuration is entirely environment-driven, with no provider baked in beyond
a coherent default pair:

    WEAVER_API_KEY / OPENAI_API_KEY   your provider key
    WEAVER_BASE_URL / OPENAI_BASE_URL endpoint root (default: OpenAI)
    WEAVER_MODEL                      model id (must be set for a custom base url)

The default model and the default base url are chosen as a matched pair. Point
`WEAVER_BASE_URL` somewhere else and you MUST also set `WEAVER_MODEL` — a model
id from one provider is meaningless at another. That mismatch is reported
loudly (`config_error()`, surfaced in `describe()` and in every fallback
reason) instead of being silently sent to an endpoint that will reject it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

#: Matched pair — this model id is valid at this base url. Change one, change both.
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

CONFIG_HINT = (
    "set WEAVER_MODEL and WEAVER_BASE_URL (plus WEAVER_API_KEY) — a custom "
    "OpenAI-compatible endpoint needs its own model id"
)

#: Some OpenAI-compatible relays 403 a request without attribution headers.
#: Weaver identifies itself as weaver — override per relay with the env vars
#: below rather than borrowing another project's identity.
DEFAULT_USER_AGENT = "CareerWeaver/0.1"
DEFAULT_REFERER = "https://careerweaver.local"
DEFAULT_APP_TITLE = "CareerWeaver"


def attribution_headers() -> dict[str, str]:
    """User-Agent / HTTP-Referer / X-Title, overridable from the environment."""

    def pick(name: str, fallback: str) -> str:
        return (os.environ.get(name) or "").strip() or fallback

    return {
        "User-Agent": pick("WEAVER_USER_AGENT", DEFAULT_USER_AGENT),
        "HTTP-Referer": pick("WEAVER_HTTP_REFERER", DEFAULT_REFERER),
        "X-Title": pick("WEAVER_APP_TITLE", DEFAULT_APP_TITLE),
    }


def provider_name() -> str:
    """'openai' when a key is configured, else 'deterministic'."""
    return "openai" if api_key() else "deterministic"


def api_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("WEAVER_API_KEY")
    return key.strip() or None if key else None


def model() -> str:
    return (os.environ.get("WEAVER_MODEL") or "").strip() or DEFAULT_MODEL


def base_url() -> str:
    url = (
        os.environ.get("WEAVER_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    return url.strip().rstrip("/")


def config_error() -> str | None:
    """The loud complaint about an incoherent provider config, or None if sane.

    The only incoherent case is a custom base url with no model id: we would be
    sending `DEFAULT_MODEL` (an OpenAI model) to somebody else's endpoint.
    """
    if base_url() != DEFAULT_BASE_URL and not (os.environ.get("WEAVER_MODEL") or "").strip():
        return (
            f"WEAVER_BASE_URL is {base_url()} but WEAVER_MODEL is unset, so the "
            f"default model {DEFAULT_MODEL!r} would be sent to a provider that "
            f"does not have it — {CONFIG_HINT}"
        )
    return None


def timeout() -> float:
    try:
        return float(os.environ.get("WEAVER_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def deterministic_fallback(reason: str = "no api key") -> dict[str, Any]:
    """The never-crash return value: callers see `_fallback` and build locally."""
    return {"_fallback": True, "_reason": reason, "provider": "deterministic"}


def complete_json(system: str, user: str) -> dict[str, Any]:
    """Ask the model for a JSON object. Never raises."""
    key = api_key()
    if not key:
        return deterministic_fallback(f"no api key — {CONFIG_HINT}")
    problem = config_error()
    if problem:
        print(f"weaver: llm config error — {problem}", file=sys.stderr)
        return deterministic_fallback(problem)

    payload = {
        "model": model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": float(os.environ.get("WEAVER_TEMPERATURE", "0.3")),
    }
    request = urllib.request.Request(
        f"{base_url()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            # Attribution headers — some relays 403 without them.
            **attribution_headers(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
        return deterministic_fallback(f"provider error: {type(exc).__name__}: {exc}")

    parsed = parse_json_object(content)
    if parsed is None:
        return deterministic_fallback("provider returned unparseable JSON")
    parsed.setdefault("provider", "openai")
    return parsed


def parse_json_object(content: str) -> dict[str, Any] | None:
    """Tolerant JSON extraction — handles fenced blocks and leading prose."""
    if not content:
        return None
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def describe() -> dict[str, Any]:
    """Provider config for `--json` output (never leaks the key)."""
    return {
        "provider": provider_name(),
        "model": model() if api_key() else None,
        "base_url": base_url() if api_key() else None,
        "key_present": bool(api_key()),
        "config_error": config_error(),
    }
