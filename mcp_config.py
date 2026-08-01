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
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Sibling by construction, not by hardcoded path — but overridable, because the
# absolute interpreter path is the one thing MCP genuinely needs and a moved
# checkout should be fixable without editing source.
MCP_CLIENTS_ROOT = Path(
    os.environ.get("MCP_CLIENTS_ROOT", REPO_ROOT.parent / "mcp-clients")
).resolve()

# Its own venv, not ours: that repo pins mcp 2.x while this one pins 1.x, and
# an MCP server is spawned as a bare subprocess with no shell and no activated
# environment, so the interpreter has to be named absolutely.
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


def preflight(*, stream=sys.stderr) -> bool:
    """Warn if the server could not possibly start.

    A misspelled command is not an error the SDK surfaces loudly: the server
    fails to spawn, the tool never appears, and the model simply answers from
    guesswork. That looks like a bad model rather than a broken path, so check
    the two files exist before blaming anything else.
    """
    missing = [p for p in (MCP_CLIENTS_PYTHON, MCP_CLIENTS_SERVER) if not p.exists()]
    if not missing:
        return True

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
