# agent-sdk-test

A progressive exploration of the Claude Agent SDK and the decisions a harness around it needs. Three layers, each adding one thing:

- `run.py`: the message stream, nothing in front of it, baseline behaviour
- `server.py`: an API boundary, jobs that outlive the request, every tool call refusable
- `app.py`: a surface that only speaks HTTP

Intention is that models and loops are rented, whilst the domain tools are a separate repo belonging to no harness, ie. portable, over in [mcp-clients](https://github.com/obviattor1824/mcp-clients).

## The decisions

Forks this hit, in roughly the order they arrive, inside `ClaudeAgentOptions` and then what to build around it.

### Configuring the SDK

**What tools does the model get?** About thirty by default, Bash among them. `run.py` leaves the set alone to show baseline behaviour.

**Who approves each call?** Either approve everything at the mode step which `run.py` does, or write a `can_use_tool` callback that answers in the human's place, which `server.py` does. You can't have both as a bare tool name in `allowed_tools` auto-approves *before* the callback runs, so a permissive setting silently voids a stricter one. MCP tools are a separate case: the callback judges file paths and an MCP tool has none, so `MCP_ALLOWED_TOOLS` lists the ones this harness trusts, by name.

**What does a denial say?** The denial message goes back as the tool result, and is all the model knows about the failure. A bare "requires approval" burns turns, then invents an answer. Naming the attempted path and the allowed directory makes it retry correctly.

**Where can it write to?** `cwd` sets a working directory and constrains nothing. Containment took a callback that resolves symlinks and `..` before deciding.

**What else gets loaded?** More than you ask for. `setting_sources=[]` blocks disk config only; connectors on your claude.ai account need `strict_mcp_config=True` as well. Without it, six of them turned up on some runs and not others, adding forty tools that `bypassPermissions` auto-approves. `check_init` prints what actually loaded, every run.

### Building around it

**Where does domain specific knowledge live?** Not in the model, nor the harness because a tool written inside a harness belongs to it and only it. Here it lives in a separate MCP server speaking stdio, so anything that speaks MCP can spawn it.

**Is a run a request or a job?** Twenty seconds to three minutes is past the timeout of most gateways. `POST /run` returns a job id and the work carries on without the connection.

**Does the UI own the agent?** If it does, closing the tab kills the run. `app.py` is an HTTP client of `server.py` and never imports the SDK.


| File             | What it is                                                             | Permissions         |
| ---------------- | ---------------------------------------------------------------------- | ------------------- |
| `run.py`         | CLI. Runs one task, dumps the raw message stream, logs JSONL.          | `bypassPermissions` |
| `resume_test.py` | Two runs against one session, to check resume actually resumes.        | `bypassPermissions` |
| `server.py`      | FastAPI job queue. Jobs run in the background and outlive the request. | `can_use_tool`      |
| `app.py`         | Streamlit UI over the server. HTTP only — it never imports the SDK.    | —                   |
| `mcp_config.py`  | Which MCP servers to spawn, and which of their tools are allowed.      | —                   |


**Only** `server.py` **confines anything** — `run.py` and `resume_test.py` run under `bypassPermissions`. So it matters which one you are running, and the least contained one is the easiest to start with.

**Even that is not a sandbox.** The containment in `server.py` is defence-in-depth for experimenting on a machine you control. It inspects command text, so it stops an agent that wanders; it cannot stop one that constructs a path at runtime. Not adversarially tested, not a security boundary.

Pairs with [mcp-clients](https://github.com/obviattor1824/mcp-clients), the MCP server this harness spawns. The code expects the two checkouts to be siblings:

```bash
git clone https://github.com/obviattor1824/agent-sdk-test.git
git clone https://github.com/obviattor1824/mcp-clients.git
```



## Setup

Python 3.10+ — check with `python3 --version`. On macOS the stock `python3` is 3.9, so name the interpreter explicitly (`python3.13`) or the venv will not install.

Last verified on 3.13 with `claude-agent-sdk` 0.2.144, `mcp` 2.1.1 and Streamlit 1.62. Nothing is pinned, so a fresh clone gets whatever is current; those are the versions this was known to work on, not a constraint.

```bash
cd agent-sdk-test
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

The SDK does not call the Anthropic API itself. It spawns the Claude Code binary and speaks JSON to it over stdio; that binary makes the calls and holds the credentials. The wheel ships its own copy, and where it does, that copy beats any `claude` on your `PATH`. **So the version under test is pinned by pip, not by your shell.** Read it off the init message — `claude_code_version` — every time you compare anything.

Auth is whatever that binary already has, from the same credential store the CLI uses; a subscription is enough. `setting_sources=[]` does not touch it — that governs settings, skills, plugins and disk-configured MCP servers, not credentials, so an isolated run is still an authenticated one. These runs report `apiKeySource: none`; setting `ANTHROPIC_API_KEY` changes which meter you are billed against.

The CLI comparison runs and `claude --mcp-config` do need a `claude` on `PATH`:

```bash
npm install -g @anthropic-ai/claude-code
```

That one is yours to keep current, and it is not the one the SDK runs.

## Walk through it

Five steps where each adds one thing to the one before.

**1. Baseline.** `run.py` on its own — no permission policy, no API, no surface. `mcp_config.py` registers the `clients` server for every entry point, so if you have already built the sibling repo the tool is here too; the baseline is what you get before that.

```bash
./.venv/bin/python run.py "Create a file called hello.txt containing the word hi, in the current directory."
```

Every message prints with its type, and `check_init` reports what the run actually booted with. The whole stream is also written to `logs/run-{timestamp}.jsonl`. Then check where `hello.txt` landed — under `bypassPermissions` a task naming no location gets one chosen for it, which is why this prompt says *in the current directory*.

**2. Add the domain tool.** First step that needs the sibling checkout built, not just cloned — it has its own venv, see [mcp-clients](https://github.com/obviattor1824/mcp-clients)'s README. `mcp_config.py` registers it for both `run.py` and `server.py`; set `MCP_CLIENTS_ROOT` if the checkout is somewhere else.

```bash
./.venv/bin/python run.py "What payment terms does Castilla Foods have?"
```

The directory says 60 days. That fact exists nowhere else, so 60 means the tool was called and anything else means it was not. Skip this step and the harness still runs: `preflight()` warns, init reports the server `failed`, and `check_init` says so loudly.

**3. Add the API boundary.** Terminal 1:

```bash
./.venv/bin/uvicorn server:app --port 8000
```

**Not** `--reload`**.** Agents write into `workspaces/`, underneath the directory the reloader watches, so a job that creates a file restarts the server *because of its own output*. `JOBS` is in memory, so that takes every running job with it and the next poll 404s. If you want reload while working on `server.py`:

```bash
./.venv/bin/uvicorn server:app --port 8000 \
  --reload --reload-exclude 'workspaces/*' --reload-exclude 'logs/*'
```

Terminal 2, or curl:

```bash
curl -sX POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"task":"Write a file called marker.txt containing BETA to /tmp."}'

curl -s localhost:8000/jobs/JOB_ID
```

`permission_denials` is the count. `permission_log` holds each decision, the path that triggered it, and the message the model received — which names both the attempted path and the directory that is allowed.

**4. Add the surface.** With the server still up:

```bash
./.venv/bin/streamlit run app.py
```

Then open [http://localhost:8501](http://localhost:8501). `AGENT_API=http://host:port` points it at a different server. Give it:

```
In this directory, create a small CSV of 20 fictional invoices with columns:
invoice_number, client_name, issue_date, amount_eur, status (paid/overdue/draft).
Then write a Python script that reads it and outputs a summary markdown file
showing total outstanding, count by status, and the three oldest overdue
invoices. Run the script and show me the output.
```

Then, in the same thread: Can we release an order to Castilla Foods? The account is on hold, so that should inform the answer not just appear in it.

The two processes are genuinely independent. A job runs in the server's event loop, so closing the browser tab does not cancel it; reopening the UI mid-run picks the stream back up. Only `DELETE /jobs/{job_id}` — the **Cancel** button — stops one.

**5. Check the resume.** Back to `run.py` alone:

```bash
./.venv/bin/python resume_test.py
```

Two runs against one session, the second asking something answerable only from the conversation. It prints its own verdict: zero tool calls in run 2 means it answered from memory rather than going back to the filesystem.

Each entry point gets its own subdirectory under `workspaces/`, so the CLI, the resume test and each HTTP job never share a working directory. Streams are logged to `logs/`, one JSON object per message.

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

Both directories are gitignored, so a fresh clone has neither until you run something.

## The UI

Rough and ready as a proof of concept. Sessions are tracked in memory, so restarting the server orphans the current thread.

- **Filtered view (default)** — assistant text in full, each tool call as one dim line, tool input and result collapsed in an expander.
- **Denials** are rendered inline as errors, never collapsed. The server confines each job to its own workspace with a `can_use_tool` callback, so denials are routine and are the most interesting thing on the page when they happen.
- **Show raw stream** swaps the filtered view for every event from
`GET /jobs/{job_id}/stream` as JSON. It can be flipped mid-run against the job in flight. Permission decisions are in that stream as `PermissionDecision` events, in sequence with the messages around them, so raw really is a superset of the filtered view rather than a different slice of it.
- **Outputs** appear when a job finishes: images inline, everything else as a download button. Fetched from `GET /jobs/{job_id}/files`, never by reading `workspaces/` — the UI is a client of the server, not a second reader of the
agent's directory. Only files that job touched are listed, so the third turn of a thread does not re-show what the first two wrote.
- **New thread** clears the stored `session_id`; until then every submission passes it to `POST /run`, which resumes the conversation and reuses its workspace.



## The API


| Endpoint                          |                                                              |
| --------------------------------- | ------------------------------------------------------------ |
| `POST /run`                       | `{"task": "...", "session_id": null}` → 202 with a `job_id`  |
| `GET /jobs/{job_id}`              | status, session, turns, cost, elapsed, `permission_log`      |
| `GET /jobs/{job_id}/stream`       | all accumulated stream events, permission decisions included |
| `GET /jobs/{job_id}/files`        | files this job created or modified: name, size, mime         |
| `GET /jobs/{job_id}/files/{name}` | those bytes, with the right content type                     |
| `GET /jobs`                       | every job this process knows about                           |
| `DELETE /jobs/{job_id}`           | cancel a running job                                         |
| `GET /health`                     | liveness, and how many jobs this process is holding          |


