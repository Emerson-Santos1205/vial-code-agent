# VIAL Code Agent

Governed coding agent built on VIAL Core, with an auditable runtime,
transactable persistence, and a safe release CLI.

## Layers

- `vial_code_agent.core`: VIAL Core integration.
- `vial_code_agent.vial_runtime`: state, authorization, consensus, tools, and mutations.
- `vial_code_agent.agent`: code generation and model routing.
- `vial_code_agent.api`: stable public boundary for integrations.
- `vial_code_agent.cli` and `vial_code_agent.app`: interfaces with no governance logic of their own.

Workspace mutations must go through `VialRuntime.apply_patch`; the governed
path validates scope, authorization, consensus, audit, commit, and recovery.

## Coding agent

```text
python -m vial_code_agent --root . --vial-root vendor/vial-core --prompt "inspect the project"
python -m benchmark.run_benchmark
python -m benchmark.run_benchmark --agent --model openai/gpt-4o
python -m benchmark.run_benchmark --adapters baseline,opencode,vial --model openai/gpt-4o
```

The default benchmark is a synthetic **Unit / Regression Benchmark**: it runs
100 isolated fixtures across eight categories, all derived from small,
deterministic transforms. It measures patch application, validation, rollback,
retries, and test execution; **it is not a coding-agent quality estimate and
does not replace SWE-bench**.

The `--agent` (or `--adapter opencode`) mode generates the patch through the
configured coding agent, applies it to a disposable fixture, runs the task
tests, and writes a JSON report to `benchmark/results/`. Reports include
success rate, latency, tokens, regressions, patch failures, rollbacks, and
human intervention.

Models use the `provider/model` format. Public defaults are
`openai/gpt-4o-mini` for fast tasks and `openai/gpt-4o` for reasoning;
availability depends on provider authentication. Do not use internal
execution aliases as public configuration identifiers.

In SWE-bench reports, success is decomposed into two metrics:
`agent_success_rate` is correct solutions divided by environmentally valid
tasks, while `end_to_end_success_rate` is correct solutions divided by all
tasks. Thus, failures classified as `environment` are not confused with agent
failures but remain included in the end-to-end evaluation.

`--adapters baseline,opencode,vial` runs the same synthetic matrix through
three paths: direct provider, conventional agent, and VIAL-Runtime-composed
agent. Real workloads can be supplied with
`--workload path/to/workload.json` using the same `tasks` structure with
`id`, `category`, `prompt`, `initial`, `patch`, and `tests`.

To download real SWE-bench Lite instances:

```text
python benchmark/fetch_swebench.py --split test --offset 0 --length 10 --out benchmark/swebench-lite-real.json
```

The dataset contains issue, repository, base commit, reference patch, and test
lists. Full execution requires cloning each repository at the base commit and
installing its dependencies, so it is not treated as a simple local fixture.
In the SWE-bench executor, the test image is chosen per instance/repository
when `--test-image` is not provided; this parameter exists only as an
experimental override for controlled reproductions.
The environment contract is resolved before the workspace and can declare
Python version, dependencies, test command, and metadata. Images are reusable
families by version, not a mandatory image per instance.
The SWE-bench report is persisted in `benchmark/results/` and records
repository, base commit, image, Python, dependencies, timeout, classification,
and evidence per task. The agent patch application is fail-closed: without
independent consensus provided via `--consensus-file`, the task is blocked
without mutating the workspace.
To generate that consensus automatically, provide a second independent model
with `--consensus-model`; divergent candidates are blocked.
For Astropy, the environment uses the pre-built image
`vial-code-agent-swebench-python39:local`, which pins `pytest==7.4.4`,
`Cython<3`, `pytest-astropy==0.9.0`, and `pytest-astropy-header==0.1.2`,
compiles extensions with `build_ext --inplace` against the container's current
ABI, and runs pytest with the warnings plugin disabled.
Compatible with historical SWE-bench commits.

Test validation in Docker sandbox:

```text
python benchmark/run_sandbox.py --limit 1
```

The executor uses disabled network, read-only filesystem, and only `/tmp`
writable.

OpenCode provider image:

```text
docker build -f docker/opencode.Dockerfile -t vial-code-agent-opencode:1.18.30 .
docker run --rm vial-code-agent-opencode:1.18.30 --version
```

Credentials must be mounted only at runtime, never copied into the image:

```text
docker run --rm --network none \
  --mount type=bind,src=%USERPROFILE%\.local\share\opencode\auth.json,dst=/root/.local/share/opencode/auth.json,readonly \
  vial-code-agent-opencode:1.18.30 providers list
```

Consensus for mutations may require evidence: each candidate is applied to a
disposable copy and statically validated; when `--test-command` is used,
behavioral tests must also pass before consensus is accepted.

