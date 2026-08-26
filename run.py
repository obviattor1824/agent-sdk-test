#!/usr/bin/env python3
"""Minimal Claude Agent SDK harness: run a task and dump the raw message stream.

Usage:
    python run.py "your task here"
    python run.py --no-log "task"           # console only
    python run.py --log-dir /tmp/runs "task"

Every message from query() is printed with its type. Nothing is filtered.
Unless --no-log is passed, the stream is also written to a timestamped JSONL
file, one JSON object per message.
"""

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from mcp_config import MCP_ALLOWED_TOOLS, MCP_SERVERS, preflight

ROOT = Path(__file__).parent
# Each entry point gets its own subdirectory under workspaces/ so the CLI, the
# resume test and each HTTP job never share a working directory.
WORKSPACE = ROOT / "workspaces" / "cli"


# --------------------------------------------------------------------------
# Console output
# --------------------------------------------------------------------------

def show(label: str, body: str = "") -> None:
    """Print a message with its type as the header."""
    print(f"\n=== {label} ===")
    if body:
        print(body)


def dump(value, limit: int = 2000) -> str:
    """Render a tool input/result as JSON, truncating only absurdly long values.

    Console-only. The JSONL log always stores the untruncated value.
    """
    try:
        text = json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > limit:
        text = f"{text[:limit]}\n... [{len(text) - limit} more chars]"
    return text


def check_init(data: dict) -> None:
    """Report the environment the agent actually booted with.

    This run is meant to be a clean harness: setting_sources=[] should mean no
    user/project settings, and therefore no skills or plugins leaking in from
    disk. The init message is the only place that is observable, so assert on it.

    MCP servers are the exception, and the distinction matters. The servers in
    MCP_SERVERS are passed to the SDK explicitly and are SUPPOSED to be here.
    Any OTHER server was loaded from ~/.claude or .claude/ and means isolation
    has failed. So this checks the set, not merely whether it is empty.
    """
    tools = data.get("tools") or []
    skills = data.get("skills") or []
    mcp_servers = data.get("mcp_servers") or []
    plugins = data.get("plugins") or []

    # mcp_servers entries are dicts like {"name": ..., "status": ...}
    mcp_names = [
        m.get("name", repr(m)) if isinstance(m, dict) else repr(m) for m in mcp_servers
    ]
    mcp_status = {
        m.get("name"): m.get("status") for m in mcp_servers if isinstance(m, dict)
    }

    print("\n--- init environment check ---")
    print(f"tools ({len(tools)}):       {', '.join(tools) if tools else '(none)'}")
    print(f"skills ({len(skills)}):      {', '.join(skills) if skills else '(none)'}")
    print(f"mcp_servers ({len(mcp_names)}): {', '.join(mcp_names) if mcp_names else '(none)'}")
    print(f"plugins ({len(plugins)}):     {len(plugins)} loaded" if plugins else "plugins (0):     (none)")

    # Tools and skills above are shipped inside the bundled Claude Code binary
    # and are expected to be non-empty -- they ARE the out-of-the-box product.
    # Plugins can only come from ~/.claude or .claude/, so with setting_sources=[]
    # any plugin means settings isolation has failed. Same for any MCP server we
    # did not ask for by name.
    expected = set(MCP_SERVERS)
    unexpected = sorted(n for n in mcp_names if n not in expected)
    missing = sorted(expected - set(mcp_names))
    # "pending" is deliberately not a failure: servers connect asynchronously
    # and init is emitted before they have all finished. Observed pending here
    # and answering correctly three messages later. Anything else unrecognised
    # is treated as broken, so a new status warns rather than passing silently.
    pending = sorted(n for n in expected if mcp_status.get(n) == "pending")
    disconnected = sorted(
        n for n in expected
        if n in mcp_status and mcp_status[n] not in ("connected", "pending")
    )

    dirty = []
    if unexpected:
        dirty.append(f"{len(unexpected)} unrequested MCP server(s): {', '.join(unexpected)}")
    if plugins:
        dirty.append(f"{len(plugins)} plugin(s)")

    if dirty:
        print()
        print("!" * 72)
        print(f"!! WARNING: settings leaked -- {'; '.join(dirty)}.")
        print("!! strict_mcp_config=True should have prevented this. This run is")
        print("!! not isolated from your machine's configuration.")
        print("!" * 72)
    else:
        print("OK: only the MCP servers this run asked for; no plugins leaked.")

    # A registered server that failed to spawn is the quiet failure mode: the
    # tool never appears and the model answers from guesswork instead. Say so.
    if missing or disconnected:
        print()
        print("!" * 72)
        for name in missing:
            print(f"!! MCP server {name!r} was registered but is absent from init.")
        for name in disconnected:
            print(f"!! MCP server {name!r} status is {mcp_status[name]!r}, not 'connected'.")
        print("!! Its tools are NOT available; answers about that domain will be guesses.")
        print("!" * 72)

    if pending:
        print(f"note: still connecting at init: {', '.join(pending)} "
              f"(normal; they usually finish before the first tool call)")

    print("--- end init check ---")


def render_block(block) -> None:
    """Print one content block, falling back to repr for unrecognised types."""
    if isinstance(block, TextBlock):
        show("TextBlock", block.text)
    elif isinstance(block, ThinkingBlock):
        show("ThinkingBlock", block.thinking)
    elif isinstance(block, ToolUseBlock):
        show(f"ToolUseBlock: {block.name}", f"id: {block.id}\ninput: {dump(block.input)}")
    elif isinstance(block, ToolResultBlock):
        show(
            f"ToolResultBlock (is_error={block.is_error})",
            f"tool_use_id: {block.tool_use_id}\ncontent: {dump(block.content)}",
        )
    else:
        # Server tools, and anything the SDK adds later.
        show(type(block).__name__, repr(block))


