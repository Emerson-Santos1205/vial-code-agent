# VIAL Code Agent

Experimental code-generation application built on top of the VIAL core.

This repository is intentionally separate from the VIAL architectural
reference. It owns product behavior such as file selection, model routing and
the developer experience. Normative concepts and validated generic
abstractions remain in the VIAL repository.

## Status

Experimental foundation. The CLI is an opencode-style terminal interface that
selects source files, routes tasks across a pool of models (or leaves routing
to an automatic orchestrator), executes tests and caches results. Model
execution is never implicit. When the VIAL core is present, the runtime
composes every official prototype surface: selective context, cognitive reuse,
Deterministic-First routing, capability/authority gating, intent-logged patch
application, cost accounting and persistent organizational state (see
`docs/vial-integration.md`).

## Install

From this repository:

```text
python -m pip install -e .
```

The `vial` command is installed by the editable package install.

Clone with the official VIAL core:

```text
git clone <vial-code-agent-repository> --recurse-submodules
git submodule update --init --recursive
```

The pinned VIAL implementation is available at `vendor/vial-core`. Code
generation uses its official selective `Context` lifecycle and patch execution
is authorized and audited through VIAL `Decision` and `Tool` contracts.

## Run

Launch the fullscreen opencode-style interface (Textual + Rich):

```text
vial
vial /path/to/project
vial --prompt "fix the parser"
vial --model openai/gpt-5.6-luna --agent plan
vial -c                    # continue the last session
vial -s <session_id>       # resume a specific session
vial --auto                # auto-approve workspace permissions (dangerous!)
```

The interface has a message viewport, a command input with autocomplete, a
right-hand panel (session / agent / model / status / pool) and a footer of
keybindings:

- `Tab` switches the agent between `build` (full access) and `plan` (read-only).
- `Ctrl+P` opens the model picker; `Enter` selects.
- `Ctrl+C` quits.

### Models and the orchestrator

Each prompt is routed by an orchestrator. With `--model auto` (the default)
`RoutingGraph` analyzes the prompt text, mirrors the VIAL Deterministic-First
chain (RFC-010), and — for implementation tasks — dispatches the prompt in
parallel to every model in the routing pool, taking the first valid response in
pool order. Pin a single model with `--model provider/model`; the identifier
always uses `provider/model`, exactly as reported by `vial --models`.

OpenAI-compatible servers are registered from the terminal with
`/server add <name> <base_url> [api_key_env]` and persisted in `.vial.json`
under the `servers` key; the API key is read from the named environment
variable at call time, never stored in the file.

### Slash commands

```text
/models [provider]        list models (registered + opencode)
/model provider/model     switch model (auto = route by prompt)
/model add server/model   add a model to a server
/model remove server/model
/agent build|plan         switch agent (or press Tab)
/auto                     toggle auto-approval
/providers                list opencode providers
/servers                  list configured HTTP servers
/server add <name> <base_url> [api_key_env]
/server remove <name>
/server models <name>
/pool                     show the parallel routing pool
/pool add <model_ref>
/pool remove <model_ref>
/status                   show session, model, agent, route and pool
/trace <decision_id>      show the audit trail of a Decision
/approve <decision_id>    approve a pending Decision
/sessions                 list past sessions
/resume <session_id>
/clear                    start a new session
/exit
```

### Non-interactive actions

```text
vial --fix "implement persistence"                    # generate + apply + verify
vial --fix "corrija o parser" --model openai/gpt-5.6-luna
vial --review path/to/change.patch                    # validate a patch
vial --status                                         # organizational snapshot
vial --status --trace DEC-0001                        # audit trail of a decision
vial --models --provider openai                       # list models
vial --providers                                      # list providers
vial --run "python -m unittest discover -s tests"     # governed command execution
```

`--fix` selects the workspace files, routes the task, generates a patch,
applies it through the governed `TOOL-PATCH-APPLY` decision chain and runs the
verification command if supplied. Failed verification rolls the patch back
automatically unless `--keep-on-failure` is used:

```text
vial --fix "fix the parser" --include "*.py" `
  --test-command python -m unittest discover -s tests
```

`--run` executes only allowlisted commands through the governed
`TOOL-RUN-BUILD`; unrestricted execution is never the default.

Copy `.vial.json.example` to `.vial.json` to configure a default model and
runtime options. The optional `.vial.json` file can define `model`, `cache_dir`,
`test_timeout`, `max_context_chars`, `opencode_executable`, `opencode_agent`,
`auto_approve`, `org_id`, `authority`, `actor`, `persist_state` and
`price_table_json`; matching `VIAL_*` environment variables override those
values.

## Test

```text
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Boundary with VIAL

The application consumes VIAL concepts through a small adapter boundary. It
does not copy VIAL specifications or make product behavior normative for VIAL.
Until the core is packaged, the adapter accepts a local VIAL path through
`--vial-root` and exposes only the context metadata needed by the application.

## VIAL integration

With `vendor/vial-core` initialized, the runtime (`VialRuntime`) composes every
official prototype module (`state`, `context`, `tokenizer`, `decision`,
`authorization`, `tool`, `resource`, `identity`, `persistence`, `coordinator`,
`reuse`, `cost`, `executor`, `errors`) into the code workflow:

- **Selective context** with token budgeting (RFC-007, SDK-004).
- **Cognitive reuse** with stale invalidation (RFC-008): identical tasks with
  unchanged files reuse the validated result without invoking a model.
- **Deterministic First** (RFC-010): mechanical tasks such as `trim trailing
  whitespace` or `add encoding header` are resolved without any model call and
  recorded as audited deterministic executions.
- **Capability ≠ Authority** (SDK-005, TOOLS-007): patch application requires a
  Decision that is proposed, approved and authorized; the `AuthorizationGate`
  rejects actors, capabilities, scopes, policies or organizations that do not
  match.
- **Atomic, idempotent, recoverable patches** (RFC-009): intent is logged
  before mutation, commits are atomic, replays are resolved from the log, and
  test-failure rollback is recorded as an auditable compensation transition.
- **Economic cost accounting** (RFC-004, RFC-010): inference, retrieval,
  construction, validation and latency are accumulated against a price table.
- **Persistent organizational cognition** (RFC-003): state, decisions, reuse,
  intents and audit records are atomically persisted to `.vial-state/` and
  restored on the next run.
- **Identity** (SDK-001 §30): the runtime authenticates the actor and authority
  before authorization.

A mechanical task is routed without a model:

```text
vial --fix "trim trailing whitespace"
```

Organizational telemetry:

```text
vial --status                     # snapshot organizacional (tools, memória, approvals)
vial --status --trace DEC-0001    # porquê de uma decisão (audit trail)
```

See `docs/vial-integration.md` for the module-by-module mapping and evidence.

## Benchmark

The offline benchmark validates the complete local loop without paid model
calls: create an isolated workspace, apply a patch, execute tests and report
quality.

```text
python benchmark/run_benchmark.py
```

The model-backed benchmark is intentionally separate and requires an
authenticated `opencode` installation.
