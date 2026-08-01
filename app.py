#!/usr/bin/env python3
"""Streamlit surface over the job queue in server.py.

HTTP only. This process never imports claude_agent_sdk and never runs an agent:
it POSTs work and polls for state. That separation is the point -- the job
outlives the browser tab, so closing the tab does not stop the run, and two tabs
polling the same job_id see the same thing.

Polling works by rerunning the whole script, not by looping inside it. A `while`
loop held open inside an st.empty() would block the script runner and freeze
every widget on the page -- including the raw-stream toggle, which is the one
control that most needs to work while a job is still going.

Run (server must already be up):
    ./.venv/bin/streamlit run app.py
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
import streamlit as st

API = os.environ.get("AGENT_API", "http://127.0.0.1:8000")
POLL_SECONDS = 1.0
TERMINAL = {"done", "error", "cancelled"}

# Outputs are pulled into the browser to be shown, so there is a ceiling. Past
# it the file is named and linked rather than fetched.
MAX_INLINE_BYTES = 10 * 1024 * 1024

# st.image renders raster bytes. SVG bytes it does not, so those fall through to
# the download path with everything else.
INLINE_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp")

# Keys checked in order for the one-line summary of a tool call. `description`
# first: it is the short human sentence the model wrote for the call, which is
# what a single dim line wants. The full input goes in the expander.
SUMMARY_KEYS = ("description", "command", "file_path", "path", "pattern", "prompt")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class ApiError(RuntimeError):
    pass


def request(method: str, path: str, timeout: float = 15.0, **kwargs) -> httpx.Response:
    """One request to the server. Every failure surfaces as a readable ApiError."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, f"{API}{path}", **kwargs)
    except httpx.RequestError as exc:
        raise ApiError(
            f"Cannot reach the server at {API} ({type(exc).__name__}). "
            f"Start it with: ./.venv/bin/uvicorn server:app --port 8000"
        ) from exc

    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ApiError(f"{method} {path} -> {response.status_code}: {detail}")

    return response


def call(method: str, path: str, **kwargs) -> dict[str, Any]:
    return request(method, path, **kwargs).json()


def file_bytes(job_id: str, name: str) -> bytes:
    """One output file, over the API.

    Deliberately an HTTP fetch and not a filesystem read: this process is a
    client of the server, and the moment it reaches into the agent's directory
    itself the separation the whole thing is built on stops being true. Cached
    per (job, file) -- contents are fixed by the time a finished job is listed.
    """
    key = f"{job_id}/{name}"
    blob = st.session_state.blobs.get(key)
    if blob is None:
        path = f"/jobs/{job_id}/files/{quote(name)}"
        blob = request("GET", path, timeout=60.0).content
        st.session_state.blobs[key] = blob
    return blob