### Fail-Closed Security Cost

The consensus protocol trades inference cost for lower risk of applying an
invalid solution. In a diagnostic run of 10 tasks, 48 candidate attempts were
recorded, 28 patches returned, 23 patches statically valid, and 17 candidates
also approved by behavioral tests. The run consumed 199,713 tokens,
approximately 19,971 tokens per task.

In that report, `candidate_completion_rate` is the ratio of returned patches to
candidate attempts (`28/48 = 0.58`). `candidate_reliability_rate` requires
static validity and behavioral approval, using all attempts as denominator
(`17/48 = 0.35`). Retries and responses without a patch remain in the
denominator; therefore these metrics make discarded model work visible before
governance. The cost is intentional: the flow is fail-closed and does not
mutate the workspace without sufficient independent evidence.

These numbers are diagnostics of a specific run, not a fixed cost estimate.
They vary by model, prompt, workload, retries, and tests.

### First Published SWE-bench Evidence

The first versioned real report is available at
[`benchmark/results/swebench-lite-10-consensus-2026-08-23.json`](benchmark/results/swebench-lite-10-consensus-2026-08-23.json).
It covers 10 SWE-bench Lite tasks with two independent candidates, behavioral
validation, and adjudication where applicable. The result was 7/10
end-to-end, with 6/10 candidate A valid, 7/10 candidate B valid, and 7/10
consensus approved. The three blocked tasks remain in the report with their
insufficient-candidate evidence rather than being removed from the score.

The first SWE-bench Verified pilot uses 5 instances and is available at
[`benchmark/results/swebench-verified-5-consensus-2026-08-25.json`](benchmark/results/swebench-verified-5-consensus-2026-08-25.json).
It achieved 2/5 end-to-end with 4/5 valid environments. This is an
infrastructure pilot, not a statistical sample or marketing number.
The rerun after executor fixes is at
[`benchmark/results/swebench-verified-5-consensus-rerun-2026-08-25.json`](benchmark/results/swebench-verified-5-consensus-rerun-2026-08-25.json): all 5 environments were valid, with 2/5 end-to-end.

A synthetic comparison of 100 tasks per adapter is available at
[`benchmark/results/synthetic-adapter-cost-comparison-2026-08-23.json`](benchmark/results/synthetic-adapter-cost-comparison-2026-08-23.json).
In that workload, `opencode` and `vial` both achieved 100/100. The VIAL path
consumed 84,701 tokens versus 66,756 for the `opencode` path (+26.9%) and had
an average latency of 9.40 s versus 9.01 s (+4.3%). This is a measurement of
the complete protocol on this synthetic benchmark, not isolated VIAL Core
overhead or a SWE-bench quality estimate.

New runs should use SWE-bench Verified. The default manual workflow fetches
50 instances from `princeton-nlp/SWE-bench_Verified`; results with fewer than
50 tasks are marked `diagnostic_only`. The report includes `economics` with
inference tokens from all candidates, duration, and cost/time per resolved
task. VIAL context tokens are kept separate and not sold as model consumption.

To run a large sample without concentrating all instances in a single job,
split the range into balanced shards. Each shard records the processed indices
in the report and can use its own `--out` directory to keep checkpoints
isolated.

```text
python benchmark/run_swebench.py --workload benchmark/swebench-verified-50.json \
  --model openai/gpt-4o --run-tests --limit 50 \
  --shard-index 0 --shard-count 10 --out benchmark/results/verified-00
```

Repeat for `--shard-index` from `0` to `9`. The `SWE-bench Real Evaluation`
workflow exposes the same fields to trigger each shard separately.

After completing the shards, consolidate them without mixing models or
workloads:

```text
python -m benchmark.aggregate_swebench benchmark/results/comparison-*/report-*.json \
  --out benchmark/results/verified-comparison.json
```

The aggregator rejects incompatible contracts and duplicate results by
`adapter:task_id`.

The manual workflow publishes each shard's report and checkpoint as a GitHub
Actions artifact. Download all artifacts, extract the reports, and run the
aggregator locally to produce the consolidated result.

The real executor also compares the three protocols on the same instance
selection. `baseline` makes a direct provider call, `opencode` uses the
`CodeAgent` without runtime, and `vial` uses the governed runtime. The report
creates `by_adapter` with success and savings for each path, and maintains
checkpoints by `adapter:index` so an interruption does not mix results.

```text
python benchmark/run_swebench.py --workload benchmark/swebench-verified-50.json \
  --model openai/gpt-4o --run-tests --limit 50 \
  --adapters baseline,opencode,vial --shard-index 0 --shard-count 10 \
  --out benchmark/results/comparison-00
```

