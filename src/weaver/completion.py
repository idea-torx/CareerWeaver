"""How a run ENDS — the loud block, the exit code, the macOS ping.

Runs used to fizzle: the browser window closed, the terminal scrolled, and
nothing said whether the application was filled, failed, or waiting on a human.
Every `weaver apply` run now ends with exactly one of three blocks, an exit code
that means the same thing to a script, and (opt-in) a notification.

    ✓ DONE — FILLED + HELD    status held/submitted   exit 0
    ✗ FAILED — <reason>       status failed/stopped   exit 2
    ⏸ AUDIT_PENDING — <what>  status audit_pending    exit 3

Nothing here touches the database or the browser: `cli.cmd_apply` owns the
ledger write, this module owns the words, the code, and the ping.
"""

from __future__ import annotations

import subprocess
import sys

#: The horizontal rule that makes the block impossible to scroll past.
RULE = "═" * 64

HELD = "held"
SUBMITTED = "submitted"
FAILED = "failed"
AUDIT_PENDING = "audit_pending"

EXIT_DONE = 0
EXIT_FAILED = 2
EXIT_AUDIT_PENDING = 3

#: Statuses that mean the run reached its intended end (`weaver ledger` says
#: `held` for a --hold park and `submitted` for the engine's `applied`).
DONE_STATUSES = frozenset({HELD, SUBMITTED, "applied"})

_NEXT_STEP = {
    HELD: "review the filled form in that tab and press send yourself",
    SUBMITTED: "nothing — it is sent; `weaver ledger` carries the row",
    AUDIT_PENDING: "finish this one in the open window, then check `weaver ledger`",
    FAILED: "fix the reason above, then re-run `weaver apply <resume_id>`",
}

_NOTIFY_WORD = {
    HELD: "FILLED + HELD",
    SUBMITTED: "SUBMITTED",
    AUDIT_PENDING: "AUDIT PENDING",
}


def _norm(status: str | None) -> str:
    """Ledger vocabulary, collapsed to the three terminal families."""
    raw = (status or "").strip()
    if raw in DONE_STATUSES:
        return SUBMITTED if raw == "applied" else raw
    if raw == AUDIT_PENDING:
        return AUDIT_PENDING
    return FAILED


def _clean(text: object, limit: int = 140) -> str:
    """One line, no runaway: blocks stay a block."""
    one = " ".join(str(text or "").split())
    return one[:limit]


def exit_code(status: str | None) -> int:
    """0 = held/submitted, 2 = failed (or stopped), 3 = audit_pending."""
    family = _norm(status)
    if family in DONE_STATUSES:
        return EXIT_DONE
    if family == AUDIT_PENDING:
        return EXIT_AUDIT_PENDING
    return EXIT_FAILED


def job_line(role: str | None = "", org: str | None = "") -> str:
    role_text = _clean(role, 80) or "(untitled role)"
    org_text = _clean(org, 40)
    return f"{role_text} @ {org_text}" if org_text else role_text


def headline(status: str | None, reason: str | None = "") -> str:
    """The one line a human reads from across the room."""
    family = _norm(status)
    if family == HELD:
        return "✓ DONE — FILLED + HELD"
    if family == SUBMITTED:
        return "✓ DONE — SUBMITTED"
    if family == AUDIT_PENDING:
        return f"⏸ AUDIT_PENDING — {_clean(reason) or 'a human has to finish this one'}"
    return f"✗ FAILED — {_clean(reason) or 'no reason recorded'}"


def block(
    status: str | None,
    *,
    role: str | None = "",
    org: str | None = "",
    reason: str | None = "",
    where: str | None = "",
    tab: str | None = "",
    ledger_id: int | None = None,
) -> list[str]:
    """The whole terminal block, rules included, for `status`."""
    family = _norm(status)
    lines = [RULE, headline(status, reason), f"  job     {job_line(role, org)}"]
    if tab:
        lines.append(f"  tab     {_clean(tab, 120)}")
    if where:
        lines.append(f"  where   {_clean(where, 120)}")
    ledger_cell = f"#{ledger_id}" if ledger_id is not None else "(NOT WRITTEN)"
    lines.append(f"  ledger  {ledger_cell} {_clean(status) or 'unknown'}")
    lines.append(f"  next    {_NEXT_STEP[family]}")
    lines.append(RULE)
    return lines


def text(*args, **kwargs) -> str:
    """`block()` as one string — what the ledger row stores."""
    return "\n".join(block(*args, **kwargs))


# ------------------------------------------------------------- notification


def notification(status: str | None, role: str | None = "", org: str | None = "") -> str:
    """`FILLED + HELD — Design Engineer @ Ramp` — the body of the ping."""
    word = _NOTIFY_WORD.get(_norm(status), "FAILED")
    return f"{word} — {job_line(role, org)}"


def wants_notification(flag: bool | None, visible: bool) -> bool:
    """--notify / --no-notify win; unset means on for a --visible run."""
    if flag is None:
        return bool(visible)
    return bool(flag)


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=False, capture_output=True, timeout=10)


def notify(message: str, *, title: str = "weaver") -> bool:
    """Post a macOS notification. False (never an exception) if it can't."""
    if not _is_macos():
        return False
    script = (
        f'display notification "{_applescript_str(message)}" '
        f'with title "{_applescript_str(title)}"'
    )
    try:
        _run(["osascript", "-e", script])
    except Exception:  # osascript missing, sandboxed, timed out — never fatal
        return False
    return True


def _applescript_str(value: str) -> str:
    """AppleScript string literal body — quotes and backslashes escaped."""
    return _clean(value, 200).replace("\\", "\\\\").replace('"', '\\"')
