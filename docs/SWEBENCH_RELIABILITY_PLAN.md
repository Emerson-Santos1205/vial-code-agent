# SWE-bench Reliability Plan

## Objective

Make the SWE-bench evaluation deterministic, diagnosable, secure, and
reliable before running the full 50-task evaluation again.

## Implementation Checklist

### P0: Immediate Unblock

- [x] Add explicit workflow inputs for primary and consensus models.
- [x] Set both defaults to the verified `openai/gpt-5.6-luna` model.
- [x] Add a reusable provider health-check utility.
- [x] Validate provider exit code, JSON text response, and error events.
- [x] Persist sanitized primary and consensus health results as artifacts.
- [x] Fail before workload execution when a provider is unhealthy.
- [x] Add unit tests for provider health success and error parsing.
- [x] Validate the P0 implementation with the relevant test suite.

### P1: Provider Reliability

- [x] Preserve provider error text and JSON error events in the Docker provider.
- [x] Separate provider failures from patch-contract failures in candidate results.
- [x] Harden JSON event parsing for malformed optional event fields.
- [x] Harden streaming timeouts and process-tree cleanup.
- [x] Add provider failure and health-check unit tests.
- [x] Validate the provider changes with the full test suite (`403 passed`).

### P1: SWE-bench Environments

- [x] Validate every required image before execution.
- [x] Reject malformed Python version declarations.
- [x] Make dependency and image resolution deterministic.
- [x] Parse string test commands into argv safely.
- [x] Record environment fingerprints in benchmark artifacts.

### P2: Isolation and CI Safety

- [x] Add job and stage timeouts.
- [x] Make checkpoints atomic and configuration-scoped.
- [x] Add container resource and privilege restrictions.
- [x] Add concurrency protection for shard output directories.

## Execution Status

The local implementation phases completed with `405 passed`, including
provider timeout cleanup, environment fingerprints, image preflight,
configuration-scoped checkpoints, and Docker privilege restrictions.

The following rollout gates still require the CI runtime and are intentionally
not marked as executed locally:

- Docker image builds and availability for every selected SWE-bench instance;
- provider health checks with the configured credentials and both models;
- one-task, two-task, five-task, and full SWE-bench smoke/evaluation runs;
- deterministic fingerprints across separate CI shard executions.

Dependency installation remains outside the network-isolated test container
contract until all required dependencies are baked into pinned images. The
resolver now emits a versioned repository/revision catalog key and canonical
dependency contract; Docker preflight converts every selected image to an
immutable digest or image ID before execution.

## Incident Diagnosis

The failed shards exposed multiple independent failure classes:

1. **Consensus model unavailable**
   - The primary model was changed to `openai/gpt-5.6-luna`.
   - The default consensus model remained `openai/gpt-4o-mini`.
   - The consensus model repeatedly returned no usable response.
   - This produced `CANDIDATE_SET_INSUFFICIENT` across completed shards.

2. **Historical environment failures**
   - Astropy revisions have incompatible pytest plugins and options.
   - The workflow builds only Python 3.8 and 3.9 images, while the resolver
     can select Python 2.7, 3.7, 3.8, 3.9, and 3.12.

3. **Patch contract failures**
   - Empty patches.
   - Patches that do not apply.
   - Patches modifying files outside the selected context.
   - These occurred frequently in Django tasks.

4. **Provider diagnostics are insufficient**
   - OpenCode JSON error events are not consistently preserved.
   - Provider, authentication, timeout, and model errors are often reported
     as `patch_contract` failures.

The environment fix was effective in at least one validation run:
`environment_status` became `VALID`. The remaining failure was then exposed as
a candidate/provider failure rather than an environment failure.

## P0: Immediate Unblock

1. Change the default `consensus_model` to `openai/gpt-5.6-luna`.
2. Add explicit workflow inputs for both `model` and `consensus_model`.
3. Remove hardcoded model names from the workflow command.
4. Add a provider health-check step before workload execution.
5. Test both models with a minimal `opencode run --format json` request.
6. Validate process exit code, text event, final event, and error events.
7. Stop the job before SWE-bench if either model is unhealthy.
8. Upload sanitized health-check diagnostics as an artifact.
9. Run a one-task `baseline` smoke test.
10. Run a one-task `vial` plus consensus smoke test.

Do not launch all shards until both smoke tests pass.

## P1: Provider Reliability

Primary files:

- `src/vial_code_agent/docker_provider.py`
- `src/vial_code_agent/model.py`
- `benchmark/run_swebench.py`

Required changes:

1. Preserve OpenCode JSON `error` events.
2. Distinguish the following failure codes:
   - `provider_auth_error`
   - `model_unavailable`
   - `provider_timeout`
   - `provider_network_error`
   - `invalid_json_event`
   - `empty_response`
   - `patch_contract`
3. Never convert a provider failure into an empty-patch failure.
4. Record model, provider, OpenCode version, exit code, stderr, duration,
   attempt count, and structured error summary.