When `vial` receives `--consensus-model`, only that adapter uses the second
model and consensus validation; `baseline` and `opencode` remain single-
candidate measurements on the same primary model.

Build pilots with distinct repositories to avoid measuring only one base
history's infrastructure:

```text
python benchmark/fetch_swebench.py --dataset princeton-nlp/SWE-bench_Verified \
  --split test --length 10 --unique-repos --out benchmark/swebench-verified-diverse-10.json
```

Before the comparison, validate each environment once. `--preflight-only`
runs `FAIL_TO_PASS` and `PASS_TO_PASS` but does not call a model or create
patches; instances whose baseline behavior does not match the contract are
marked `baseline_tests` and should not enter the commercial score.

```text
python benchmark/run_swebench.py --workload benchmark/swebench-verified-diverse-10.json \
  --run-tests --preflight-only --limit 10 --out benchmark/results/preflight
```

## Installation

The project depends on VIAL Core in `vendor/vial-core`, configured as a Git
submodule. For a fresh clone, initialize the repository including submodules:

```text
git clone --recurse-submodules https://github.com/Emerson-Santos1205/vial-code-agent.git
cd vial-code-agent
```

If the repository was already cloned without `--recurse-submodules`, initialize
the submodule manually:

```text
git submodule update --init --recursive
```

Confirm that `vendor/vial-core` exists before running the application or
benchmarks. Then install the package locally:

```text
python -m pip install -e .
```

For full development (including linting and type-checking tools):

```text
python -m pip install -e .[dev]
```

### Code Quality & Tests

To run the full test suite and static quality checks:

```text
python -m pytest                        # Run the 417 unit and integration tests
python -m ruff check src/ tests/        # Linting and import validation with Ruff
python -m mypy src/vial_code_agent      # Static type checking
```

### Providers & VS Code

Configure OpenAI-compatible providers without depending on OpenCode aliases:

```text
vial --root . --add-server local http://127.0.0.1:11434/v1
vial --root . --add-model local/qwen2.5-coder
vial --root . --pool-set local/qwen2.5-coder openai/gpt-4o
```

For the VS Code extension or HTTP integrations, start the loopback server:

```text
vial --root . --serve
```

The server exposes:
- `GET /health`: Server status and workspace root.
- `GET /api/v1/schema` or `GET /openapi.json`: Full OpenAPI 3.0 API specification.
- `POST /chat`: Send prompts to the agent.

Before first use, diagnose the installation without calling any model:

```text
vial --root . --doctor
vial --root . --doctor --json
```

The server accepts only loopback addresses and pins the workspace to the
process that started it; it does not accept workspace paths from the extension.

## Release Orchestrator

The release orchestration CLI is invoked via `python -m release_orchestrator`.

### Usage

```text
python -m release_orchestrator scan
python -m release_orchestrator scan --json
python -m release_orchestrator changelog release-orchestrator-v0.1.0 --force
python -m release_orchestrator check --allow-dirty
python -m release_orchestrator release 1.2.3 --confirm
python -m release_orchestrator release 1.2.3 --confirm --dry-run
python -m release_orchestrator rollback 1.2.3 --confirm
python -m release_orchestrator rollback 1.2.3 --dry-run
```

### Subcommands

- `scan`: show current branch, latest commit, and modified files.
- `changelog`: generate `CHANGELOG.md` from a tag.
- `check`: validate README, tests, secrets, test suite, and working tree.
- `release`: validate semver, require `--confirm`, run checks, update `VERSION` and `CHANGELOG.md`, and create an annotated tag.
- `rollback`: remove only the tag created by the tool.

### Release artifacts

Tags `vMAJOR.MINOR.PATCH` trigger the distribution workflow. The workflow
validates entry points by installing the wheel and publishes wheel/sdist as
GitHub Actions artifacts. PyPI is not part of the current critical path and
will be enabled when the product is mature enough for public distribution.

### Exit codes

- `0`: success.
- `1`: validation failure, dirty repository, secret file, broken test, missing confirmation, or refused operation.

All errors are sent to `stderr`.

### Limitations

- Uses the Python standard library and `textual` for the TUI interface.
- Requires `git` and `python -m unittest` available in the environment.
- `changelog` refuses to overwrite `CHANGELOG.md` without `--force`.
- `rollback` removes only tags in the `release-orchestrator-vMAJOR.MINOR.PATCH` format.
- `--dry-run` does not persist changes.
- `release` requires `--confirm` before creating the tag and updating files.

### JSON

`scan`, `check`, and `changelog` support `--json` for structured output.

## License

VIAL Code Agent is distributed under the [Apache License 2.0](LICENSE). The
VIAL Core included in `vendor/vial-core` is a separate submodule with its own
license declaration.
