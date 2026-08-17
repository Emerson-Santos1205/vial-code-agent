# VIAL Threat Model

## Assets

- Source files and uncommitted workspace changes.
- Credentials, provider configuration and `.git` metadata.
- Runtime state, decisions, intents, audit records and consensus evidence.
- Integrity of generated patches and test results.

## Trust Boundaries

- Operator and VIAL Runtime.
- Runtime and model/provider process.
- Staging workspace and real workspace.
- Candidate patch and governed mutation tool.
- Runtime state and persistent storage.

## Threats And Controls

| Threat | Control | Residual risk |
| --- | --- | --- |
| Provider writes source files | Disposable provider staging and post-generation detection | OS-level isolation is not complete |
| Path traversal or symlink patch | `PatchApplier` root containment and allowed paths | TOCTOU needs platform-specific hardening |
| Invalid model output | Patch contract retry, parser and `git apply --check` | Model may repeatedly fail |
| False textual consensus | Candidate evidence and behavioral tests in isolated copies | Test quality limits evidence |
| State corruption | Transactional snapshot, manifest and checksums | Filesystem/device failure may remain |
| Dangerous automatic action | Deterministic risk policy for `--auto` | Classification is lexical |
| Replay or interrupted mutation | Intent coordinator and idempotent operation IDs | External side effects need compensation |

## Security Invariants

- No provider output mutates the real workspace directly.
- No patch outside the selected scope is authorized.
- No missing patch is treated as an executable fallback.
- No consensus is accepted without candidate evidence when evidence is required.
- No corrupted active snapshot is silently loaded.

## Executed Adversarial Checks

Run with `python benchmark/run_adversarial.py`:

- path traversal;
- `.git` metadata mutation;
- `allowed_paths` violation;
- invalid patch;
- isolated behavioral evidence;
- symlink escape;
- high-risk automatic approval.

The current suite reports `7/7` checks passed and `0` security violations.

## Validation Plan

- Path traversal, symlink, `.git`, race and invalid patch tests.
- Provider writes to selected, unselected and outside paths.
- Provider timeout and child-process behavior.
- Corrupt manifest and checksum recovery.
- Risk policy tests for low, medium, high and critical tasks.
