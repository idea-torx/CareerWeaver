"""`weaver-mcp` — the CLI, exposed over MCP stdio.

An adapter, not a rewrite: every tool shells into `weaver.cli.main` with
`--json` and hands the JSON straight back. No new domain logic lives here.

Hand-rolled JSON-RPC rather than the official SDK, which drags uvicorn +
starlette + pydantic in to read stdin. The wire surface is three methods.

stdout is the protocol channel, so `cli.main` runs under `redirect_stdout`;
anything it prints is captured, never leaked into the stream.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .cli import main as cli_main

PROTOCOL_VERSION = "2024-11-05"


def data_dir() -> str:
    """Where weaver.db lives, resolved once per call.

    The CLI defaults to ./data relative to cwd, which is fine for a shell and
    wrong for us: an MCP client spawns this server with whatever cwd it likes,
    so the default would silently point at an empty directory. Every config
    snippet in the README sets WEAVER_DATA_DIR for exactly this reason.
    """
    return os.environ.get("WEAVER_DATA_DIR") or str(Path.cwd() / "data")

#: name -> (description, JSON-schema properties, required, argv builder).
#: Adding a tool is one entry; there is deliberately no registry class.
Tool = tuple[str, dict[str, Any], list[str], Callable[[dict], list[str]]]

TOOLS: dict[str, Tool] = {
    "weaver_stats": (
        "Fact-graph statistics: how many verified facts, roles, and lenses exist. "
        "Cheap first call to see what the applicant profile actually contains.",
        {},
        [],
        lambda a: ["stats"],
    ),
    "weaver_lens_list": (
        "List persona lenses. A lens selects which facts a tailored resume draws on.",
        {},
        [],
        lambda a: ["lens", "list"],
    ),
    "weaver_jobs_add": (
        "Save a job posting (url, file path, or raw posting text) and return its job_id.",
        {
            "value": {"type": "string", "description": "url, file path, or raw posting text"},
            "fetch": {"type": "boolean", "description": "fetch the url over the network"},
        },
        ["value"],
        lambda a: ["jobs", "add", a["value"]] + (["--fetch"] if a.get("fetch") else []),
    ),
    "weaver_jobs_list": (
        "List saved job postings with their job_ids.",
        {},
        [],
        lambda a: ["jobs", "list"],
    ),
    "weaver_preflight": (
        "Audit a saved job's form questions against the applicant's facts. "
        "No browser. Reports which fields are covered by evidence and which are not.",
        {"job_id": {"type": "integer"}},
        ["job_id"],
        lambda a: ["preflight", str(a["job_id"])],
    ),
    "weaver_tailor": (
        "Generate a tailored resume from the fact graph for a saved job. "
        "Returns the resume_id and output path.",
        {
            "job": {"type": "string", "description": "job id, url, file, or raw posting text"},
            "lens": {"type": "string", "description": "lens name (see weaver_lens_list)"},
            "source": {"type": "string", "description": "source resume file, or 'graph' for the whole fact graph"},
            "format": {"type": "string", "enum": ["md", "docx", "pdf"]},
        },
        ["job"],
        lambda a: ["tailor", a.get("source") or "graph", "--job", str(a["job"])]
        + (["--lens", a["lens"]] if a.get("lens") else [])
        + (["--format", a["format"]] if a.get("format") else []),
    ),
    "weaver_apply_hold": (
        "Fill a job application in a visible local Chrome window and STOP before "
        "submitting — parks at audit_pending for a human to review and send. "
        "Never submits. Drives a real browser, so it can run for minutes.",
        {
            "resume_id": {"type": "integer", "description": "from weaver_tailor"},
            "cover": {"type": "string", "description": "cover letter file path or URL"},
        },
        ["resume_id"],
        lambda a: ["apply", str(a["resume_id"]), "--hold", "--visible"]
        + (["--cover", a["cover"]] if a.get("cover") else []),
    ),
    "weaver_apps_list": (
        "List application ledger rows: what was filled, held, or submitted, and when.",
        {},
        [],
        lambda a: ["apps", "list"],
    ),
    "weaver_apps_show": (
        "Show one application's full receipt: the payload that was filled and its status.",
        {"application_id": {"type": "integer"}},
        ["application_id"],
        lambda a: ["apps", "show", str(a["application_id"])],
    ),
}


def run_cli(argv: list[str]) -> tuple[int, str]:
    """Run the CLI in-process with stdout captured. Returns (exit_code, output).

    os.environ is snapshotted and restored: every command loads <data-dir>/env
    into the environment, and this server is long-lived, so without the restore
    one call's credentials would bleed into every later call.
    """
    buf = io.StringIO()
    saved = os.environ.copy()
    try:
        with contextlib.redirect_stdout(buf):
            code = cli_main([*argv, "--data-dir", data_dir(), "--json"])
    except SystemExit as exc:  # argparse usage errors
        code = int(exc.code or 0)
    except Exception as exc:  # a crash is a tool error, not a dead server
        return 1, json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return code, buf.getvalue().strip()


def dispatch(method: str, params: dict) -> Any:
    if method == "initialize":
        return {
            # Echo the client's version when it sends one: every current client
            # speaks a version we can serve, and echoing avoids a false mismatch.
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "careerweaver", "version": __version__},
        }
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": name,
                    "description": desc,
                    "inputSchema": {"type": "object", "properties": props, "required": req},
                }
                for name, (desc, props, req, _) in TOOLS.items()
            ]
        }
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            raise LookupError(f"unknown tool: {name}")
        code, out = run_cli(TOOLS[name][3](params.get("arguments") or {}))
        return {
            "content": [{"type": "text", "text": out or f"(no output, exit {code})"}],
            # Exit 3 is `audit_pending` — a held application is a success.
            "isError": code not in (0, 3),
        }
    raise NotImplementedError(method)


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:  # a notification: acknowledged by silence
            continue
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": dispatch(msg.get("method", ""), msg.get("params") or {})}
        except NotImplementedError as exc:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"method not found: {exc}"}}
        except Exception as exc:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(exc)}}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
