# agent-sdk-test

A test harness for the Claude Agent SDK, in three layers. Each one wraps the one
below it and adds exactly one thing.

| File | What it is |
| --- | --- |
| `run.py` | CLI. Runs one task, dumps the raw message stream, logs JSONL. |
| `resume_test.py` | Two runs against one session, to check resume actually resumes. |
| `server.py` | FastAPI job queue. Jobs run in the background and outlive the request. |
| `app.py` | Streamlit UI over the server. HTTP only — it never imports the SDK. |
| `mcp_config.py` | Which MCP servers to spawn, and which of their tools are allowed. |

Working directories are `workspaces/{job_id}/`, one per job. Streams are logged
to `logs/job-{job_id}.jsonl`, one JSON object per message.

## MCP servers

`run.py` and `server.py` both register the `clients` MCP server, which lives in
the sibling repo `../mcp-clients` and exposes `lookup_client`. It is defined once
in `mcp_config.py` so the CLI and the job queue cannot drift apart. Point
`MCP_CLIENTS_ROOT` elsewhere if that checkout moves.

Two things about this are easy to get wrong:

**`setting_sources=[]` does not block it.** That switch controls what is loaded
from disk — `~/.claude`, `.claude/`. `mcp_servers` is handed to the SDK
directly and is unaffected, so the runs stay isolated from your machine's
configuration while still getting this one server. `check_init` in `run.py`
enforces exactly that distinction: the servers in `MCP_SERVERS` are expected,
any *other* server means isolation has failed. It also warns when a registered
server is missing or not `connected`, because a server that fails to spawn is
silent — the tool never appears and the model answers from guesswork.

**Registering a server is not the same as trusting its tools.** `server.py`
allows MCP tools by name, in `MCP_ALLOWED_TOOLS`. The workspace containment
tests can't apply to them (there is no path to resolve — the work happens in
another process), so identity is the only thing left to judge. Adding a tool to
a server does not make it callable here until it is listed. That list has to be
maintained by hand; that is the trade being made.

Note the MCP tool is deliberately *not* in `server.py`'s `allowed_tools`, which
stays empty. A name there would auto-approve the tool before `can_use_tool` ran,
so the call would never be logged or refusable.

## Running the server and the UI

The UI needs Streamlit; everything else is already in the venv. It uses `httpx`
for its HTTP calls, which FastAPI already pulls in:

```bash
./.venv/bin/pip install streamlit
```

Two processes, two terminals. The UI talks to the server over HTTP and nothing
else, so the server has to be up first.

Terminal 1 — the API:

```bash
./.venv/bin/uvicorn server:app --port 8000
```

**Not `--reload`.** Agents write their output into `workspaces/`, which is
underneath the directory the reloader watches, so a job that creates a file
restarts the server *because of its own output*. `JOBS` is in memory, so the
restart takes every running job and every known session with it, and the UI's
next poll gets a 404 on a job that was fine a second ago. If you want reload
while working on `server.py`:

```bash
./.venv/bin/uvicorn server:app --port 8000 \
  --reload --reload-exclude 'workspaces/*' --reload-exclude 'logs/*'
```

Terminal 2 — the UI:

```bash
./.venv/bin/streamlit run app.py
```

Then open http://localhost:8501. Point the UI at a different server with
`AGENT_API=http://host:port ./.venv/bin/streamlit run app.py`.

The two are genuinely independent. A job runs in the server's event loop, so
closing the browser tab does not cancel it — reopening the UI mid-run and
polling the same job picks the stream back up. Only `DELETE /jobs/{job_id}`
(the **Cancel** button) stops one.

## The UI

- **Filtered view (default)** — assistant text in full, each tool call as one
  dim line, tool input and result collapsed in an expander.
- **Denials** are rendered inline as errors, never collapsed. The server confines
  each job to its own workspace with a `can_use_tool` callback, so denials are
  routine and are the most interesting thing on the page when they happen.
- **Show raw stream** swaps the filtered view for every event from
  `GET /jobs/{job_id}/stream` as JSON. It can be flipped mid-run against the job
  in flight. Permission decisions are in that stream as `PermissionDecision`
  events, in sequence with the messages around them, so raw really is a superset
  of the filtered view rather than a different slice of it.
- **Outputs** appear when a job finishes: images inline, everything else as a
  download button. Fetched from `GET /jobs/{job_id}/files`, never by reading
  `workspaces/` — the UI is a client of the server, not a second reader of the
  agent's directory. Only files that job touched are listed, so the third turn
  of a thread does not re-show what the first two wrote.
- **New thread** clears the stored `session_id`; until then every submission
  passes it to `POST /run`, which resumes the conversation and reuses its
  workspace.

Sessions are tracked in memory, so restarting the server orphans the current
thread. The UI notices the 404 and drops the stale id.

## The API

| Endpoint | |
| --- | --- |
| `POST /run` | `{"task": "...", "session_id": null}` → 202 with a `job_id` |
| `GET /jobs/{job_id}` | status, session, turns, cost, elapsed, `permission_log` |
| `GET /jobs/{job_id}/stream` | all accumulated stream events, permission decisions included |
| `GET /jobs/{job_id}/files` | files this job created or modified: name, size, mime |
| `GET /jobs/{job_id}/files/{name}` | those bytes, with the right content type |
| `GET /jobs` | every job this process knows about |
| `DELETE /jobs/{job_id}` | cancel a running job |
| `GET /health` | |
