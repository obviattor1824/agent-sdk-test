"""MCP server registration, shared by every entry point in this repo.

Defined once here and imported by run.py and server.py so the CLI harness and
the HTTP harness cannot drift apart — and so that adding a tool means editing
one file, not hunting for every allowlist.

The server itself lives in the sibling repo ~/repos/obviattor/mcp-clients and
knows nothing about this harness. Nothing in this file is sent to it; this is
purely instructions for how to spawn the process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Sibling by construction, not by hardcoded path — but overridable, because the
# absolute interpreter path is the one thing MCP genuinely needs and a moved
# checkout should be fixable without editing source.
MCP_CLIENTS_ROOT = Path(
    os.environ.get("MCP_CLIENTS_ROOT", REPO_ROOT.parent / "mcp-clients")
).resolve()

# Its own venv, not ours: this venv is capped at mcp 1.x by claude-agent-sdk
# while that server needs 2.x, and an MCP server is spawned as a bare subprocess
# with no shell and no activated environment, so the interpreter has to be named
# absolutely.
MCP_CLIENTS_PYTHON = MCP_CLIENTS_ROOT / ".venv" / "bin" / "python"
MCP_CLIENTS_SERVER = MCP_CLIENTS_ROOT / "server.py"

SERVER_NAME = "clients"

MCP_SERVERS: dict[str, dict] = {
    SERVER_NAME: {
        "type": "stdio",
        "command": str(MCP_CLIENTS_PYTHON),
        "args": [str(MCP_CLIENTS_SERVER)],
    }
}

# Allowlisted by name, one entry per tool. This has to be extended by hand when
# a server gains a tool, which is deliberate: a new tool appearing inside a
# server the harness already trusts should not become callable just because the
# server is registered. Someone decides, per tool.
MCP_ALLOWED_TOOLS: tuple[str, ...] = (
    f"mcp__{SERVER_NAME}__lookup_client",
)


# Asks the server's own interpreter to load the server file. __name__ is not
# "__main__" under this loader, so server.run() does not fire and this cannot
# hang on a server whose module body ends in a blocking transport.
_IMPORT_PROBE = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('_probe', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "sys.modules['_probe'] = module\n"
    "spec.loader.exec_module(module)\n"
)


def preflight(*, stream=sys.stderr) -> bool:
    """Warn if the server could not possibly start.

    A misspelled command is not an error the SDK surfaces loudly: the server
    fails to spawn, the tool never appears, and the model simply answers from
    guesswork. That looks like a bad model rather than a broken path, so check
    the two files exist before blaming anything else.

    Existence is not enough. A venv built against the wrong major version of
    mcp passes every path check and still dies on import, silently and in the
    same way. So the interpreter is asked to load the server file, and what it
    says on the way down is printed verbatim rather than paraphrased.
    """
    missing = [p for p in (MCP_CLIENTS_PYTHON, MCP_CLIENTS_SERVER) if not p.exists()]
    if missing:
        print(f"WARNING: MCP server {SERVER_NAME!r} cannot start.", file=stream)
        for path in missing:
            print(f"  missing: {path}", file=stream)
        print(
            "  Set MCP_CLIENTS_ROOT, or create the venv:\n"
            f"    python3.13 -m venv {MCP_CLIENTS_ROOT}/.venv\n"
            f"    {MCP_CLIENTS_ROOT}/.venv/bin/pip install -r {MCP_CLIENTS_ROOT}/requirements.txt",
            file=stream,
        )
        return False

    try:
        probe = subprocess.run(
            [str(MCP_CLIENTS_PYTHON), "-c", _IMPORT_PROBE, str(MCP_CLIENTS_SERVER)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"WARNING: MCP server {SERVER_NAME!r} could not be checked: "
            f"{type(exc).__name__}: {exc}",
            file=stream,
        )
        return False

    if probe.returncode == 0:
        return True

    print(f"WARNING: MCP server {SERVER_NAME!r} cannot start.", file=stream)
    print(f"  {MCP_CLIENTS_PYTHON}", file=stream)
    print(f"  cannot import {MCP_CLIENTS_SERVER}:", file=stream)
    for line in (probe.stderr or "").strip().splitlines()[-3:]:
        print(f"    {line}", file=stream)
    print(
        "  That venv is missing a dependency or holds the wrong version of one:\n"
        f"    {MCP_CLIENTS_ROOT}/.venv/bin/pip install -r {MCP_CLIENTS_ROOT}/requirements.txt",
        file=stream,
    )
    return False
