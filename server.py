#!/usr/bin/env python3
"""HTTP job queue over the Claude Agent SDK.

POST /run accepts work and returns 202 immediately; the agent runs in a
background task. Nothing here streams -- the job outlives any connection, which
is the point: duration, concurrency, cancellation and client-disconnect are all
observable as separate things.

Run:
    ./.venv/bin/uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shlex
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from mcp_config import MCP_ALLOWED_TOOLS, MCP_SERVERS, preflight
from pydantic import BaseModel

# Reuse run.py's JSONL logging so every surface writes the same format.
from run import StreamLog, encode

ROOT = Path(__file__).parent
WORKSPACES = ROOT / "workspaces"
LOG_DIR = ROOT / "logs"

# A job is considered unwatched if no client has polled it within this window.
POLL_STALE_SECONDS = 30.0

JobStatus = Literal["queued", "running", "done", "error", "cancelled"]


# ---------------------------------------------------------------------------
# Job state (in-memory; a restart loses everything, which is fine for a test)
# ---------------------------------------------------------------------------

@dataclass
class Job:
    job_id: str
    task: str
    workspace: Path
    log_path: Path
    resume: str | None = None

    status: JobStatus = "queued"
    session_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None

    num_turns: int | None = None
    total_cost_usd: float | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    terminal_reason: str | None = None

    final_text: str | None = None
    error: str | None = None

    events: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    permission_log: list[dict[str, Any]] = field(default_factory=list)

    # Workspace contents before the agent started, so the file listing can show
    # what THIS job produced. A resumed job inherits a populated directory.
    workspace_before: dict[str, tuple[int, int]] = field(default_factory=dict)

    # Disconnect tracking. last_polled_at is refreshed by any GET on this job.
    last_polled_at: float | None = None
    client_disconnected: bool | None = None

    task_handle: asyncio.Task | None = None
    client: ClaudeSDKClient | None = None

    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return round(end - self.created_at, 3)


JOBS: dict[str, Job] = {}
# session_id -> workspace, so a resume reuses the directory the session ran in.
# cwd must match or the SDK looks for the transcript in the wrong place.
SESSION_WORKSPACES: dict[str, Path] = {}


# ---------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Permission policy
# --------------------------------------------------------------------------
#
# Two kinds of location are readable from outside the workspace. Neither is
# writable -- see bash_write_targets below, which judges write destinations on
# workspace containment alone so that being readable never launders a write.
#
# 1. Standard interpreter/binary locations. Without these, `/usr/bin/python3 x.py`
#    would be denied for referencing a path outside the workspace, which breaks
#    tasks that legitimately worked before.
# 2. Claude Code's bundled skills. The binary unpacks them under /tmp instead of
#    into the workspace, so a skill's own assets -- the dataviz palette validator,
#    for instance -- look like an escape to a workspace-only rule. They are
#    harness-provided resources, not job data. Only the bundled-skills directory
#    opens up: its siblings under /tmp/claude-<uid>/ are per-session scratch
#    directories for other projects and stay shut.
EXEC_PREFIXES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin")

BUNDLED_SKILLS = os.environ.get(
    "BUNDLED_SKILLS_DIR", f"/tmp/claude-{os.getuid()}/bundled-skills"
)

# Each root in both spellings, because the two ends of the comparison disagree
# about symlinks and each has to. On macOS /tmp IS a symlink to /private/tmp, so
# a bundled-skills path only matches once /private/tmp is a known root; but the
# token being tested is deliberately not fully resolved (see lexical_path), so
# the unresolved spelling has to be known too.
READ_ONLY_ROOTS = tuple(
    dict.fromkeys(
        form
        for raw in (*EXEC_PREFIXES, BUNDLED_SKILLS)
        for form in (Path(raw), Path(raw).resolve())
    )
)

# Absolute paths, and relative paths that climb with "..". Quotes and shell
# metacharacters terminate a token. The lookbehind stops `s/a/b/` in a sed
# expression from being read as the path `/a/b/`.
_ABS_PATH_RE = re.compile(r"""(?<![\w=])(/[^\s;|&"'<>()]*)""")
_DOTDOT_RE = re.compile(r"""(?<![\w=])((?:\.\./)[^\s;|&"'<>()]*)""")

# Shell constructs that name a file the command writes to. `>` and `>>` cover
# redirection (including `2>f`); the rest are commands whose destination is the
# last argument, or, for tee, every argument.
_REDIRECT_RE = re.compile(r""">>?\s*([^\s;|&"'<>()]+)""")
_SEGMENT_RE = re.compile(r"&&|\|\||[;|\n]")
_DEST_LAST_CMDS = {"cp", "mv", "install", "ln", "rsync"}
_DEST_ALL_CMDS = {"tee"}


def resolve_path(raw: str, base: Path) -> Path:
    """Resolve to a real absolute path: symlinks followed, '..' collapsed.

    Relative paths resolve against the job workspace (the agent's cwd).
    strict=False so paths that don't exist yet (a Write target) still resolve.
    """
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def lexical_path(raw: str, base: Path) -> Path:
    """Absolute and '..'-free, but without following symlinks.

    Used only for the read-only allowlist. Most of /opt/homebrew/bin is symlinks
    into ../Cellar, so testing the fully resolved path would deny the very
    interpreters EXEC_PREFIXES exists to permit. Collapsing '..' lexically is
    still enough to stop /usr/bin/../../etc/passwd from passing as a binary.
    """
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    return Path(os.path.normpath(str(p)))


def is_inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def is_read_only_asset(path: Path) -> bool:
    return any(is_inside(path, root) for root in READ_ONLY_ROOTS)


def bash_path_tokens(command: str) -> list[str]:
    """Every path-like token in a shell command that could escape the cwd."""
    return _ABS_PATH_RE.findall(command) + _DOTDOT_RE.findall(command)


def bash_write_targets(command: str) -> list[str]:
    """Paths the command would write to, as opposed to read or execute.

    Deliberately conservative rather than a shell parser: it catches redirection
    and the handful of copy/move commands an agent actually reaches for. A write
    smuggled through something not listed here still has to name a path, and any
    path outside the workspace that is not a read-only asset is denied anyway --
    so the gap is a write *into* a read-only root by an unlisted command, not a
    write to somewhere arbitrary.
    """
    targets = _REDIRECT_RE.findall(command)

    for segment in _SEGMENT_RE.split(command):
        # Redirections are already collected; strip them so the destination of
        # `cp a b > log` is read as b rather than log.
        try:
            words = shlex.split(_REDIRECT_RE.sub(" ", segment))
        except ValueError:
            continue
        if not words:
            continue
        name = Path(words[0]).name
        args = [w for w in words[1:] if not w.startswith("-")]
        if name in _DEST_LAST_CMDS and args:
            targets.append(args[-1])
        elif name in _DEST_ALL_CMDS:
            targets.extend(args)

    return targets


# ---------------------------------------------------------------------------
# Workspace files
# ---------------------------------------------------------------------------

def snapshot_workspace(root: Path) -> dict[str, tuple[int, int]]:
    """(size, mtime_ns) for every file in the workspace, keyed by relative path.

    Compared against a later scan to work out what a job actually produced.
    Comparing snapshots rather than filtering on "modified since I started"
    avoids depending on mtime granularity or on the wall clock moving forwards.
    """
    found: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return found
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        found[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return found


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def job_files(job: Job) -> list[dict[str, Any]]:
    """Files created or modified since this job started, sorted by name."""
    before = job.workspace_before
    files = []
    for name, (size, mtime_ns) in snapshot_workspace(job.workspace).items():
        if before.get(name) == (size, mtime_ns):
            continue
        files.append(
            {
                "name": name,
                "size": size,
                "mime": guess_mime(name),
                "modified": datetime.fromtimestamp(
                    mtime_ns / 1e9, timezone.utc
                ).isoformat(),
            }
        )
    files.sort(key=lambda f: f["name"])
    return files


def resolve_workspace_file(job: Job, filename: str) -> Path:
    """Resolve a requested name inside the job workspace, or refuse.

    The sandbox the permission callback puts around the agent would be worth
    little if the read-back endpoint could be walked out of, so this repeats the
    same containment test. Absolute paths are rejected outright rather than
    joined: Path("/ws") / "/etc/passwd" is "/etc/passwd", so joining is not by
    itself a containment check. Everything else is resolved -- '..' collapsed and
    symlinks followed -- before being tested, which catches both a literal
    ../../ and a symlink inside the workspace pointing out of it.
    """
    root = job.workspace.resolve()
    if not filename or Path(filename).is_absolute():
        raise HTTPException(status_code=400, detail=f"invalid filename {filename!r}")

    target = (root / filename).resolve()
    if not is_inside(target, root):
        raise HTTPException(
            status_code=403,
            detail=f"{filename!r} resolves to {target}, outside this job's workspace",
        )
    if not target.is_file():
        raise HTTPException(
            status_code=404, detail=f"no such file {filename!r} in this job's workspace"
        )
    return target


def make_permission_callback(job: "Job", log: StreamLog):
    """Build a can_use_tool callback scoped to one job's workspace.

    Denial messages are returned to the model as the tool result, so they are
    written to be actionable: they name the offending path AND the directory
    that is allowed, so the model can retry correctly instead of flailing.
    """
    root = job.workspace.resolve()

    def record(tool: str, tool_input: dict, allowed: bool, reason: str):
        entry = {
            "tool": tool,
            "input": tool_input,
            "decision": "allow" if allowed else "deny",
            "reason": reason,
        }
        job.permission_log.append(entry)
        log.write("permission", entry)
        # Also into the event stream, in sequence with the messages around it.
        # Without this a permission decision is only visible on GET /jobs/{id},
        # so /stream -- the "everything" view -- is the one place a denial does
        # not appear. _type mirrors the encoded SDK messages so a client can
        # dispatch on it the same way.
        job.events.append(encode({"_type": "PermissionDecision", **entry}))
        return (
            PermissionResultAllow()
            if allowed
            else PermissionResultDeny(message=reason, interrupt=False)
        )

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ):
        # ---- file tools: judge the single path they operate on ----
        # Edit belongs here for the same reason Write does: one file_path, one
        # containment test. Leaving it out did not make the job safer, it just
        # pushed the model into rewriting whole files with Write to change a line.
        if tool_name in ("Read", "Write", "Edit", "Glob"):
            raw = tool_input.get("file_path") or tool_input.get("path")
            if raw is None:
                # Glob with no path defaults to cwd, which is the workspace.
                return record(tool_name, tool_input, True, f"no path given; defaults to {root}")
            target = resolve_path(str(raw), root)
            if is_inside(target, root):
                return record(tool_name, tool_input, True, f"{target} is inside {root}")
            return record(
                tool_name,
                tool_input,
                False,
                f"{tool_name} denied: {target} is outside this job's workspace. "
                f"This job may only read and write inside {root}. "
                f"Use a relative path, or an absolute path under that directory.",
            )

        # ---- bash: judge every path token the command references ----
        if tool_name == "Bash":
            command = str(tool_input.get("command", ""))

            # Writes first, and judged on containment alone. A read-only root is
            # readable, never writable: `echo x > /usr/bin/y` names a path under
            # an allowed prefix and is still an escape.
            for token in bash_write_targets(command):
                target = resolve_path(token, root)
                if not is_inside(target, root):
                    return record(
                        tool_name,
                        tool_input,
                        False,
                        f"Bash denied: the command writes to {token} "
                        f"(resolves to {target}), which is outside this job's workspace. "
                        f"Reading system paths is allowed; writing to them is not. "
                        f"Write inside {root}.",
                    )

            for token in bash_path_tokens(command):
                target = resolve_path(token, root)
                if is_inside(target, root) or is_read_only_asset(lexical_path(token, root)):
                    continue
                return record(
                    tool_name,
                    tool_input,
                    False,
                    f"Bash denied: the command references {token} "
                    f"(resolves to {target}), which is outside this job's workspace. "
                    f"Commands may only touch paths inside {root}, "
                    f"system binaries, and Claude Code's bundled skills. "
                    f"Run from the working directory and use relative paths.",
                )

            return record(
                tool_name,
                tool_input,
                True,
                f"all path tokens resolve inside {root} or a read-only system path",
            )

        # ---- MCP tools: allowlisted by name ----
        # The containment tests above are meaningless here. An MCP tool takes
        # domain arguments, not paths, and the work happens in a separate
        # process that never touches this workspace -- there is nothing to
        # resolve against root. So judge it by identity instead: named tools
        # are allowed, every other mcp__ tool is denied.
        #
        # Registering a server is therefore not the same as trusting it. If
        # clients/server.py grows a write_client tool tomorrow, it arrives here
        # unlisted and is refused until someone adds it to MCP_ALLOWED_TOOLS.
        if tool_name.startswith("mcp__"):
            if tool_name in MCP_ALLOWED_TOOLS:
                return record(
                    tool_name, tool_input, True, f"{tool_name} is on the MCP allowlist"
                )
            allowed = ", ".join(MCP_ALLOWED_TOOLS) or "(none)"
            return record(
                tool_name,
                tool_input,
                False,
                f"{tool_name} denied: it is not on this job's MCP allowlist. "
                f"The MCP tools available to this job are: {allowed}.",
            )

        # ---- anything else ----
        return record(
            tool_name,
            tool_input,
            False,
            f"{tool_name} denied: this job may only use Read, Write, Edit, Glob, "
            f"Bash and the allowlisted MCP tools.",
        )

    return can_use_tool


def make_options(workspace: Path, resume: str | None, can_use_tool) -> ClaudeAgentOptions:
    """Same options as run.py, but the callback decides every tool call.

    permission_mode is "default" and allowed_tools is empty ON PURPOSE. Per the
    SDK docs, an auto-approved tool never reaches can_use_tool: bypassPermissions
    approves everything at the mode step, and a bare name like "Read" in
    allowed_tools approves that tool before the callback is consulted. Either one
    would silently disable this policy.

    That applies to the MCP tool too: mcp__clients__lookup_client is NOT listed
    in allowed_tools. It is permitted by can_use_tool instead, so its approval
    is logged and revocable like every other decision this job makes.
    """
    return ClaudeAgentOptions(
        cwd=str(workspace),
        setting_sources=[],
        # Passed to the SDK directly, so neither isolation flag excludes it.
        mcp_servers=MCP_SERVERS,
        # setting_sources=[] blocks only what is configured on disk. Account
        # connectors on claude.ai arrive another way and need this as well --
        # without it a run had third-party write tools in scope.
        strict_mcp_config=True,
        allowed_tools=[],
        permission_mode="default",
        can_use_tool=can_use_tool,
        max_turns=20,
        resume=resume,
    )


async def run_job(job: Job) -> None:
    """Drive one agent run to completion, recording everything on the job."""
    log = StreamLog(job.log_path)
    job.status = "running"
    job.started_at = time.monotonic()
    job.workspace_before = snapshot_workspace(job.workspace)

    log.write(
        "run_start",
        {
            "job_id": job.job_id,
            "task": job.task,
            "cwd": str(job.workspace),
            "resume": job.resume,
            "setting_sources": [],
            "allowed_tools": [],
            "permission_mode": "default",
            "mcp_servers": sorted(MCP_SERVERS),
            "mcp_allowed_tools": list(MCP_ALLOWED_TOOLS),
            "policy": f"can_use_tool confines Read/Write/Glob/Bash to {job.workspace}; "
            f"MCP tools are allowlisted by name",
            "max_turns": 20,
        },
    )

    options = make_options(job.workspace, job.resume, make_permission_callback(job, log))

    try:
        # ClaudeSDKClient rather than query(): it exposes interrupt(), so DELETE
        # can stop the run cleanly and still collect a ResultMessage with cost.
        async with ClaudeSDKClient(options=options) as client:
            job.client = client
            await client.query(job.task)

            async for message in client.receive_response():
                log.write("message", message)
                job.events.append(encode(message))

                if isinstance(message, SystemMessage):
                    if message.subtype == "init" and isinstance(message.data, dict):
                        job.session_id = message.data.get("session_id")
                        if job.session_id:
                            SESSION_WORKSPACES.setdefault(job.session_id, job.workspace)

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            job.tool_calls.append(block.name)
                        elif isinstance(block, TextBlock):
                            job.final_text = block.text

                elif isinstance(message, ResultMessage):
                    job.session_id = message.session_id or job.session_id
                    job.num_turns = message.num_turns
                    job.total_cost_usd = message.total_cost_usd
                    job.terminal_reason = message.terminal_reason
                    if isinstance(message.result, str) and message.result:
                        job.final_text = message.result
                    usage = message.usage or {}
                    if isinstance(usage, dict):
                        job.cache_read_input_tokens = usage.get("cache_read_input_tokens")
                        job.cache_creation_input_tokens = usage.get(
                            "cache_creation_input_tokens"
                        )
                    if job.session_id:
                        SESSION_WORKSPACES.setdefault(job.session_id, job.workspace)

        if job.status == "running":
            job.status = "done"

    except asyncio.CancelledError:
        job.status = "cancelled"
        job.error = "cancelled via task cancellation"
        log.write("cancelled", {"job_id": job.job_id, "via": "task"})
        raise
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        log.write("error", {"type": type(exc).__name__, "message": str(exc)})
    finally:
        job.finished_at = time.monotonic()
        job.client = None

        # Was anyone still watching when this finished? A queue keeps running
        # regardless -- this records whether that actually happened unobserved.
        if job.last_polled_at is None:
            job.client_disconnected = True
        else:
            job.client_disconnected = (
                job.finished_at - job.last_polled_at
            ) > POLL_STALE_SECONDS

        log.write(
            "run_end",
            {
                "job_id": job.job_id,
                "status": job.status,
                "session_id": job.session_id,
                "num_turns": job.num_turns,
                "total_cost_usd": job.total_cost_usd,
                "cache_read_input_tokens": job.cache_read_input_tokens,
                "cache_creation_input_tokens": job.cache_creation_input_tokens,
                "terminal_reason": job.terminal_reason,
                "elapsed_seconds": job.elapsed_seconds(),
                "client_disconnected": job.client_disconnected,
                "seconds_since_last_poll": (
                    round(job.finished_at - job.last_polled_at, 3)
                    if job.last_polled_at is not None
                    else None
                ),
                "tool_calls": job.tool_calls,
            },
        )
        log.close()


# ---------------------------------------------------------------------------

app = FastAPI(title="Claude Agent SDK job queue")

# At import time, so a broken MCP path is reported in the uvicorn startup log
# rather than discovered as a mysteriously unhelpful answer three jobs later.
preflight()


class RunRequest(BaseModel):
    task: str
    session_id: str | None = None


@app.post("/run", status_code=202)
async def post_run(req: RunRequest) -> dict[str, Any]:
    """Accept work and return immediately. 202 = accepted, not completed."""
    job_id = uuid.uuid4().hex[:12]

    if req.session_id:
        # Resume: reuse the session's original directory. Sessions are looked up
        # under an encoding of cwd, so a different directory silently starts a
        # fresh conversation instead of erroring.
        workspace = SESSION_WORKSPACES.get(req.session_id)
        if workspace is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"unknown session_id {req.session_id!r}; no workspace on record. "
                    "Sessions are tracked in memory and lost on server restart."
                ),
            )
    else:
        workspace = WORKSPACES / job_id
        workspace.mkdir(parents=True, exist_ok=True)

    LOG_DIR.mkdir(exist_ok=True)
    job = Job(
        job_id=job_id,
        task=req.task,
        workspace=workspace,
        log_path=LOG_DIR / f"job-{job_id}.jsonl",
        resume=req.session_id,
    )
    JOBS[job_id] = job
    job.task_handle = asyncio.create_task(run_job(job))

    return {
        "job_id": job_id,
        "status": job.status,
        "workspace": str(workspace),
        "resume": req.session_id,
    }