5. Parse defensively when `part` is null, a string, or an unexpected type.
6. Handle invalid JSON lines without aborting the entire benchmark.
7. Implement wall-clock timeouts during streaming, not only after EOF.
8. Drain stdout and stderr concurrently to avoid deadlocks.
9. Terminate the complete subprocess tree on timeout or cancellation.
10. Ensure cleanup happens in `finally` blocks.
11. Ensure method-level `timeout_seconds` values are respected.

## P1: Candidate and Governance Semantics

Keep the consensus gate, but make every stage explicit:

1. `candidate_generation`
2. `candidate_contract`
3. `candidate_static_validation`
4. `candidate_consensus`
5. `patch_apply`
6. `behavioral_tests`

Rules:

1. No response from a model is a provider failure.
2. Authentication, rate limit, model, and network failures must not trigger
   patch retries.
3. Consensus unavailability must not be reported as
   `candidate_set_insufficient`.
4. Preserve diagnostics for every candidate attempt.
5. Distinguish provider attempts, contract retries, behavioral retries, and
   total attempts.

## P1: SWE-bench Environment Reliability

Primary files:

- `.github/workflows/swebench.yml`
- `benchmark/swebench_environment.py`
- `benchmark/run_swebench.py`
- `docker/*.Dockerfile`

Required changes:

1. Build or validate every image required by the actual workload before
   evaluation.
2. Validate inside each container:
   - Python version;
   - pytest version;
   - installed plugins;
   - project import;
   - selected test command.
3. Create a versioned environment catalog by repository and revision.
4. Pin base images by digest.
5. Pin pip, setuptools, pytest, plugins, and project requirements.
6. Avoid repeated dependency installation during baseline, candidate, retry,
   and final test phases.
7. Parse `test_command` strings with `shlex.split`; do not quote the entire
   string as one argument.
8. Validate that configured commands execute the selected
   `FAIL_TO_PASS` and `PASS_TO_PASS` tests.
9. Disable only incompatible plugins, preserving required plugins such as
   `pytest-doctestplus`.
10. Record Python, pip freeze, pytest/plugin versions, image digest, and
    requirements fingerprint in the result artifact.

## P2: Execution Isolation and CI Safety

1. Define a workflow `timeout-minutes` value.
2. Use separate budgets for clone, install, generation, tests, and task total.
3. Make checkpoint writes atomic.
4. Include workload hash, commit, model, consensus model, adapters, and image
   fingerprint in checkpoint identity.
5. Reject incompatible checkpoints.
6. Prevent concurrent jobs from writing the same shard directory.
7. Protect fixture rollback with `try/finally`.
8. Preserve full logs as artifacts while limiting console output.
9. Run tests in containers with:
   - `--network none`;
   - read-only filesystem;
   - non-root user;
   - `--cap-drop=ALL`;
   - `no-new-privileges`;
   - explicit CPU, memory, PID, and temporary filesystem limits.
10. Remove credentials, proxies, and CI secrets from test environments.

## Failure Classification

Replace substring-only classification with stage and exception based codes.
Keep substring matching only as a fallback.

At minimum, classify separately:

- dependency installation;
- project build;
- pytest/plugin discovery;
- test runner usage;
- Docker;
- Git/network;
- provider authentication;
- model availability;
- provider timeout;
- response protocol;
- patch contract;
- patch application;
- consensus;
- behavioral test failure.

`environment_valid` must be derived from the effective classification, not
only from the nominal execution stage.

## Required Test Coverage

Add unit and integration tests for:

1. Provider health success and failure.
2. JSON error events.
3. Empty responses.
4. Invalid JSON and malformed `part` values.
5. HTTP 401, 403, 429, and 5xx responses.
6. Streaming timeout and child-process cleanup.
7. Cancellation cleanup.
8. String and list forms of `test_command`.
9. Missing images.
10. Declared versus actual Python version mismatch.
11. Historical Astropy plugin compatibility.
12. Checkpoint mismatch and concurrent writes.
13. Fixture rollback after exceptions.
14. Correct classification for every execution stage.
15. End-to-end Docker provider protocol parsing.

## Rollout Gates

1. Provider health check passes for both configured models.
2. One-task baseline smoke test passes environment validation.
3. One-task VIAL plus consensus smoke test completes with real responses.
4. No provider error is classified as a patch failure.
5. Environment validation reaches 100% on the smoke workload.
6. Two-task shard passes with deterministic fingerprints.
7. Two five-task shards pass before launching all ten shards.
8. Full evaluation runs only after all previous gates pass.

## Success Metrics

Track separately:

- provider availability rate;
- provider response rate;
- environment validity rate;
- candidate patch validity rate;
- consensus agreement rate;
- patch application rate;
- behavioral test success rate;
- task completion rate;
- timeout rate;
- retry rate;
- cost and latency per task.

Do not use a single aggregate failure rate as the primary diagnostic metric.

## References

- `.github/workflows/swebench.yml`
- `benchmark/run_swebench.py`
- `benchmark/swebench_environment.py`
- `src/vial_code_agent/docker_provider.py`
- `src/vial_code_agent/model.py`
- `tests/test_benchmarks.py`
- `tests/test_docker_provider.py`
- `tests/test_model.py`
- OpenCode CLI documentation: <https://opencode.ai/docs/cli/>
- OpenCode provider documentation: <https://opencode.ai/docs/providers/>
