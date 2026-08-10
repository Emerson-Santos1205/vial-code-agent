# VIAL Code Agent

Experimental code-generation application built on top of the VIAL core.

This repository is intentionally separate from the VIAL architectural
reference. It owns product behavior such as file selection, model routing and
the developer experience. Normative concepts and validated generic
abstractions remain in the VIAL repository.

## Status

Early scaffold. The current CLI selects source files and applies a deterministic
model-routing policy. Model execution, patch application and test orchestration
will be added only with dedicated benchmarks.

## Run

From this repository:

```text
python -m pip install -e .
python -m vial_code_agent --root . --include "*.py"
```

The command prints the selected files and the route chosen for the request.

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
