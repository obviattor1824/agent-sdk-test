# agent-sdk-test

A test harness for the Claude Agent SDK, in three layers. Each one wraps the one
below it and adds exactly one thing.

Pairs with [mcp-clients](https://github.com/obviattor1824/mcp-clients), the MCP server this harness spawns.
The code expects the two checkouts to be siblings:

```bash
git clone https://github.com/obviattor1824/agent-sdk-test.git
git clone https://github.com/obviattor1824/mcp-clients.git
```

## Setup

Python 3.10+. Verified on 3.13, `claude-agent-sdk` 0.2.128, Streamlit 1.60.

```bash
cd agent-sdk-test
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Nothing here talks to Anthropic. The SDK spawns the Claude Code binary as a
subprocess and speaks JSON to it over stdio; that binary makes the calls and
holds the credentials. The wheel ships its own copy at
`claude_agent_sdk/_bundled/claude` — it does on macOS arm64; where it does not,
`_find_cli()` falls back to `PATH`. When a bundled copy exists it wins, so
`claude --version` in your shell tells you nothing about what a run just used. **The version under test is pinned by
pip.** Read it off the init message — `claude_code_version` — every time you
compare anything.

Auth is whatever that binary already has, from the same credential store the CLI
uses; a subscription is enough. `setting_sources=[]` does not touch it — that
governs settings, skills, plugins and disk-configured MCP servers, not
credentials, so an isolated run is still an authenticated one. These runs report
`apiKeySource: none`; setting `ANTHROPIC_API_KEY` changes which meter you are
billed against.

The CLI comparison runs and `claude --mcp-config` do need a `claude` on `PATH`:

```bash
npm install -g @anthropic-ai/claude-code
```

That one is yours to keep current, and it is not the one the SDK runs.

`mcp-clients` has its own setup — see its README. Without it the harness still
runs: `preflight()` warns at startup, the init message reports the server as
`status: "failed"`, `check_init` says so loudly, and `lookup_client` is absent.

## Try it

Five runs. The first two and the last need nothing but `run.py`.

**1. Does any of this work.**

```bash
./.venv/bin/python run.py "Create a file called hello.txt containing the word hi, in the current directory."
```

Every message from the loop prints with its type, and `check_init` reports what
the run actually booted with. Then check where `hello.txt` actually landed. Under
`bypassPermissions` a task that names no location gets one chosen for it, and
`cwd` does not prevent that — which is why this prompt says *in the current
directory* and why it is worth confirming that it worked.

**2. Did the MCP server connect.**

```bash
./.venv/bin/python run.py "What payment terms does Castilla Foods have?"
```

The directory says 60 days. That fact exists nowhere else, so 60 means the tool
was called and anything else means it was not — and the init check above will
already have said why.

**3. Watch a denial.** Server in one terminal:

```bash
./.venv/bin/uvicorn server:app --port 8000
```

Then:

```bash
curl -sX POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"task":"Write a file called marker.txt containing BETA to /tmp."}'

curl -s localhost:8000/jobs/JOB_ID
```

`permission_denials` is the count. `permission_log` holds each decision, the path
that triggered it, and the message the model received — which names both the
attempted path and the directory that is allowed. That is what lets a run recover
instead of retrying variations.

**4. The task worth watching, in the UI.** With the server still up:

```bash
./.venv/bin/streamlit run app.py
```

```
In this directory, create a small CSV of 20 fictional invoices with columns:
invoice_number, client_name, issue_date, amount_eur, status (paid/overdue/draft).
Then write a Python script that reads it and outputs a summary markdown file
showing total outstanding, count by status, and the three oldest overdue
invoices. Run the script and show me the output.
```

Then, in the same thread: *Can we release an order to Castilla Foods?* The
account is on hold. Whether that changes the answer rather than merely appearing
in it is the point — the tool supplies the fact, the model supplies the
judgement, and neither works alone.

**5. Does resume actually resume.**

```bash
./.venv/bin/python resume_test.py
```

Two runs against one session, the second asking something answerable only from
the conversation. It prints its own verdict: zero tool calls in run 2 means it
answered from memory rather than going back to the filesystem.

| File | What it is | Permissions |
| --- | --- | --- |
| `run.py` | CLI. Runs one task, dumps the raw message stream, logs JSONL. | `bypassPermissions` |
| `resume_test.py` | Two runs against one session, to check resume actually resumes. | `bypassPermissions` |
| `server.py` | FastAPI job queue. Jobs run in the background and outlive the request. | `can_use_tool` |
| `app.py` | Streamlit UI over the server. HTTP only — it never imports the SDK. | — |
| `mcp_config.py` | Which MCP servers to spawn, and which of their tools are allowed. | — |

Each entry point gets its own subdirectory under `workspaces/`, so the CLI, the
resume test and each HTTP job never share a working directory. Streams are
logged to `logs/`, one JSON object per message.

```
workspaces/
  cli/                     run.py
  resume-test/             resume_test.py — emptied at the start of each run
  {job_id}/                one per job from server.py; reused when a session resumes
logs/
  run-{timestamp}.jsonl    run.py
  resume-sdk-1.jsonl       resume_test.py, one file per run
  resume-sdk-2.jsonl
  job-{job_id}.jsonl       server.py
```

Both directories are gitignored, so a fresh clone has neither until you run
something.

**`run.py` and `resume_test.py` run under `bypassPermissions`; only `server.py`
confines anything.** That contrast is what this repo is for, so it matters which
one you are running. Under `bypassPermissions`, `cwd` is a suggestion: asked to
write `hello.txt` with `cwd` set to `workspaces/cli`, a run created
`../obviattor-agent-sdk-test/` and wrote there instead. `server.py`'s
`can_use_tool` callback is what stops that.

**Even that is not a sandbox.** The containment in `server.py` is
defence-in-depth for experimenting on a machine you control. It inspects command
text, so it stops an agent that wanders; it cannot stop one that constructs a
path at runtime. Not adversarially tested, not a security boundary.

## MCP servers

`run.py` and `server.py` both register the `clients` MCP server, which lives in
the sibling repo [`../mcp-clients`](https://github.com/obviattor1824/mcp-clients) and exposes `lookup_client`. It is defined once
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