def _get(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id!r}")
    job.last_polled_at = time.monotonic()
    return job


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = _get(job_id)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "session_id": job.session_id,
        "num_turns": job.num_turns,
        "total_cost_usd": job.total_cost_usd,
        "cache_read_input_tokens": job.cache_read_input_tokens,
        "cache_creation_input_tokens": job.cache_creation_input_tokens,
        "terminal_reason": job.terminal_reason,
        "elapsed_seconds": job.elapsed_seconds(),
        "client_disconnected": job.client_disconnected,
        "tool_calls": job.tool_calls,
        "permission_log": job.permission_log,
        "permission_denials": sum(
            1 for d in job.permission_log if d["decision"] == "deny"
        ),
        "workspace": str(job.workspace),
        "log": str(job.log_path),
        "final_text": job.final_text,
        "error": job.error,
    }


@app.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    return {
        "jobs": [
            {
                "job_id": j.job_id,
                "status": j.status,
                "session_id": j.session_id,
                "elapsed_seconds": j.elapsed_seconds(),
            }
            for j in JOBS.values()
        ]
    }


@app.get("/jobs/{job_id}/stream")
async def get_stream(job_id: str, request: Request) -> dict[str, Any]:
    """Accumulated stream events, so a run can be inspected without the log file."""
    job = _get(job_id)
    if await request.is_disconnected():
        job.client_disconnected = True
    return {
        "job_id": job.job_id,
        "status": job.status,
        "event_count": len(job.events),
        "events": job.events,
    }


