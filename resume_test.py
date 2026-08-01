#!/usr/bin/env python3
"""Test that ClaudeAgentOptions(resume=...) restores conversation memory.

Run 1 creates a CSV and reports a total. The query is then torn down completely
-- separate asyncio.run() calls, so the transport subprocess exits between runs
-- and Run 2 resumes by session_id and asks a question answerable only from the
prior conversation.

If resume works, Run 2 answers from memory and makes zero tool calls. Any tool
call means it went back to the filesystem instead, which defeats the point.

Usage:
    python resume_test.py
"""

import asyncio
import shutil
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    UserMessage,
    query,
)

# Reuse run.py's logging and rendering so the JSONL format is identical.
from run import StreamLog, dump, render_block, show

ROOT = Path(__file__).parent
WORKSPACE = ROOT / "workspaces" / "resume-test"
LOG_DIR = ROOT / "logs"

PROMPT_1 = (
    "Create a CSV of 12 fictional invoices "
    "(invoice_number, client_name, issue_date, amount_eur, status) "
    "and tell me the total."
)
PROMPT_2 = (
    "Which client did you give the largest invoice to, "
    "and why did you pick that name?"
)


def make_options(resume: str | None = None) -> ClaudeAgentOptions:
    """Identical options for both runs, except `resume`.

    cwd must match across runs: sessions are stored under an encoding of the
    working directory, so resuming from elsewhere silently starts fresh.
    """
    return ClaudeAgentOptions(
        cwd=str(WORKSPACE),
        setting_sources=[],
        allowed_tools=["Read", "Write", "Bash", "Glob"],
        permission_mode="bypassPermissions",
        max_turns=20,
        resume=resume,
    )


async def do_run(label: str, prompt: str, log_name: str, resume: str | None = None) -> dict:
    """Run one query to completion, logging every message. Returns a summary."""
    options = make_options(resume)
    log = StreamLog(LOG_DIR / log_name)

    print("\n" + "=" * 72)
    print(f"{label}: {prompt}")
    print(f"resume={resume!r}  log={log_name}")
    print("=" * 72)

    log.write(
        "run_start",
        {
            "label": label,
            "prompt": prompt,
            "cwd": str(WORKSPACE),
            "setting_sources": options.setting_sources,
            "allowed_tools": options.allowed_tools,
            "permission_mode": options.permission_mode,
            "max_turns": options.max_turns,
            "resume": resume,
        },
    )

    init_session_id = None
    result_session_id = None
    tool_calls: list[str] = []

    try:
        async for message in query(prompt=prompt, options=options):
            log.write("message", message)

            if isinstance(message, SystemMessage):
                if message.subtype == "init" and isinstance(message.data, dict):
                    # Python nests the id inside .data (TypeScript exposes it directly).
                    init_session_id = message.data.get("session_id")
                    print(f"\n[init] session_id = {init_session_id}")
                else:
                    show(f"SystemMessage: {message.subtype}", dump(message.data))

            elif isinstance(message, AssistantMessage):
                show(f"AssistantMessage (stop_reason={message.stop_reason})")
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_calls.append(block.name)
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
                # Present on every result, success or error -- the reliable source.
                result_session_id = message.session_id
                show(
                    f"ResultMessage: {message.subtype}",
                    "\n".join(
                        [
                            f"session_id:     {message.session_id}",
                            f"num_turns:      {message.num_turns}",
                            f"total_cost_usd: {message.total_cost_usd}",
                            f"result:         {message.result}",
                        ]
                    ),
                )

            else:
                show(type(message).__name__, repr(message))

    except Exception as exc:
        # A single-shot query() raises after yielding an error result; the ids
        # above may already be captured.
        log.write("error", {"type": type(exc).__name__, "message": str(exc)})
        print(f"\n[{label}] query raised: {type(exc).__name__}: {exc}")
    finally:
        log.close()

    return {
        "label": label,
        "init_session_id": init_session_id,
        "result_session_id": result_session_id,
        "tool_calls": tool_calls,
    }


def main() -> int:
    # Fresh workspace so run 1 genuinely starts from nothing.
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)
    LOG_DIR.mkdir(exist_ok=True)
    print(f"workspace emptied: {WORKSPACE}")

    # Separate asyncio.run() calls: the event loop and the transport subprocess
    # are torn down between them, so run 2 cannot be a continued connection.
    one = asyncio.run(do_run("RUN 1", PROMPT_1, "resume-sdk-1.jsonl"))

    session_id = one["init_session_id"] or one["result_session_id"]
    if not session_id:
        print("\nFATAL: run 1 produced no session_id; cannot test resume.")
        return 1

    two = asyncio.run(do_run("RUN 2", PROMPT_2, "resume-sdk-2.jsonl", resume=session_id))

    # ---------------- summary ----------------
    sid1 = one["init_session_id"] or one["result_session_id"]
    sid2 = two["init_session_id"] or two["result_session_id"]
    calls = two["tool_calls"]

    print("\n" + "=" * 72)
    print("RESUME TEST SUMMARY")
    print("=" * 72)
    print(f"run 1 session_id: {sid1}")
    print(f"run 2 session_id: {sid2}")
    print(f"ids match:        {sid1 == sid2}")
    print(f"run 2 tool calls: {len(calls)}{' -> ' + ', '.join(calls) if calls else ''}")

    if not sid1 == sid2:
        print()
        print("!" * 72)
        print("!! WARNING: session ids differ. With fork_session=False a resumed")
        print("!! session should keep its id -- a new id means resume did not")
        print("!! attach and run 2 started a fresh conversation.")
        print("!" * 72)

    if calls:
        print()
        print("!" * 72)
        print(f"!! WARNING: run 2 made {len(calls)} tool call(s).")
        print("!! It should have answered from conversation memory alone. Reading")
        print("!! the file back suggests the resumed context was not available.")
        print("!" * 72)
    else:
        print("\nOK: run 2 answered with no tool calls -- resumed from memory.")

    print(f"\nlogs: {LOG_DIR / 'resume-sdk-1.jsonl'}")
    print(f"      {LOG_DIR / 'resume-sdk-2.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
