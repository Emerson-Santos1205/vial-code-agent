# VIAL Code Agent

Agente de coding baseado no VIAL Core, com Runtime governado, persistência
auditável e uma CLI de release segura.

## Camadas

- `vial_code_agent.core`: integração com o VIAL Core.
- `vial_code_agent.vial_runtime`: estado, autorização, consenso, ferramentas e mutações.
- `vial_code_agent.agent`: geração de código e roteamento de modelos.
- `vial_code_agent.api`: fronteira pública estável para integrações.
- `vial_code_agent.cli` e `vial_code_agent.app`: interfaces, sem regras de governança próprias.

Mutação de workspace deve passar por `VialRuntime.apply_patch`; o caminho
governado valida escopo, autorização, consenso, auditoria, commit e recuperação.

## Coding agent

```text
python -m vial_code_agent --root . --vial-root vendor/vial-core --prompt "inspect the project"
python benchmark/run_benchmark.py
python benchmark/run_benchmark.py --agent --model openai/gpt-5.6-luna
```

O benchmark padrão executa 50 fixtures isoladas em oito categorias. O modo
`--agent` (ou `--adapter opencode`) gera o patch através do coding-agent
configurado, aplica-o em uma fixture descartável, roda os testes da tarefa e
grava relatório JSON em `benchmark/results/`. Os relatórios incluem taxa de
sucesso, latência, tokens, regressões, rollbacks e intervenção humana.

Consenso para mutações pode exigir evidência: cada candidato é aplicado em uma
cópia descartável e validado estaticamente; quando `--test-command` é usado,
os testes comportamentais também precisam passar antes do consenso ser aceito.

## Instalação

```text
python -m pip install -e .
```

O comando principal é `python -m release_orchestrator`.

## Uso

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

## Subcomandos

- `scan`: mostra branch atual, último commit e arquivos modificados.
- `changelog`: gera `CHANGELOG.md` a partir de uma tag.
- `check`: valida README, testes, segredos, suíte de testes e working tree.
- `release`: valida semver, exige `--confirm`, executa checks, atualiza `VERSION` e `CHANGELOG.md`, e cria tag anotada.
- `rollback`: remove somente a tag criada pela ferramenta.

## Códigos de saída

- `0`: sucesso.
- `1`: falha de validação, repositório sujo, arquivo secreto, teste quebrado, confirmação ausente ou operação recusada.

Todos os erros são enviados para `stderr`.

## Limitações

- Usa apenas a biblioteca padrão do Python.
- Requer `git` e `python -m unittest` disponíveis no ambiente.
- `changelog` recusa sobrescrever `CHANGELOG.md` sem `--force`.
- `rollback` remove apenas tags no formato `release-orchestrator-vMAJOR.MINOR.PATCH`.
- `--dry-run` não persiste alterações.
- `release` exige `--confirm` antes de criar tag e atualizar arquivos.

## JSON

`scan`, `check` e `changelog` suportam `--json` para saída estruturada.
