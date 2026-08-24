# Changelog

## 0.1.0
_Changes since initial release_

### feat
- 969a127 feat: scaffold VIAL code agent
- bbe9db5 feat: add model adapter and codegen benchmark
- eccec52 feat: complete code generation workflow
- ae2ec72 feat: add review safety and telemetry
- da4c781 feat: integrate official VIAL core
- 7688b4f feat: add governed tool catalog, cognition engine, and audit trails
- 6c16c19 feat: VIAL runtime improvements, event hub, project state, and reuse persistence
- 50b4488 feat: upgrade TUI with streaming, task cancellation, session picker, and multi-line prompt
- 19b1578 feat: add cross-model consensus dispatch for high-risk tasks
- 38d2083 feat: enforce cross-model consensus gate for mutation tools (TOOLS-001)
- 8543402 feat: operator opt-out and graceful no-pool fallback for consensus gate
- ca0c05e feat: harden TUI responses and add release orchestrator
- d0c8b2c feat: complete governed TUI workflow and UX fixes
- e3929e8 feat: complete release orchestrator implementation
- a3b60c2 feat: enforce runtime mutation boundaries
- 4246ea1 feat: harden persistence and expand coding benchmark
- b1db80e feat: add evidence-backed agent evaluation
- 4c5ceb6 feat: compare benchmark adapters
- f106154 feat: recover bounded patch contract failures
- b5d25b2 feat: harden persistence and risk controls
- 5201f2b feat: expose governed pipeline in TUI
- bdd1f6c feat: add governed TUI inspection views
- ef7a32d feat: add adversarial security validation
- 4993ddc feat: complete TUI observability and benchmark score
- 8f637de feat: add TUI failure diagnostics and coverage gate
- a27e205 feat: add Docker sandbox benchmark runner
- 3a260a7 feat: package OpenCode provider and real dataset fetcher
- e8040c9 feat: run SWE-bench with Docker provider
- 8741831 feat: harden real patch recovery
- 795e067 feat: support multi-hunk patch recovery
- 7a91b6a feat: run complete swebench tests
- 5a564a8 feat: add test feedback retries and linux swebench
- 0f8c6c7 feat: add event-driven pipeline observability
- 8d04369 feat: harden benchmark patch validation
- 6ce62ed feat: add candidate diagnostic telemetry

### fix
- 64502cf fix: pin vial-core to d681c01, allowlist python3, remove dead duplicate modules
- a7d1434 fix: do not manufacture consensus (CODE_OF_CONDUCT #31)
- ab738aa fix: add VERSION file to test_python_module_entry_point
- 470783f fix: keep TUI selection inside output log
- 336fe7e fix: pass Windows model prompts through stdin
- 391c3f0 fix: make TUI model timeout configurable
- 1cdc8c2 fix: require evidence for consensus candidates
- 479188e fix: validate generated patches in staging
- 4cba160 fix: ground patch prompts in staged files
- 3dcc20a fix: install vendored vial core in ci
- fb8e2a1 fix: align required ci check name
- de27146 fix: govern SWE-bench patch execution

### docs
- d9376dc docs: document submodule setup
- 34cecc8 docs: publish benchmark evidence and cost comparison

### test
- 365caa9 test: raise coverage to 91% with unit tests for chat, cli, model, and errors
- ccd9601 test: make CI hermetic for Linux runners
- cafc87d test: make release orchestrator fixture unittest-compatible
- 42164cb test: use unittest fixture in all release checks
- 685d32c test: support platform-specific prompt transport
- 4706c3e test: synchronize TUI streaming assertion

### other
- b82dbd2 chore: standardize version at 0.3.0 and add coverage gate to CI
- 9341f83 chore: remove duplicate size_utils helper not used by the agent flow
- 80fedfd Merge pull request #1 from Emerson-Santos1205/development
- d661fde Merge branch 'main' into development
- 4c69ef3 Merge pull request #2 from Emerson-Santos1205/development
- b877e1b Merge branch 'main' into development
- 337a12c Merge pull request #3 from Emerson-Santos1205/development
- cb665f7 Merge branch 'main' into development
- d3a925e Merge pull request #4 from Emerson-Santos1205/development
- e141a0c Merge remote-tracking branch 'origin/development' into development
- 267966e Merge branch 'main' into development
- a6574ea Merge remote-tracking branch 'origin/development' into development
- 3d134f2 Merge pull request #5 from Emerson-Santos1205/development
- 52185b2 Merge branch 'main' into development
- ac9f50a Merge pull request #6 from Emerson-Santos1205/development
- 0497618 Merge remote-tracking branch 'origin/development' into development
- d806f50 Merge pull request #7 from Emerson-Santos1205/development
- 2abd04e Merge branch 'main' into development
- 6038db6 Merge pull request #8 from Emerson-Santos1205/development
- e94587f Merge branch 'main' into development
- 7c8c8cb Merge pull request #9 from Emerson-Santos1205/development
- 8fefb04 Merge branch 'main' into development
- 871d5b0 Merge pull request #10 from Emerson-Santos1205/development
- 4ac7d63 optimize historical SWE-bench environments
- 80da820 Merge pull request #11 from Emerson-Santos1205/development
- c18e4a4 Merge branch 'main' into development
- 7834efd Merge remote-tracking branch 'origin/development' into development
- 6d746c9 chore: prepare first public release