@app.get("/jobs/{job_id}/files")
async def list_job_files(job_id: str) -> dict[str, Any]:
    """What this job wrote. Not the whole workspace -- a resumed thread shares
    one directory, and re-listing every earlier turn's output on each poll would
    make the third turn look like it produced everything the first two did."""
    job = _get(job_id)
    files = job_files(job)
    return {
        "job_id": job.job_id,
        "workspace": str(job.workspace),
        "count": len(files),
        "files": files,
    }


@app.get("/jobs/{job_id}/files/{filename:path}")
async def get_job_file(job_id: str, filename: str) -> FileResponse:
    """One file's bytes, typed. No Content-Disposition, so an image renders
    inline in a browser; a client wanting a download names it itself."""
    job = _get(job_id)
    target = resolve_workspace_file(job, filename)
    return FileResponse(target, media_type=guess_mime(target.name))


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, Any]:
    """Cancel a running job.

    Prefers ClaudeSDKClient.interrupt(), which stops the agent cleanly and still
    produces a ResultMessage (so cost and terminal_reason survive). Falls back to
    cancelling the task if interrupt is unavailable or does not land.
    """
    job = _get(job_id)
    if job.status in ("done", "error", "cancelled"):
        return {"job_id": job_id, "status": job.status, "detail": "already finished"}

    method = None
    client = job.client
    if client is not None:
        try:
            await client.interrupt()
            method = "interrupt"
        except Exception as exc:
            method = f"interrupt failed: {type(exc).__name__}: {exc}"

    if method != "interrupt" and job.task_handle is not None:
        job.task_handle.cancel()
        with suppress(asyncio.CancelledError):
            await job.task_handle
        method = "task.cancel"

    job.status = "cancelled"
    if job.finished_at is None:
        job.finished_at = time.monotonic()

    return {
        "job_id": job_id,
        "status": job.status,
        "cancelled_via": method,
        "terminal_reason": job.terminal_reason,
        "total_cost_usd": job.total_cost_usd,
        "elapsed_seconds": job.elapsed_seconds(),
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "jobs": len(JOBS)}
