# VIAL Code Agent

Experimental code-generation application built on top of the VIAL core.

This repository is intentionally separate from the VIAL architectural
reference. It owns product behavior such as file selection, model routing and
the developer experience. Normative concepts and validated generic
abstractions remain in the VIAL repository.

## Status

Experimental foundation. The CLI selects source files, routes tasks, executes
tests and caches results. The `opencode` adapter and patch applier are available
behind explicit calls; model execution is never implicit.

## Run

From this repository:

```text
python -m pip install -e .
python -m vial_code_agent --root . --include "*.py"
```

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

The `vial` command is installed by the editable package install. The optional
`.vial.json` file can define `model`, `cache_dir`, `test_timeout`,
`max_context_chars`, `opencode_executable` and `opencode_agent`; matching `VIAL_*` environment
variables override those values.

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

## Benchmark

The offline benchmark validates the complete local loop without paid model
calls: create an isolated workspace, apply a patch, execute tests and report
quality.

```text
python benchmark/run_benchmark.py
```

The model-backed benchmark is intentionally separate and requires an
authenticated `opencode` installation.