def fetch(job_id: str) -> dict[str, Any]:
    """Job state plus its stream, cached once the job reaches a terminal status.

    Without the cache every poll would refetch the full event list of every job
    in the thread, which grows without bound as the thread does.
    """
    cached = st.session_state.cache.get(job_id)
    # "files" in cached: a session that was open before the file endpoints
    # existed holds cache entries of the old shape. They survive a rerun (state
    # does), so treat a missing key as stale rather than KeyError-ing on it.
    if cached is not None and cached["job"]["status"] in TERMINAL and "files" in cached:
        return cached

    job = call("GET", f"/jobs/{job_id}")
    data = {
        "job": job,
        "stream": call("GET", f"/jobs/{job_id}/stream"),
        # Only once the job is finished. Mid-run the workspace is a moving
        # target, and a half-written PNG renders as a broken image.
        "files": call("GET", f"/jobs/{job_id}/files")["files"]
        if job["status"] in TERMINAL
        else [],
    }
    st.session_state.cache[job_id] = data
    return data


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def blocks(content: Any) -> list[dict[str, Any]]:
    """Content blocks of a message; [] for the plain-string form."""
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def one_line(text: str, limit: int = 110) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def tool_summary(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return one_line(tool_input)
    for key in SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return one_line(value)
    return one_line(json.dumps(tool_input, default=str))


def canonical(tool_input: Any) -> str:
    try:
        return json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(tool_input)


def decision_index(permission_log: list[dict[str, Any]]) -> dict[tuple, deque]:
    """Index permission entries by (tool, input) so each can be matched to its call.

    The permission log is a flat list on the job; the tool calls live in the
    stream. Matching on the exact input pairs them up, and a deque per key keeps
    repeated identical calls in order. Allows are indexed too, not just denials
    -- popping them keeps the queues aligned when a call repeats.
    """
    index: dict[tuple, deque] = {}
    for position, entry in enumerate(permission_log):
        key = (entry.get("tool"), canonical(entry.get("input")))
        index.setdefault(key, deque()).append((position, entry))
    return index


def take_decision(index: dict[tuple, deque], name: str, tool_input: Any):
    queue = index.get((name, canonical(tool_input)))
    if queue:
        return queue.popleft()[1]
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_denial(entry: dict[str, Any]) -> None:
    """Denials are errors on the page, never folded away.

    A blocked action changes what the agent did and is the thing a user most
    needs to see; burying it in the same collapsed expander as ordinary output
    would make a confined run look like an unconfined one.
    """
    st.error(f"**{entry.get('tool')} denied** — {entry.get('reason', 'no reason given')}")


def render_result(block: dict[str, Any] | None, tool_input: Any, label: str) -> None:
    """Tool input and result, collapsed. Expanded by default when it errored."""
    is_error = bool(block and block.get("is_error"))
    title = f"{'⚠ ' if is_error else ''}{label}"

    with st.expander(title, expanded=False):
        st.caption("input")
        st.json(tool_input, expanded=False)
        st.caption("result")
        if block is None:
            st.write("_no result yet_")
            return
        content = block.get("content")
        if isinstance(content, str):
            st.code(content, language=None)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    st.code(item["text"], language=None)
                else:
                    st.json(item, expanded=False)
        else:
            st.json(content, expanded=False)


def render_filtered(job: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Assistant text in full, tool calls as one dim line, results collapsed.

    ThinkingBlocks and SystemMessages are dropped here on purpose -- they are in
    the raw view, which is what the raw view is for.
    """
    results: dict[str, dict] = {}
    for event in events:
        if event.get("_type") == "UserMessage":
            for block in blocks(event.get("content")):
                if block.get("_type") == "ToolResultBlock":
                    results[block.get("tool_use_id")] = block

    index = decision_index(job.get("permission_log") or [])
    rendered_any = False

    for event in events:
        if event.get("_type") != "AssistantMessage":
            continue
        for block in blocks(event.get("content")):
            kind = block.get("_type")

            if kind == "TextBlock":
                text = (block.get("text") or "").strip()
                if text:
                    st.markdown(text)
                    rendered_any = True

            elif kind == "ToolUseBlock":
                rendered_any = True
                name = block.get("name") or "?"
                tool_input = block.get("input")
                st.caption(f"⚙ **{name}** · {tool_summary(tool_input)}")

                decision = take_decision(index, name, tool_input)
                if decision and decision.get("decision") == "deny":
                    render_denial(decision)

                render_result(results.get(block.get("id")), tool_input, f"result · {name}")

    # Anything in the permission log that never matched a visible tool call.
    # Should be empty; if it is not, a denial would otherwise vanish silently.
    leftover = sorted(
        (item for queue in index.values() for item in queue if item[1].get("decision") == "deny"),
        key=lambda item: item[0],
    )
    for _, entry in leftover:
        render_denial(entry)

    if not rendered_any and job["status"] not in TERMINAL:
        st.caption("waiting for the first message…")

    if job.get("error"):
        st.error(f"**job {job['status']}** — {job['error']}")


def render_raw(job_id: str, stream: dict[str, Any]) -> None:
    events = stream.get("events") or []
    st.caption(f"GET /jobs/{job_id}/stream — {len(events)} events")
    for i, event in enumerate(events):
        st.caption(f"[{i}] {event.get('_type', '?')}")
        st.json(event)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def render_files(job_id: str, files: list[dict[str, Any]]) -> None:
    """Images inline, everything else as a download."""
    if not files:
        return

    st.caption(f"**{len(files)} file{'' if len(files) == 1 else 's'}** from this job")
    for entry in files:
        name = entry["name"]
        mime = entry.get("mime") or "application/octet-stream"
        size = entry.get("size") or 0

        if size > MAX_INLINE_BYTES:
            st.caption(f"📄 `{name}` · {human_size(size)} · too large to load inline")
            st.markdown(f"[{API}/jobs/{job_id}/files/{quote(name)}]({API}/jobs/{job_id}/files/{quote(name)})")
            continue

        try:
            blob = file_bytes(job_id, name)
        except ApiError as exc:
            st.warning(f"`{name}` — {exc}")
            continue

        if mime in INLINE_IMAGE_TYPES:
            st.image(blob, caption=f"{name} · {human_size(size)}", width="stretch")
        else:
            st.download_button(
                f"⬇ {name} · {human_size(size)}",
                data=blob,
                file_name=PurePosixPath(name).name,
                mime=mime,
                key=f"dl-{job_id}-{name}",
            )


def render_footer(job: dict[str, Any]) -> None:
    cost = job.get("total_cost_usd")
    turns = job.get("num_turns")
    denials = job.get("permission_denials") or 0

    parts = [
        f"**{job['status']}**",
        f"{turns if turns is not None else '—'} turns",
        f"{job.get('elapsed_seconds', 0):.1f}s",
        f"${cost:.4f}" if isinstance(cost, (int, float)) else "$—",
    ]
    if denials:
        parts.append(f"{denials} denied")
    st.caption(" · ".join(parts) + f" · `{job['job_id']}`")


def render_job(job_id: str, live: bool) -> bool:
    """Render one turn of the thread. Returns True if the job is still going."""
    task = st.session_state.tasks.get(job_id)
    if task:
        with st.chat_message("user"):
            st.markdown(task)

    try:
        data = fetch(job_id)
    except ApiError as exc:
        st.error(str(exc))
        return False

    job, stream, files = data["job"], data["stream"], data["files"]

    # The active job is drawn into a placeholder so each poll replaces the
    # region in place instead of the page visibly rebuilding underneath it.
    container = st.empty().container() if live else st.container()
    with container:
        if st.session_state.raw:
            render_raw(job_id, stream)
            # Outputs are job artifacts, not stream events, so raw mode shows the
            # listing as the API returns it rather than rendering the files.
            if files:
                st.caption(f"GET /jobs/{job_id}/files")
                st.json(files)
        else:
            with st.chat_message("assistant"):
                render_filtered(job, stream.get("events") or [])
                render_files(job_id, files)
        render_footer(job)

    if job.get("session_id"):
        st.session_state.session_id = job["session_id"]

    return job["status"] not in TERMINAL


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Agent jobs", page_icon="⚙", layout="centered")

for key, default in (
    ("job_ids", []),
    ("tasks", {}),
    ("cache", {}),
    ("blobs", {}),
    ("session_id", None),
    ("raw", False),
):
    st.session_state.setdefault(key, default)

st.title("Agent jobs")

active_id = st.session_state.job_ids[-1] if st.session_state.job_ids else None
active_status = None
if active_id:
    cached = st.session_state.cache.get(active_id)
    active_status = cached["job"]["status"] if cached else "running"
running = active_status is not None and active_status not in TERMINAL

# Controls are built before anything expensive so they stay responsive during a
# poll cycle. The raw toggle is keyed, so flipping it mid-run just reruns the
# script against the same job_id and redraws the same events differently.
left, middle, right = st.columns([2, 1, 1])
with left:
    st.toggle("Show raw stream", key="raw")
with middle:
    if st.button("New thread", use_container_width=True, disabled=not st.session_state.job_ids):
        st.session_state.job_ids = []
        st.session_state.tasks = {}
        st.session_state.cache = {}
        st.session_state.blobs = {}
        st.session_state.session_id = None
        st.rerun()
with right:
    if st.button("Cancel", use_container_width=True, disabled=not running, type="primary"):
        try:
            call("DELETE", f"/jobs/{active_id}")
        except ApiError as exc:
            st.error(str(exc))
        else:
            st.session_state.cache.pop(active_id, None)
            st.rerun()

st.caption(
    f"{API} · session `{st.session_state.session_id or 'new'}`"
    + (" · resuming this thread" if st.session_state.session_id else "")
)

still_running = False
for job_id in st.session_state.job_ids:
    st.divider()
    if render_job(job_id, live=job_id == active_id):
        still_running = True

prompt = st.chat_input(
    "Give the agent a task…" if not still_running else "Running…",
    disabled=still_running,
)
if prompt:
    try:
        started = call(
            "POST",
            "/run",
            json={"task": prompt, "session_id": st.session_state.session_id},
        )
    except ApiError as exc:
        # A restarted server forgets its sessions, so a stale session_id 404s.
        # Drop it rather than stranding the thread on an id that can never work.
        if "unknown session_id" in str(exc):
            st.session_state.session_id = None
            st.warning("That session is gone (server restarted). Submit again to start a new thread.")
        else:
            st.error(str(exc))
    else:
        job_id = started["job_id"]
        st.session_state.job_ids.append(job_id)
        st.session_state.tasks[job_id] = prompt
        st.rerun()

if still_running:
    time.sleep(POLL_SECONDS)
    st.rerun()