# --------------------------------------------------------------------------
# JSONL logging
# --------------------------------------------------------------------------

def encode(obj):
    """Recursively convert SDK objects to JSON-safe values.

    Dataclasses keep their class name under "_type" -- without it, TextBlock and
    ToolUseBlock would both flatten to anonymous dicts and the log would be
    ambiguous. Anything unrecognised degrades to repr() rather than raising.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {"_type": type(obj).__name__}
        for field in dataclasses.fields(obj):
            out[field.name] = encode(getattr(obj, field.name))
        return out
    if isinstance(obj, dict):
        return {str(k): encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [encode(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return repr(obj)


class StreamLog:
    """Append-only JSONL sink. A no-op when disabled."""

    def __init__(self, path: Path | None):
        self.path = path
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")

    def write(self, kind: str, payload) -> None:
        if self._fh is None:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "payload": encode(payload),
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        # Flush per message so a crashed or interrupted run still leaves a
        # readable log up to the failure point.
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


# --------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a task through the Claude Agent SDK and dump the raw message stream."
    )
    parser.add_argument("task", nargs="+", help="the task to give the agent")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=ROOT / "logs",
        help="directory for JSONL logs (default: ./logs)",
    )
    parser.add_argument("--no-log", action="store_true", help="console output only")
    return parser.parse_args(argv)


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    task = " ".join(args.task)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    preflight()

    log_path = None
    if not args.no_log:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = args.log_dir / f"run-{stamp}.jsonl"
    log = StreamLog(log_path)

    options = ClaudeAgentOptions(
        cwd=str(WORKSPACE),
        max_turns=20,
        # Empty list = load NO user/project/local settings from disk, so no
        # skills, plugins, MCP servers or custom tools leak in from ~/.claude
        # or .claude. Requires SDK > 0.1.59; None (the default) loads them all.
        setting_sources=[],
        # Passed to the SDK directly, so neither flag above or below excludes
        # it -- this one server still arrives.
        mcp_servers=MCP_SERVERS,
        # setting_sources=[] only blocks servers configured on disk. It does
        # nothing about account-level connectors on claude.ai, which arrive by
        # another route entirely and showed up on some runs and not others with
        # no config change. This is the switch that excludes them; it maps to
        # the CLI's --strict-mcp-config, which the CLI comparison runs always
        # had and the SDK side did not.
        strict_mcp_config=True,
        # NB: `tools` is deliberately left unset. The binary's own ~31 tools and
        # ~16 skills are the out-of-the-box product; restricting them would mean
        # testing a configured harness rather than the default one.
        #
        # Pre-approved without prompting. Note this does NOT restrict Claude to
        # only these tools -- it is an allow list for approval, not a whitelist.
        # (permission_mode below approves everything anyway; the MCP tool is
        # named here so this still behaves if the mode is ever tightened.)
        allowed_tools=["Read", "Write", "Bash", "Glob", *MCP_ALLOWED_TOOLS],
        # Skip permission checks entirely. NB: cwd sets the working directory but
        # does NOT sandbox writes -- under bypassPermissions the agent can and does
        # write outside it (e.g. /tmp). acceptEdits blocks those; this does not.
        permission_mode="bypassPermissions",
    )

    print(f"task:            {task}")
    print(f"cwd:             {WORKSPACE}")
    print(f"max_turns:       {options.max_turns}")
    print(f"setting_sources: {options.setting_sources}")
    print(f"allowed_tools:   {options.allowed_tools}")
    print(f"permission_mode: {options.permission_mode}")
    print(f"mcp_servers:     {', '.join(sorted(MCP_SERVERS)) or '(none)'}")
    print(f"log:             {log_path or '(disabled)'}")

    log.write(
        "run_start",
        {
            "task": task,
            "cwd": str(WORKSPACE),
            "max_turns": options.max_turns,
            "setting_sources": options.setting_sources,
            "allowed_tools": options.allowed_tools,
            "permission_mode": options.permission_mode,
            "mcp_servers": sorted(MCP_SERVERS),
        },
    )

    try:
        async for message in query(prompt=task, options=options):
            log.write("message", message)

            if isinstance(message, SystemMessage):
                show(f"SystemMessage: {message.subtype}", dump(message.data))
                if message.subtype == "init" and isinstance(message.data, dict):
                    check_init(message.data)

            elif isinstance(message, AssistantMessage):
                show(
                    f"AssistantMessage (model={message.model}, "
                    f"stop_reason={message.stop_reason})"
                )
                for block in message.content:
                    render_block(block)

            elif isinstance(message, UserMessage):
                show("UserMessage")
                content = message.content
                if isinstance(content, str):
                    print(content)
                else:
                    for block in content:
                        render_block(block)

            elif isinstance(message, ResultMessage):
                show(
                    f"ResultMessage: {message.subtype}",
                    "\n".join(
                        [
                            f"is_error:       {message.is_error}",
                            f"num_turns:      {message.num_turns}",
                            f"total_cost_usd: {message.total_cost_usd}",
                            f"duration_ms:    {message.duration_ms} (api: {message.duration_api_ms})",
                            f"stop_reason:    {message.stop_reason}",
                            f"session_id:     {message.session_id}",
                            f"usage:          {dump(message.usage)}",
                            f"result:         {message.result}",
                        ]
                    ),
                )

            else:
                # StreamEvent, RateLimitEvent, task notifications, future types.
                show(type(message).__name__, repr(message))

    except BaseException as exc:  # includes KeyboardInterrupt
        log.write("error", {"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        log.close()
        if log_path is not None:
            print(f"\nstream written to {log_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
