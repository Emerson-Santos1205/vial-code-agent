# VIAL Code Agent

Experimental code-generation application built on top of the VIAL core.

This repository is intentionally separate from the VIAL architectural
reference. It owns product behavior such as file selection, model routing and
the developer experience. Normative concepts and validated generic
abstractions remain in the VIAL repository.

## Status

Experimental foundation. The CLI selects source files, routes tasks, executes
tests and caches results. The `opencode` adapter and patch applier are available
behind explicit calls; model execution is never implicit. When the VIAL core is
present, the runtime composes every official prototype surface: selective
context, cognitive reuse, Deterministic-First routing, capability/authority
gating, intent-logged patch application, cost accounting and persistent
organizational state (see `docs/vial-integration.md`).

## Run

From this repository:

```text
python -m pip install -e .
python -m vial_code_agent --root . --include "*.py"
```

Clone with the official VIAL core:

```text
git clone <vial-code-agent-repository> --recurse-submodules
git submodule update --init --recursive
```

The pinned VIAL implementation is available at `vendor/vial-core`. Code
generation uses its official selective `Context` lifecycle and patch execution
is authorized and audited through VIAL `Decision` and `Tool` contracts.

The command prints the selected files and the route chosen for the request.

Generate a patch with the locally installed `opencode` command:

```text
python -m vial_code_agent --root . --task "fix the parser" --generate
```

The default is preview-only. Apply after reviewing the diff:

```text
python -m vial_code_agent --root . --task "fix the parser" --generate --apply
```

Use `--yes` for automation. If a test command is supplied, failed tests roll
the patch back automatically unless `--keep-on-failure` is used:

```text
vial --root . --task "fix the parser" --generate --apply --yes `
  --test-command python -m unittest discover -s tests
```

Review a patch without applying it:

```text
vial review path/to/change.patch --root .
```

The agent rejects generated patches that modify files outside the selected
context. Local telemetry is written as JSONL without prompts or file contents.
Copy `.vial.json.example` to `.vial.json` to configure a default model and
runtime options. The `vial` command is installed by the editable package install. The optional
`.vial.json` file can define `model`, `cache_dir`, `test_timeout`,
`max_context_chars`, `opencode_executable`, `opencode_agent`, `org_id`,
`authority`, `actor`, `persist_state` and `price_table_json`; matching `VIAL_*` environment
variables override those values.

Additional interfaces:

```text
vial fix "corrija o parser" --generate --apply --yes
vial run --exec-command "python -m unittest discover -s tests"
vial serve --port 8765
vial chat
vial chat --plain
```

`vial chat` opens a widget-based fullscreen terminal UI built with Textual,
following the same component approach used by modern coding terminals: a
message viewport, focused multiline composer, sidebar, command palette and
model picker. Use `--plain` for the legacy line-by-line prompt in terminals
without a compatible fullscreen UI. The fallback also remains available when
the optional Textual dependency cannot be imported.

Typing `/` inside the input raises a floating command picker that filters as
you type. Use Up/Down to highlight a command and Enter (or Tab to complete) to
select it — the same muscle memory as opencode.

Inside `vial chat`, use OpenCode-style commands:

```text
/models [provider]        list models (registered + opencode)
/model provider/model     switch model (auto = route by prompt analysis)
/model add server/model   add a model to a server
/model remove server/model
/providers                list opencode providers
/servers                  list configured HTTP servers
/server add <name> <base_url> [api_key_env]   add an OpenAI-compatible server
/server remove <name>     remove a server
/server models <name>     list a server's models
/pool                     show the parallel routing pool
/pool add <model_ref>     add a model to the routing pool
/pool remove <model_ref>
/status                   show session, model, route and pool
/sessions                 list past sessions
/resume <session_id>      resume a past session
/clear                    start a new session
/exit
```

Prompts are routed automatically from the text alone, mirroring the VIAL
Deterministic-First chain (RFC-010): mechanical tasks (`trim trailing
whitespace`, `add encoding header`) resolve locally without a model call,
explanations go to the cheapest fast model, and implementation tasks fan out
to every model in the routing pool in parallel, taking the first valid
response in pool order. Configure the pool with `/pool add`, or statically in
`.vial.json` under the `pool` key.

OpenAI-compatible servers are registered directly from the terminal with
`/server add <name> <base_url> [api_key_env]` and persisted in `.vial.json`
under the `servers` key. The API key is read from the named environment
variable at call time, never stored in the file.

Discover and select providers/models:

```text
vial providers
vial models
vial models --provider openai
vial fix "corrija o parser" --model openai/gpt-5.6-luna
```

The model identifier always uses `provider/model`, exactly as reported by
`vial models`. `--model auto` keeps the VIAL routing policy.

The local web API exposes `GET /health` and `POST /chat`. The VS Code
extension in `vscode-extension/` connects to that local server. Chat sessions
are stored in `.vial-sessions/`; sequential workflows and multi-agent teams
are available through the `workflow` and `agents` modules.

The command runner is allowlisted by default. `--unsafe` is an explicit opt-in
for trusted workspaces; unrestricted execution is never the default.

Run a workspace test command, keeping it last because it consumes the remaining
CLI arguments:

```text
python -m vial_code_agent --root . --test-command python -m unittest discover -s tests
```

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
vial --root . --task "trim trailing whitespace" --generate --apply --yes
```

Organizational telemetry:

```text
vial status                           # snapshot organizacional (tools, memória, approvals)
vial status --trace DEC-0001          # porquê de uma decisão (audit trail)
```

The local web API exposes `GET /org` for the same organizational snapshot.

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
